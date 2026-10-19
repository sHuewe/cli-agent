"""Optional offline-indexed semantic vector retrieval, without extra dependencies.

Neither the query model nor the retrieval model receives filesystem paths with
unrestricted read permissions. The index is a candidate locator, never a
source of authoritative document content.
"""
from __future__ import annotations

import hashlib
import math
import os
import sqlite3
import struct
import tempfile
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx

from .config import VectorKnowledgeConfig
from .filesystem_security import regular_file_has_multiple_links
from .model_factory import _validate_api_key_env
from .config import ModelConfig
from .network_policy import NetworkConfig, validate_http_url

MAX_SOURCE_BYTES = 2_560_000
MAX_SEARCH_QUERY = 2000
MAX_SEARCH_RESULTS = 20
MAX_STALE_CANDIDATES = 100
MAX_BATCH = 32


def source_root(config: VectorKnowledgeConfig, *, okf_root: Path | None, config_file: Path | None) -> Path:
    if config.mode != "okf":
        raise ValueError("Nur OKF-basierte Vektorsuche wird unterstützt.")
    if okf_root is None:
        raise ValueError("knowledge.vector.mode='okf' erfordert [okf].repository.")
    return okf_root.resolve()


def db_path(config: VectorKnowledgeConfig, *, config_file: Path | None) -> Path:
    path = Path(config.database).expanduser()
    if not path.is_absolute():
        path = (config_file.parent if config_file else Path.cwd()) / path
    return path.resolve()


def _source_file(root: Path, relative: str) -> Path:
    if not relative or "\\" in relative or relative.startswith("/") or ":" in relative:
        raise ValueError("Ungültiger Knowledge-Pfad.")
    pure = Path(relative)
    if any(part in {".", "..", ""} for part in pure.parts) or pure.is_absolute():
        raise ValueError("Ungültiger Knowledge-Pfad.")
    candidate = root / pure
    # Reject symlinks in every component, including links that remain in-root.
    current = root
    for part in pure.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("Symbolische Links sind keine Knowledge-Quellen.")
    resolved = candidate.resolve()
    if not resolved.is_relative_to(root) or not resolved.is_file():
        raise ValueError("Knowledge-Pfad liegt nicht in der konfigurierten Quelle.")
    if regular_file_has_multiple_links(resolved):
        raise ValueError("Knowledge-Dateien mit Hardlinks sind nicht erlaubt.")
    return resolved


def _read_verified_okf_bytes(root: Path, relative: str, *, max_read_bytes: int) -> bytes:
    """Read an OKF Concept through the shared bounded descriptor reader."""
    from .filesystem_security import read_bounded_regular_file
    validated = _source_file(root, relative)
    identity = validated.stat()
    return read_bounded_regular_file(
        root, relative, max_bytes=max_read_bytes,
        expected_identity=(identity.st_dev, identity.st_ino),
    )


def _read_source(
    root: Path, relative: str, *, mode: str,
    max_read_bytes: int = MAX_SOURCE_BYTES,
) -> str:
    if mode != "okf":
        raise ValueError("Nur OKF-basierte Vektorsuche wird unterstützt.")
    if Path(relative).name in {"index.md", "log.md"} or Path(relative).suffix.lower() != ".md":
        raise ValueError("Nur OKF-Concepts können indexiert werden.")
    content = _read_verified_okf_bytes(root, relative, max_read_bytes=max_read_bytes).decode("utf-8")
    # Apply the same restrictive OKF frontmatter parser as the MCP server,
    # without reopening the pathname after the verified descriptor read.
    from .okf_mcp_server.repository_support import RepositoryMetadataMixin
    RepositoryMetadataMixin._parse_frontmatter(content)
    return content


def _okf_paths(root: Path, *, max_files: int, max_read_bytes: int = MAX_SOURCE_BYTES) -> list[str]:
    from .okf_mcp_server.repository import OkfRepository
    repo = OkfRepository.from_directory(root, max_read_bytes=max_read_bytes, max_index_entries=2000)
    queue = [(".", "knowledge_index")]
    seen: set[tuple[str, str]] = set()
    concepts: set[str] = set()
    while queue:
        path, tool = queue.pop(0)
        if (path, tool) in seen:
            continue
        seen.add((path, tool))
        if len(seen) > max_files * 20 + 2000:
            raise ValueError("OKF-Navigation überschreitet das Indexierungslimit.")
        if tool == "knowledge_index":
            result = repo.knowledge_index(path)
        else:
            result = repo.knowledge_read(path)
            if result["kind"] == "concept":
                concepts.add(path)
                if len(concepts) > max_files:
                    raise ValueError("OKF enthält zu viele Concepts.")
        for link in (*result.get("internal_links", []), *result.get("entries", [])):
            if not isinstance(link, dict) or link.get("exists") is False:
                continue
            next_tool = link.get("next_tool")
            next_path = link.get("path")
            if next_tool in {"knowledge_index", "knowledge_read"} and isinstance(next_path, str):
                queue.append((next_path, next_tool))
    return sorted(concepts)


def _chunks(text: str, *, size: int = 1800, overlap: int = 200) -> list[str]:
    if not text.strip():
        return []
    result = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        if end < len(text):
            cut = text.rfind("\n", start + size // 2, end)
            if cut > start:
                end = cut + 1
        value = text[start:end].strip()
        if value:
            result.append(value)
        if end == len(text):
            break
        start = max(start + 1, end - overlap)
    return result


def _encode(vector: list[float]) -> bytes:
    if not vector or len(vector) > 8192 or any(type(v) not in (int, float) or not math.isfinite(v) for v in vector):
        raise ValueError("Embedding enthält ungültige Werte.")
    return struct.pack(f"<{len(vector)}f", *vector)


def _decode(blob: bytes) -> tuple[float, ...]:
    if len(blob) % 4 or not blob or len(blob) > 8192 * 4:
        raise ValueError("Ungültiger gespeicherter Embedding-Vektor.")
    return struct.unpack(f"<{len(blob) // 4}f", blob)


def _embedding_endpoint_identity(base_url: str) -> str:
    """Canonical endpoint identity; never persist secrets or query parameters."""
    parsed = urlsplit(base_url)
    if parsed.username is not None or parsed.password is not None or parsed.query or parsed.fragment:
        raise ValueError("Embedding-Endpunkt darf keine Credentials, Query oder Fragmente enthalten.")
    scheme = parsed.scheme.lower()
    hostname = (parsed.hostname or "").lower().rstrip(".")
    if not hostname or scheme not in {"http", "https"}:
        raise ValueError("Ungültiger Embedding-Endpunkt.")
    host = f"[{hostname}]" if ":" in hostname else hostname
    port = parsed.port
    if port is not None and port != (80 if scheme == "http" else 443):
        host += f":{port}"
    return urlunsplit((scheme, host, parsed.path.rstrip("/"), "", ""))


class EmbeddingProvider:
    def __init__(self, config: VectorKnowledgeConfig, network: NetworkConfig, credential_rules: tuple[Any, ...] = ()) -> None:
        self.config = config
        self.base_url = validate_http_url(config.base_url, allowed_hosts=network.model_allowed_hosts, purpose="Embedding-Modell").rstrip("/")
        self.endpoint_identity = _embedding_endpoint_identity(self.base_url)
        if config.provider == "openai":
            _validate_api_key_env(ModelConfig(provider="openai", base_url=self.base_url, api_key_env=config.api_key_env), credential_rules)
        elif config.api_key_env:
            raise ValueError("api_key_env wird nur für den OpenAI-Embedding-Provider unterstützt.")

    async def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts or len(texts) > MAX_BATCH or any(not isinstance(t, str) or not t or len(t) > 16000 for t in texts):
            raise ValueError("Ungültiger Embedding-Batch.")
        config = self.config
        headers: dict[str, str] = {}
        if config.api_key_env:
            key = os.environ.get(config.api_key_env)
            if not key:
                raise ValueError(f"Embedding-Credential {config.api_key_env!r} fehlt.")
            headers["Authorization"] = f"Bearer {key}"
        if config.provider == "ollama":
            url = self.base_url + "/api/embed"
            payload = {"model": config.model, "input": texts}
        else:
            url = self.base_url + "/embeddings"
            payload = {"model": config.model, "input": texts}
        async with httpx.AsyncClient(timeout=90, follow_redirects=False, trust_env=False) as client:
            response = await client.post(url, json=payload, headers=headers)
            response.raise_for_status()
            data = response.json()
        if config.provider == "ollama":
            vectors = data.get("embeddings")
        else:
            entries = data.get("data")
            # OpenAI-compatible embeddings return the original input position
            # for each vector. Reject duplicates, gaps, booleans, negative or
            # out-of-range indices instead of silently misassigning vectors.
            if not isinstance(entries, list) or len(entries) != len(texts):
                raise RuntimeError("Embedding-Antwort hat eine ungültige Anzahl Vektoren.")
            indices = [entry.get("index") if isinstance(entry, dict) else None for entry in entries]
            if (
                any(type(index) is not int for index in indices)
                or sorted(indices) != list(range(len(texts)))
            ):
                raise RuntimeError("Embedding-Antwort enthält ungültige oder doppelte Indizes.")
            vectors = [entry["embedding"] for entry in sorted(entries, key=lambda x: x["index"])
                       if "embedding" in entry]
            if len(vectors) != len(texts):
                raise RuntimeError("Embedding-Antwort enthält Einträge ohne Vektor.")
        if not isinstance(vectors, list) or len(vectors) != len(texts):
            raise RuntimeError("Embedding-Antwort hat eine ungültige Anzahl Vektoren.")
        dim = None
        for vector in vectors:
            if not isinstance(vector, list):
                raise RuntimeError("Ungültiges Embedding-Format.")
            _encode(vector)
            if dim is not None and len(vector) != dim:
                raise RuntimeError("Inkonsistente Embedding-Dimensionen.")
            dim = len(vector)
        return vectors


@dataclass(frozen=True)
class IndexUpdateResult:
    """Counts of changed source Concepts from one atomic update run."""

    added: int
    changed: int
    unchanged: int
    removed: int

    @property
    def total(self) -> int:
        return self.added + self.changed + self.unchanged


class SQLiteKnowledgeStore:
    def __init__(self, path: Path, *, readonly: bool = True) -> None:
        self.path = path
        if readonly:
            # pathlib percent-encodes URI metacharacters (notably ?, #, %).
            # A raw "file:" + path string can silently open a different file.
            self.db = sqlite3.connect(path.absolute().as_uri() + "?mode=ro", uri=True)
        else:
            path.parent.mkdir(parents=True, exist_ok=True)
            self.db = sqlite3.connect(str(path))
            self.db.execute("PRAGMA journal_mode=WAL")
        self.db.row_factory = sqlite3.Row

    def close(self) -> None:
        self.db.close()

    def create(self) -> None:
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sources (
                path TEXT PRIMARY KEY, sha256 TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS chunks (
                id INTEGER PRIMARY KEY, source TEXT NOT NULL,
                vector BLOB NOT NULL,
                FOREIGN KEY(source) REFERENCES sources(path)
            );
        """)

    def metadata(self) -> dict[str, str]:
        return {r["key"]: r["value"] for r in self.db.execute("SELECT key,value FROM metadata")}

    def configure(self, *, root: Path, mode: str, provider: str, model: str, endpoint: str, dim: int,
                  max_read_bytes: int = MAX_SOURCE_BYTES) -> None:
        values = {"root": str(root), "mode": mode, "provider": provider, "model": model,
                  "endpoint": endpoint, "dimension": str(dim),
                  "okf_max_read_bytes": str(max_read_bytes), "schema": "4"}
        self.db.executemany("INSERT OR REPLACE INTO metadata(key,value) VALUES (?,?)", values.items())

    def check(self, *, root: Path, mode: str, provider: str, model: str, endpoint: str,
              max_read_bytes: int = MAX_SOURCE_BYTES) -> int:
        meta = self.metadata()
        expected = {"root": str(root), "mode": mode, "provider": provider, "model": model,
                    "endpoint": endpoint, "okf_max_read_bytes": str(max_read_bytes), "schema": "4"}
        if any(meta.get(k) != v for k, v in expected.items()):
            raise ValueError(
                "Knowledge-Index passt nicht zu Quelle/Embedding-Modell/Endpunkt; "
                "bitte mit init-vector-db neu indexieren."
            )
        dim = int(meta["dimension"])
        if not 1 <= dim <= 8192:
            raise ValueError("Ungültige Vektordimension im Index.")
        return dim

    def replace_documents(self, records: list[tuple[str, str, list[list[float]]]]) -> None:
        """Store only concept identifiers, source fingerprints and embedding vectors.

        Raw document/Chunk text is used only by the indexing process to compute
        embeddings and fingerprints, never passed to or persisted by this store.
        """
        with self.db:
            self.db.execute("DELETE FROM chunks")
            self.db.execute("DELETE FROM sources")
            for path, sha256, vectors in records:
                self.db.execute("INSERT INTO sources(path,sha256) VALUES (?,?)", (path, sha256))
                for vector in vectors:
                    self.db.execute(
                        "INSERT INTO chunks(source,vector) VALUES (?,?)",
                        (path, _encode(vector)),
                    )

    def copy_unchanged_source(
        self,
        old_store: "SQLiteKnowledgeStore",
        *,
        path: str,
        sha256: str,
        dimension: int,
    ) -> None:
        """Copy only binary vectors from a validated prior index.

        A matching source hash guarantees the current original document is
        unchanged. Do not decode or persist the document text here.
        """
        self.db.execute(
            "INSERT INTO sources(path,sha256) VALUES (?,?)", (path, sha256)
        )
        for row in old_store.db.execute(
            "SELECT vector FROM chunks WHERE source=? ORDER BY id", (path,)
        ):
            blob = row["vector"]
            if not isinstance(blob, bytes) or len(blob) != dimension * 4:
                raise ValueError("Ungültiger Vektor im bisherigen Knowledge-Index.")
            # Fail closed on corrupted or non-finite persisted vectors.
            _encode(list(_decode(blob)))
            self.db.execute(
                "INSERT INTO chunks(source,vector) VALUES (?,?)", (path, blob)
            )

    def source_sha(self, path: str) -> str | None:
        row = self.db.execute("SELECT sha256 FROM sources WHERE path=?", (path,)).fetchone()
        return row["sha256"] if row is not None else None

    def search(self, vector: list[float], *, limit: int = 5) -> list[dict[str, Any]]:
        """Rank concepts by their best chunk cosine similarity, without lexical data."""
        if not 1 <= limit <= MAX_STALE_CANDIDATES:
            raise ValueError("Ungültiges Knowledge-Suchlimit.")
        # Verify the supplied query vector before evaluating any stored vectors.
        _encode(vector)
        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0:
            raise ValueError("Embedding hat Norm 0.")
        best_similarity: dict[str, float] = {}
        for row in self.db.execute("SELECT source,vector FROM chunks"):
            saved = _decode(row["vector"])
            _encode(list(saved))  # Reject non-finite or otherwise invalid persisted floats.
            if len(saved) != len(vector):
                raise ValueError("Embedding-Dimension stimmt nicht mit dem Index überein.")
            saved_norm = math.sqrt(sum(value * value for value in saved))
            similarity = (
                sum(a * b for a, b in zip(saved, vector)) / (norm * saved_norm)
                if saved_norm else -1.0
            )
            source = row["source"]
            if source not in best_similarity or similarity > best_similarity[source]:
                best_similarity[source] = similarity
        ranked = sorted(
            best_similarity.items(),
            key=lambda item: (-item[1], item[0]),
        )[:limit]
        return [
            {"path": source, "score": round(score, 5), "next_tool": "knowledge_read"}
            for source, score in ranked
        ]


async def build_index(config: VectorKnowledgeConfig, *, root: Path, path: Path, network: NetworkConfig,
                      credential_rules: tuple[Any, ...] = (), okf_max_read_bytes: int = MAX_SOURCE_BYTES) -> int:
    provider = EmbeddingProvider(config, network, credential_rules)
    if config.mode != "okf":
        raise ValueError("Nur OKF-basierte Vektorsuche wird unterstützt.")
    paths = _okf_paths(root, max_files=config.max_files, max_read_bytes=okf_max_read_bytes)
    # Persist each completed batch directly to a *temporary* database.
    # The live index is replaced only after every embedding request succeeds.
    import tempfile
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="cli-agent-knowledge-", dir=path.parent) as temp:
        new_path = Path(temp) / "index.sqlite"
        store = SQLiteKnowledgeStore(new_path, readonly=False)
        indexed = 0
        dim: int | None = None
        try:
            store.create()
            for relative in paths:
                content = _read_source(root, relative, mode=config.mode, max_read_bytes=okf_max_read_bytes)
                source_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
                store.db.execute(
                    "INSERT INTO sources(path,sha256) VALUES (?,?)",
                    (relative, source_sha256),
                )
                # Never accumulate embeddings from multiple source documents.
                split = _chunks(content)
                for pos in range(0, len(split), MAX_BATCH):
                    for vector in await provider.embed(split[pos:pos + MAX_BATCH]):
                        if dim is None:
                            dim = len(vector)
                        elif len(vector) != dim:
                            raise ValueError("Inkonsistente Embedding-Dimensionen.")
                        store.db.execute(
                            "INSERT INTO chunks(source,vector) VALUES (?,?)",
                            (relative, _encode(vector)),
                        )
                indexed += 1
            if dim is None:
                raise ValueError("Knowledge-Quelle enthält keine indexierbaren Texte.")
            store.configure(
                root=root, mode=config.mode, provider=config.provider,
                model=config.model, endpoint=provider.endpoint_identity, dim=dim,
                max_read_bytes=okf_max_read_bytes,
            )
            store.db.commit()
        finally:
            store.close()
        for suffix in ("-wal", "-shm"):
            if Path(str(new_path) + suffix).exists():
                raise RuntimeError("Index-Datenbank besitzt noch aktive WAL-Dateien.")
        os.replace(new_path, path)
    return indexed



async def update_index(
    config: VectorKnowledgeConfig,
    *,
    root: Path,
    path: Path,
    network: NetworkConfig,
    credential_rules: tuple[Any, ...] = (),
    okf_max_read_bytes: int = MAX_SOURCE_BYTES,
) -> IndexUpdateResult:
    """Atomically update a schema-v3 index, embedding only changed documents.

    Unchanged chunks are copied as raw binary vectors from the existing index.
    New and changed Concepts are embedded against their current source text.
    Removed/unreachable Concepts are left out of the replacement index.
    """
    if not path.is_file():
        raise ValueError(
            "Es existiert noch kein SQLite-Vektorindex. "
            "Zuerst cli-agent-rag init-vector-db ausführen."
        )

    # Validate endpoint identity even for a no-change run. This prevents mixing
    # stored vectors and query vectors from different embedding deployments.
    provider = EmbeddingProvider(config, network, credential_rules)
    old = SQLiteKnowledgeStore(path)
    try:
        dimension = old.check(
            root=root, mode=config.mode, provider=config.provider,
            model=config.model, endpoint=provider.endpoint_identity,
            max_read_bytes=okf_max_read_bytes,
        )
        # Check both the configuration and structural validity of the old index
        # before using it as the source for unchanged embeddings.
        existing = {
            row["path"]: row["sha256"]
            for row in old.db.execute("SELECT path,sha256 FROM sources")
        }
        if config.mode != "okf":
            raise ValueError("Nur OKF-basierte Vektorsuche wird unterstützt.")
        paths = _okf_paths(root, max_files=config.max_files, max_read_bytes=okf_max_read_bytes)

        # Only retain identifiers and checksums for changed files. Holding
        # pre-split contents for the whole corpus can exhaust memory.
        changed_records: list[tuple[str, str]] = []
        unchanged_records: list[tuple[str, str]] = []
        for relative in paths:
            content = _read_source(root, relative, mode=config.mode, max_read_bytes=okf_max_read_bytes)
            fingerprint = hashlib.sha256(content.encode("utf-8")).hexdigest()
            if existing.get(relative) == fingerprint:
                unchanged_records.append((relative, fingerprint))
            else:
                changed_records.append((relative, fingerprint))

        added = sum(1 for relative, _ in changed_records if relative not in existing)
        changed = len(changed_records) - added
        removed = len(set(existing).difference(paths))
        stats = IndexUpdateResult(
            added=added,
            changed=changed,
            unchanged=len(unchanged_records),
            removed=removed,
        )
        if not changed_records and not removed:
            # No embeddings are requested and the database is not rewritten.
            return stats

        # Construct and validate the complete new DB before publication. If an
        # embedding request fails, the currently published index is untouched.
        path.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(
            prefix="cli-agent-knowledge-", dir=path.parent
        ) as temp:
            new_path = Path(temp) / "index.sqlite"
            new_store = SQLiteKnowledgeStore(new_path, readonly=False)
            try:
                new_store.create()
                new_store.configure(
                    root=root, mode=config.mode, provider=config.provider,
                    model=config.model, endpoint=provider.endpoint_identity,
                    dim=dimension, max_read_bytes=okf_max_read_bytes,
                )
                for relative, fingerprint in unchanged_records:
                    new_store.copy_unchanged_source(
                        old, path=relative, sha256=fingerprint, dimension=dimension
                    )
                for relative, fingerprint in changed_records:
                    # Re-read one source at a time; nothing but its fingerprint
                    # was retained from the initial change-detection pass.
                    content = _read_source(root, relative, mode=config.mode, max_read_bytes=okf_max_read_bytes)
                    if hashlib.sha256(content.encode("utf-8")).hexdigest() != fingerprint:
                        raise ValueError(
                            "Knowledge-Quelle wurde während der Indexierung "
                            "geändert; bitte den Update-Lauf wiederholen."
                        )
                    new_store.db.execute(
                        "INSERT INTO sources(path,sha256) VALUES (?,?)",
                        (relative, fingerprint),
                    )
                    chunks = _chunks(content)
                    for pos in range(0, len(chunks), MAX_BATCH):
                        batch = await provider.embed(chunks[pos:pos + MAX_BATCH])
                        for vector in batch:
                            if len(vector) != dimension:
                                raise ValueError(
                                    "Embedding-Dimension hat sich geändert; "
                                    "bitte mit init-vector-db neu indexieren."
                                )
                            new_store.db.execute(
                                "INSERT INTO chunks(source,vector) VALUES (?,?)",
                                (relative, _encode(vector)),
                            )
                new_store.db.commit()
            finally:
                new_store.close()

            # WAL sidecars must not survive publication of the new database.
            for suffix in ("-wal", "-shm"):
                if Path(str(new_path) + suffix).exists():
                    raise RuntimeError("Aktualisierter Index enthält aktive WAL-Dateien.")

            # Release the old reader before replacing the file (Windows).
            old.close()
            os.replace(new_path, path)
        return stats
    finally:
        # Closing twice is harmless for sqlite3, and also handles no-op/error.
        old.close()


class VectorKnowledgeSession:
    """Read-only tool adapter used exclusively inside the Knowledge phase."""

    def __init__(self, config: VectorKnowledgeConfig, *, root: Path, path: Path, network: NetworkConfig,
                 credential_rules: tuple[Any, ...] = (), okf_max_read_bytes: int = MAX_SOURCE_BYTES) -> None:
        if config.mode != "okf":
            raise ValueError("Nur OKF-basierte Vektorsuche wird unterstützt.")
        self.config = config
        self.root = root
        self.okf_max_read_bytes = okf_max_read_bytes
        self.embedder = EmbeddingProvider(config, network, credential_rules)
        self.store = SQLiteKnowledgeStore(path)
        try:
            self.dimension = self.store.check(
                root=root, mode=config.mode, provider=config.provider,
                model=config.model, endpoint=self.embedder.endpoint_identity,
                max_read_bytes=okf_max_read_bytes,
            )
        except Exception:
            self.store.close()
            raise

    def close(self) -> None:
        self.store.close()

    async def search(self, query: str) -> dict[str, Any]:
        if not isinstance(query, str) or not query.strip() or len(query) > MAX_SEARCH_QUERY:
            raise ValueError("Knowledge-Suchanfrage ist leer oder zu lang.")
        vector = (await self.embedder.embed([query]))[0]
        if len(vector) != self.dimension:
            raise ValueError("Embedding-Modell passt nicht zur Indexdimension.")
        # Bound expensive source re-reads even if a large part of the index is
        # stale. Searching all stored vectors is the current SQLite backend's
        # expected cost; validating all source files is not.
        candidate_limit = min(
            MAX_STALE_CANDIDATES, max(10, self.config.search_limit * 5)
        )
        candidates = self.store.search(vector, limit=candidate_limit)
        # Hide stale/deleted documents and confirm the selected root again.
        hits = []
        for item in candidates:
            try:
                current = _read_source(self.root, item["path"], mode=self.config.mode, max_read_bytes=self.okf_max_read_bytes)
            except (OSError, ValueError):
                continue
            if hashlib.sha256(current.encode("utf-8")).hexdigest() == self.store.source_sha(item["path"]):
                hits.append(item)
                if len(hits) >= self.config.search_limit:
                    break
        return {"query": query, "entries": hits}

    async def call_tool(self, name: str, arguments: dict[str, Any]):
        try:
            if name == "knowledge_search":
                data = await self.search(arguments["query"])
            else:
                raise ValueError("Nicht unterstütztes Knowledge-Tool.")
            return SimpleNamespace(structuredContent=data, content=[], isError=False)
        except (ValueError, OSError, KeyError) as exc:
            return SimpleNamespace(structuredContent={"error": str(exc)}, content=[], isError=True)
