"""Optional offline-indexed vector / lexical retrieval, without extra dependencies.

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
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx

from .config import VectorKnowledgeConfig
from .filesystem_security import regular_file_has_multiple_links
from .model_factory import _validate_api_key_env
from .config import ModelConfig
from .network_policy import NetworkConfig, validate_http_url

MAX_SOURCE_BYTES = 2_560_000
MAX_SEARCH_QUERY = 2000
MAX_SEARCH_RESULTS = 20
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


class EmbeddingProvider:
    def __init__(self, config: VectorKnowledgeConfig, network: NetworkConfig, credential_rules: tuple[Any, ...] = ()) -> None:
        self.config = config
        self.base_url = validate_http_url(config.base_url, allowed_hosts=network.model_allowed_hosts, purpose="Embedding-Modell").rstrip("/")
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
                path TEXT PRIMARY KEY, sha256 TEXT NOT NULL, content TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS chunks (
                id INTEGER PRIMARY KEY, source TEXT NOT NULL,
                body TEXT NOT NULL, vector BLOB NOT NULL,
                FOREIGN KEY(source) REFERENCES sources(path)
            );
            CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(body);
        """)

    def metadata(self) -> dict[str, str]:
        return {r["key"]: r["value"] for r in self.db.execute("SELECT key,value FROM metadata")}

    def configure(self, *, root: Path, mode: str, provider: str, model: str, dim: int) -> None:
        values = {"root": str(root), "mode": mode, "provider": provider, "model": model, "dimension": str(dim), "schema": "1"}
        self.db.executemany("INSERT OR REPLACE INTO metadata(key,value) VALUES (?,?)", values.items())

    def check(self, *, root: Path, mode: str, provider: str, model: str) -> int:
        meta = self.metadata()
        expected = {"root": str(root), "mode": mode, "provider": provider, "model": model, "schema": "1"}
        if any(meta.get(k) != v for k, v in expected.items()):
            raise ValueError("Knowledge-Index passt nicht zu Quelle/Modell; neu indexieren.")
        dim = int(meta["dimension"])
        if not 1 <= dim <= 8192:
            raise ValueError("Ungültige Vektordimension im Index.")
        return dim

    def replace_documents(self, records: list[tuple[str, str, list[tuple[str, list[float]]]]]) -> None:
        with self.db:
            self.db.execute("DELETE FROM chunks_fts")
            self.db.execute("DELETE FROM chunks")
            self.db.execute("DELETE FROM sources")
            for path, content, chunks in records:
                sha = hashlib.sha256(content.encode("utf-8")).hexdigest()
                self.db.execute("INSERT INTO sources VALUES (?,?,?)", (path, sha, content))
                for text, vector in chunks:
                    cursor = self.db.execute("INSERT INTO chunks(source,body,vector) VALUES(?,?,?)", (path, text, _encode(vector)))
                    self.db.execute("INSERT INTO chunks_fts(rowid,body) VALUES (?,?)", (cursor.lastrowid, text))

    def source_sha(self, path: str) -> str | None:
        row = self.db.execute("SELECT sha256 FROM sources WHERE path=?", (path,)).fetchone()
        return row["sha256"] if row is not None else None

    def search(self, vector: list[float], query: str, *, limit: int = 5) -> list[dict[str, Any]]:
        if not 1 <= limit <= MAX_SEARCH_RESULTS or not query.strip() or len(query) > MAX_SEARCH_QUERY:
            raise ValueError("Ungültige Knowledge-Suchparameter.")
        dim = len(vector)
        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0:
            raise ValueError("Embedding hat Norm 0.")
        lexical: dict[int, int] = {}
        # FTS5 input tokens are safely quoted, not interpreted as FTS expressions.
        import re
        words = re.findall(r"\w+", query, re.UNICODE)[:15]
        if words:
            expression = " OR ".join('"' + token.replace('"', '""') + '"' for token in words)
            try:
                for rank, row in enumerate(self.db.execute(
                    "SELECT rowid FROM chunks_fts WHERE chunks_fts MATCH ? ORDER BY rank LIMIT 50", (expression,)
                )):
                    lexical[row["rowid"]] = rank
            except sqlite3.OperationalError:
                pass
        semantic: list[tuple[float, int, str]] = []
        rows = self.db.execute("SELECT id,source,vector FROM chunks")
        for row in rows:
            saved = _decode(row["vector"])
            if len(saved) != dim:
                raise ValueError("Embedding-Dimension stimmt nicht mit dem Index überein.")
            saved_norm = math.sqrt(sum(v*v for v in saved))
            similarity = sum(a*b for a,b in zip(saved, vector)) / (norm*saved_norm) if saved_norm else -1.0
            semantic.append((similarity, row["id"], row["source"]))
        semantic.sort(key=lambda item: item[0], reverse=True)
        scores: dict[str, float] = {}
        for rank, (_, chunk_id, source) in enumerate(semantic[:max(50, limit * 10)]):
            scores[source] = scores.get(source, 0.0) + 1.0 / (60 + rank + 1)
            if chunk_id in lexical:
                scores[source] += 1.0 / (60 + lexical[chunk_id] + 1)
        return [{"path": path, "score": round(score, 5), "next_tool": "knowledge_read"} for path, score in sorted(scores.items(), key=lambda pair: pair[1], reverse=True)[:limit]]


async def build_index(config: VectorKnowledgeConfig, *, root: Path, path: Path, network: NetworkConfig, credential_rules: tuple[Any, ...] = ()) -> int:
    provider = EmbeddingProvider(config, network, credential_rules)
    paths = _okf_paths(root, max_files=config.max_files) if config.mode == "okf" else _raw_paths(root, max_files=config.max_files)
    # All embedding calls succeed before replacing the on-disk index.
    records = []
    dimensions: set[int] = set()
    for relative in paths:
        content = _read_source(root, relative, mode=config.mode)
        split = _chunks(content)
        vectors: list[list[float]] = []
        for pos in range(0, len(split), MAX_BATCH):
            batch = await provider.embed(split[pos:pos + MAX_BATCH])
            vectors.extend(batch)
        for vector in vectors:
            dimensions.add(len(vector))
        records.append((relative, content, list(zip(split, vectors))))
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
            store.configure(root=root, mode=config.mode, provider=config.provider, model=config.model, dim=next(iter(dimensions)))
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


class VectorKnowledgeSession:
    """Read-only tool adapter used exclusively inside the Knowledge phase."""

    def __init__(self, config: VectorKnowledgeConfig, *, root: Path, path: Path, network: NetworkConfig, credential_rules: tuple[Any, ...] = ()) -> None:
        self.config = config
        self.root = root
        self.store = SQLiteKnowledgeStore(path)
        self.dimension = self.store.check(root=root, mode=config.mode, provider=config.provider, model=config.model)
        self.embedder = EmbeddingProvider(config, network, credential_rules)

    def close(self) -> None:
        self.store.close()

    async def search(self, query: str) -> dict[str, Any]:
        if not isinstance(query, str) or not query.strip() or len(query) > MAX_SEARCH_QUERY:
            raise ValueError("Knowledge-Suchanfrage ist leer oder zu lang.")
        vector = (await self.embedder.embed([query]))[0]
        if len(vector) != self.dimension:
            raise ValueError("Embedding-Modell passt nicht zur Indexdimension.")
        candidates = self.store.search(vector, query, limit=self.config.search_limit)
        # Hide stale/deleted documents and confirm the selected root again.
        hits = []
        for item in candidates:
            try:
                current = _read_source(self.root, item["path"], mode=self.config.mode)
            except (OSError, ValueError):
                continue
            if hashlib.sha256(current.encode("utf-8")).hexdigest() == self.store.source_sha(item["path"]):
                hits.append(item)
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
