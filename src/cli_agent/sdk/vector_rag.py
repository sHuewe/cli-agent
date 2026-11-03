"""Standalone file-directory vector RAG for Python applications.

The index and search functions do not instantiate an LLM agent or alter its
history. They reuse the existing bounded SQLite embedding implementation.
"""
from __future__ import annotations

import hashlib
from uuid import uuid4
from dataclasses import dataclass
from pathlib import Path

from cli_agent.context.knowledge_vector import (
    MAX_SEARCH_QUERY,
    MAX_SOURCE_BYTES,
    MAX_BATCH,
    _chunks,
    _encode,
    EmbeddingProvider,
    SQLiteKnowledgeStore,
    _read_source,
    build_index,
    update_index,
)
from cli_agent.security.network_policy import NetworkConfig
from cli_agent.settings.admin_config import AdminConfig
from cli_agent.settings.config import VectorKnowledgeConfig


@dataclass(frozen=True)
class VectorRagHit:
    path: str
    score: float
    preview: str
    source: str | None = None
    record_id: str | None = None


@dataclass(frozen=True)
class VectorRagBuildResult:
    rag: VectorRag
    total: int
    added: int
    changed: int
    unchanged: int
    removed: int


@dataclass(frozen=True)
class VectorRag:
    """Serializable index description, not an open SQLite connection."""

    path: Path
    database: Path
    config: VectorKnowledgeConfig
    admin_config: AdminConfig
    max_read_bytes: int

    def _embedder(self) -> EmbeddingProvider:
        return EmbeddingProvider(
            self.config, self.admin_config.network, self.admin_config.model_credentials,
        )

    def _validate_store(self, store: SQLiteKnowledgeStore, embedder: EmbeddingProvider) -> int:
        return store.check(
            root=self.path, mode="files", provider=self.config.provider,
            model=self.config.model, endpoint=embedder.endpoint_identity,
            max_read_bytes=self.max_read_bytes,
        )

    async def add_record(
        self,
        content: str,
        *,
        id: str | None = None,
        source: str | None = None,
    ) -> str:
        """Atomically upsert an inline record; return its stable ID.

        Only the affected record is embedded. Source is optional provenance
        metadata, independent from the record's identity.
        """
        if not isinstance(content, str) or not content.strip():
            raise ValueError("content must be a nonempty string")
        if len(content.encode("utf-8")) > self.max_read_bytes:
            raise ValueError("content exceeds the configured byte limit")
        if id is None:
            id = str(uuid4())
        if not isinstance(id, str) or not 1 <= len(id) <= 200 or id != id.strip() or any(
            ord(ch) < 32 or ord(ch) == 127 for ch in id
        ):
            raise ValueError("id must be a nonempty ID of at most 200 characters")
        if source is not None and (
            not isinstance(source, str) or not 1 <= len(source) <= 2000
            or not source.strip() or any(ord(ch) < 32 or ord(ch) == 127 for ch in source)
        ):
            raise ValueError("source must be a nonempty string of at most 2000 characters")
        key = f"record://{id}"
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        embedder = self._embedder()

        # Read the current index without mutating it while the embedding
        # operation runs. Do not re-embed on an identical-text upsert.
        old_store = SQLiteKnowledgeStore(self.database)
        try:
            old_dimension = self._validate_store(old_store, embedder)
            existing = old_store.sdk_record(id)
            unchanged = existing is not None and existing["content"] == content
        finally:
            old_store.close()

        vectors: list[list[float]] = []
        if not unchanged:
            parts = _chunks(content)
            for position in range(0, len(parts), MAX_BATCH):
                batch = await embedder.embed(parts[position:position + MAX_BATCH])
                for vector in batch:
                    if old_dimension and len(vector) != old_dimension:
                        raise ValueError("Embedding dimensions differ from the stored index")
                    vectors.append(vector)

        store = SQLiteKnowledgeStore(self.database, readonly=False)
        try:
            with store.db:
                # SQLite serializes writers; re-check state after acquiring
                # the write lock to avoid silently replacing concurrent edits.
                store.db.execute("BEGIN IMMEDIATE")
                dimension = self._validate_store(store, embedder)
                store.create_sdk_record_table()
                current = store.sdk_record(id)
                if unchanged:
                    if current is None or current["content"] != content:
                        raise RuntimeError("Record changed during embedding; retry the operation")
                    if current["source"] != source:
                        store.db.execute(
                            "UPDATE sdk_records SET source=? WHERE id=?", (source, id)
                        )
                else:
                    if (current is None) != (existing is None) or (
                        current is not None and existing is not None
                        and (current["content"] != existing["content"] or current["source"] != existing["source"])
                    ):
                        raise RuntimeError("Record changed during embedding; retry the operation")
                    if dimension != old_dimension:
                        raise RuntimeError("Index dimensions changed during embedding")
                    if not vectors:
                        raise ValueError("content did not produce embeddings")
                    if dimension == 0:
                        dimension = len(vectors[0])
                        store.configure(
                            root=self.path, mode="files", provider=self.config.provider,
                            model=self.config.model, endpoint=embedder.endpoint_identity,
                            dim=dimension, max_read_bytes=self.max_read_bytes,
                        )
                    store.db.execute("DELETE FROM chunks WHERE source=?", (key,))
                    store.db.execute("DELETE FROM sources WHERE path=?", (key,))
                    store.db.execute(
                        "INSERT INTO sources(path,sha256) VALUES (?,?)", (key, digest)
                    )
                    for vector in vectors:
                        store.db.execute(
                            "INSERT INTO chunks(source,vector) VALUES (?,?)", (key, _encode(vector))
                        )
                    store.db.execute(
                        "INSERT INTO sdk_records(id,content,source) VALUES (?,?,?) "
                        "ON CONFLICT(id) DO UPDATE SET content=excluded.content,source=excluded.source",
                        (id, content, source),
                    )
            return id
        finally:
            store.close()

    async def search(self, query: str, *, limit: int = 5, preview_chars: int = 500) -> tuple[VectorRagHit, ...]:
        """Search and verify source fingerprints before returning snippets."""
        if not isinstance(query, str) or not query.strip() or len(query) > MAX_SEARCH_QUERY:
            raise ValueError("Query must contain 1-2000 characters")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 20:
            raise ValueError("limit must be between 1 and 20")
        if not isinstance(preview_chars, int) or isinstance(preview_chars, bool) or not 0 <= preview_chars <= 2000:
            raise ValueError("preview_chars must be between 0 and 2000")

        embedder = self._embedder()
        store = SQLiteKnowledgeStore(self.database)
        try:
            dimension = self._validate_store(store, embedder)
            if dimension == 0:
                return ()
            vector = (await embedder.embed([query]))[0]
            if len(vector) != dimension:
                raise ValueError("Embedding dimensions differ from the stored index")
            hits = []
            # Inspect a bounded number of candidates to omit stale sources.
            for candidate in store.search(vector, limit=min(100, max(10, limit * 5))):
                path = candidate["path"]
                record_id: str | None = None
                if path.startswith("record://"):
                    record_id = path[len("record://"):]
                    record = store.sdk_record(record_id)
                    if record is None:
                        continue
                    content = record["content"]
                    origin = record["source"]
                else:
                    try:
                        content = _read_source(
                            self.path, path, mode="files",
                            max_read_bytes=self.max_read_bytes,
                        )
                    except (OSError, ValueError, UnicodeError):
                        continue
                    origin = path
                checksum = hashlib.sha256(content.encode("utf-8")).hexdigest()
                if checksum != store.source_sha(path):
                    continue
                hits.append(VectorRagHit(
                    path=path, score=candidate["score"],
                    preview=content[:preview_chars],
                    source=origin, record_id=record_id,
                ))
                if len(hits) >= limit:
                    break
            return tuple(hits)
        finally:
            store.close()


def open_vector_rag(
    database: str | Path,
    *,
    api_key_env: str | None = None,
    admin_config: AdminConfig | None = None,
) -> VectorRag:
    """Open an existing standalone SQLite index without rebuilding it.

    The database stores the embedding endpoint/model and source root, but
    never API credentials. For external endpoints, callers must provide
    an explicitly authorized credential environment variable and policy.
    """
    target = Path(database).expanduser().resolve()
    if not target.is_file():
        raise FileNotFoundError(f"Vector database not found: {target}")
    policy = admin_config if admin_config is not None else AdminConfig()
    store = SQLiteKnowledgeStore(target)
    try:
        meta = store.metadata()
        required = {
            "root", "mode", "provider", "model", "endpoint", "dimension",
            "okf_max_read_bytes", "schema",
        }
        if not required.issubset(meta) or meta["mode"] != "files" or meta["schema"] != "4":
            raise ValueError("Not a compatible standalone file-vector index")
        root = Path(meta["root"]).resolve()
        if not root.is_dir() or target.is_relative_to(root):
            raise ValueError("Invalid or missing vector source directory")
        limit = int(meta["okf_max_read_bytes"])
        if not 1 <= limit <= MAX_SOURCE_BYTES:
            raise ValueError("Invalid stored source read limit")
        if meta["provider"] not in {"ollama", "openai"}:
            raise ValueError("Unsupported stored embedding provider")
        if meta["provider"] == "ollama" and api_key_env:
            raise ValueError("api_key_env is only supported for OpenAI-compatible providers")
        config = VectorKnowledgeConfig(
            mode="files", database=str(target), provider=meta["provider"],
            model=meta["model"], base_url=meta["endpoint"], api_key_env=api_key_env,
        )
        rag = VectorRag(root, target, config, policy, limit)
        rag._validate_store(store, rag._embedder())
        return rag
    finally:
        store.close()


async def build_vector_rag(
    path: str | Path,
    *,
    database: str | Path | None = None,
    provider: str = "ollama",
    model: str = "embeddinggemma",
    base_url: str | None = None,
    api_key_env: str | None = None,
    max_files: int = 10_000,
    max_read_bytes: int = MAX_SOURCE_BYTES,
    incremental: bool = False,
    admin_config: AdminConfig | None = None,
) -> VectorRagBuildResult:
    """Build or update a vector index of ordinary .txt/.md files.

    Files are recursively discovered, verified against symlinks/hardlinks,
    chunked, embedded and persisted atomically in SQLite. Only source
    identifiers, fingerprints and vectors are stored, not document text.
    """
    root = Path(path).expanduser().resolve()
    if not root.is_dir():
        raise ValueError(f"Source directory does not exist: {root}")
    target = (
        Path(database).expanduser().resolve()
        if database is not None
        else root.parent / (root.name + ".vectors.sqlite")
    )
    if target.is_relative_to(root):
        raise ValueError("Vector database must be outside the source directory")
    if not isinstance(max_files, int) or isinstance(max_files, bool) or not 1 <= max_files <= 100_000:
        raise ValueError("max_files must be between 1 and 100000")
    if not isinstance(max_read_bytes, int) or isinstance(max_read_bytes, bool) or not 1 <= max_read_bytes <= MAX_SOURCE_BYTES:
        raise ValueError("max_read_bytes is outside supported range")
    if provider not in {"ollama", "openai"}:
        raise ValueError("provider must be ollama or openai")
    if not isinstance(model, str) or not model.strip():
        raise ValueError("model cannot be empty")
    if provider == "openai" and not base_url:
        raise ValueError("OpenAI-compatible provider requires base_url")
    if provider == "ollama" and api_key_env:
        raise ValueError("api_key_env is only supported for OpenAI-compatible providers")
    settings = VectorKnowledgeConfig(
        mode="files", database=str(target), provider=provider,
        model=model.strip(),
        base_url=base_url or "http://localhost:11434",
        api_key_env=api_key_env, max_files=max_files,
    )
    policy = admin_config if admin_config is not None else AdminConfig()
    kwargs = {
        "root": root,
        "path": target,
        "network": policy.network,
        "credential_rules": policy.model_credentials,
        "okf_max_read_bytes": max_read_bytes,
    }
    if incremental:
        changes = await update_index(settings, **kwargs)
        total, added, changed, unchanged, removed = (
            changes.total, changes.added, changes.changed,
            changes.unchanged, changes.removed,
        )
    else:
        total = await build_index(settings, **kwargs)
        added, changed, unchanged, removed = total, 0, 0, 0

    return VectorRagBuildResult(
        rag=VectorRag(root, target, settings, policy, max_read_bytes),
        total=total, added=added, changed=changed,
        unchanged=unchanged, removed=removed,
    )
