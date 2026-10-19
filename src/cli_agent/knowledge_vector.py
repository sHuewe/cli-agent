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
    if config.mode == "okf":
        if okf_root is None:
            raise ValueError("knowledge.vector.mode='okf' erfordert [okf].repository.")
        return okf_root.resolve()
    if not config.source:
        raise ValueError("knowledge.vector.source ist im raw-Modus erforderlich.")
    root = Path(config.source).expanduser()
    if not root.is_absolute():
        root = (config_file.parent if config_file else Path.cwd()) / root
    root = root.resolve()
    if not root.is_dir():
        raise ValueError(f"Knowledge-Quellordner existiert nicht: {root}")
    return root


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
    if resolved.stat().st_size > MAX_SOURCE_BYTES:
        raise ValueError("Knowledge-Datei überschreitet das Größenlimit.")
    return resolved


def _read_source(root: Path, relative: str, *, mode: str) -> str:
    path = _source_file(root, relative)
    if mode == "okf":
        from .okf_mcp_server.repository import OkfRepository
        repository = OkfRepository.from_directory(root, max_read_bytes=MAX_SOURCE_BYTES)
        result = repository.knowledge_read(relative)
        if result["kind"] != "concept":
            raise ValueError("Nur OKF-Concepts können indexiert werden.")
        return result["content"]
    if path.suffix.lower() in {".md", ".txt"}:
        return path.read_text(encoding="utf-8")
    if path.suffix.lower() == ".pdf":
        from pypdf import PdfReader
        reader = PdfReader(str(path), strict=False)
        if len(reader.pages) > 100:
            raise ValueError("PDF überschreitet das Seitenlimit (100).")
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    raise ValueError("Nicht unterstützter Knowledge-Dateityp.")


def _raw_paths(root: Path, *, max_files: int) -> list[str]:
    paths: list[str] = []
    for path in sorted(root.rglob("*")):
        if path.is_symlink():
            continue
        if path.is_file() and path.suffix.lower() in {".md", ".txt", ".pdf"}:
            relative = path.relative_to(root).as_posix()
            try:
                _source_file(root, relative)
            except (OSError, ValueError):
                continue
            paths.append(relative)
            if len(paths) > max_files:
                raise ValueError("Zu viele Dateien; max_files überschritten.")
    return paths


def _okf_paths(root: Path, *, max_files: int) -> list[str]:
    from .okf_mcp_server.repository import OkfRepository
    repo = OkfRepository.from_directory(root, max_read_bytes=MAX_SOURCE_BYTES, max_index_entries=2000)
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
    if not vector or len(vector) > 8192 or any(not isinstance(v, (int, float)) or not math.isfinite(v) for v in vector):
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
            vectors = [item.get("embedding") for item in sorted(entries, key=lambda x: x["index"])] if isinstance(entries, list) else None
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
            self.db = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
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

    def configure(self, *, root: Path, mode: str, provider: str, model: str, endpoint: str, dim: int) -> None:
        values = {"root": str(root), "mode": mode, "provider": provider, "model": model,
                  "endpoint": endpoint, "dimension": str(dim), "schema": "3"}
        self.db.executemany("INSERT OR REPLACE INTO metadata(key,value) VALUES (?,?)", values.items())

    def check(self, *, root: Path, mode: str, provider: str, model: str, endpoint: str) -> int:
        meta = self.metadata()
        expected = {"root": str(root), "mode": mode, "provider": provider, "model": model,
                    "endpoint": endpoint, "schema": "3"}
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


async def build_index(config: VectorKnowledgeConfig, *, root: Path, path: Path, network: NetworkConfig, credential_rules: tuple[Any, ...] = ()) -> int:
    provider = EmbeddingProvider(config, network, credential_rules)
    paths = _okf_paths(root, max_files=config.max_files) if config.mode == "okf" else _raw_paths(root, max_files=config.max_files)
    # All embedding calls succeed before replacing the on-disk index.
    records: list[tuple[str, str, list[list[float]]]] = []
    dimensions: set[int] = set()
    for relative in paths:
        content = _read_source(root, relative, mode=config.mode)
        source_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
        split = _chunks(content)
        vectors: list[list[float]] = []
        for pos in range(0, len(split), MAX_BATCH):
            batch = await provider.embed(split[pos:pos + MAX_BATCH])
            vectors.extend(batch)
        for vector in vectors:
            dimensions.add(len(vector))
        # Only the fingerprint and vectors cross into the persistence layer.
        records.append((relative, source_sha256, vectors))
    if not dimensions or len(dimensions) != 1:
        raise ValueError("Knowledge-Quelle enthält keine indexierbaren Texte oder inkompatible Embeddings.")
    import tempfile
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="cli-agent-knowledge-", dir=path.parent) as temp:
        new_path = Path(temp) / "index.sqlite"
        store = SQLiteKnowledgeStore(new_path, readonly=False)
        try:
            store.create()
            store.replace_documents(records)
            store.configure(root=root, mode=config.mode, provider=config.provider, model=config.model,
                            endpoint=provider.endpoint_identity, dim=next(iter(dimensions)))
            store.db.commit()
        finally:
            store.close()
        # The writer is closed before atomically replacing the read-only index.
        # Separate WAL sidecars must never belong to the published database.
        for suffix in ("-wal", "-shm"):
            sidecar = Path(str(new_path) + suffix)
            if sidecar.exists():
                raise RuntimeError("Index-Datenbank besitzt noch aktive WAL-Dateien.")
        os.replace(new_path, path)
    return len(records)



async def update_index(
    config: VectorKnowledgeConfig,
    *,
    root: Path,
    path: Path,
    network: NetworkConfig,
    credential_rules: tuple[Any, ...] = (),
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
        )
        # Check both the configuration and structural validity of the old index
        # before using it as the source for unchanged embeddings.
        existing = {
            row["path"]: row["sha256"]
            for row in old.db.execute("SELECT path,sha256 FROM sources")
        }
        paths = (
            _okf_paths(root, max_files=config.max_files)
            if config.mode == "okf"
            else _raw_paths(root, max_files=config.max_files)
        )

        changed_records: list[tuple[str, str, list[str]]] = []
        unchanged_records: list[tuple[str, str]] = []
        for relative in paths:
            content = _read_source(root, relative, mode=config.mode)
            fingerprint = hashlib.sha256(content.encode("utf-8")).hexdigest()
            if existing.get(relative) == fingerprint:
                unchanged_records.append((relative, fingerprint))
            else:
                changed_records.append((relative, fingerprint, _chunks(content)))

        added = sum(1 for relative, _, _ in changed_records if relative not in existing)
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
                    dim=dimension,
                )
                for relative, fingerprint in unchanged_records:
                    new_store.copy_unchanged_source(
                        old, path=relative, sha256=fingerprint, dimension=dimension
                    )
                for relative, fingerprint, chunks in changed_records:
                    new_store.db.execute(
                        "INSERT INTO sources(path,sha256) VALUES (?,?)",
                        (relative, fingerprint),
                    )
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

    def __init__(self, config: VectorKnowledgeConfig, *, root: Path, path: Path, network: NetworkConfig, credential_rules: tuple[Any, ...] = ()) -> None:
        self.config = config
        self.root = root
        self.embedder = EmbeddingProvider(config, network, credential_rules)
        self.store = SQLiteKnowledgeStore(path)
        try:
            self.dimension = self.store.check(
                root=root, mode=config.mode, provider=config.provider,
                model=config.model, endpoint=self.embedder.endpoint_identity,
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
                current = _read_source(self.root, item["path"], mode=self.config.mode)
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
            elif name == "knowledge_read" and self.config.mode == "raw":
                path = arguments["path"]
                if not isinstance(path, str) or self.store.source_sha(path) is None:
                    raise ValueError("Dokument nicht im Knowledge-Index.")
                content = _read_source(self.root, path, mode="raw")
                if hashlib.sha256(content.encode("utf-8")).hexdigest() != self.store.source_sha(path):
                    raise ValueError("Quelle wurde geändert; bitte neu indexieren.")
                data = {"path": path, "kind": "concept", "content": content, "internal_links": []}
            else:
                raise ValueError("Nicht unterstütztes Knowledge-Tool.")
            return SimpleNamespace(structuredContent=data, content=[], isError=False)
        except (ValueError, OSError, KeyError) as exc:
            return SimpleNamespace(structuredContent={"error": str(exc)}, content=[], isError=True)
