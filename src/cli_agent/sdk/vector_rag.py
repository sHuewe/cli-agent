"""Standalone file-directory vector RAG for Python applications.

The index and search functions do not instantiate an LLM agent or alter its
history. They reuse the existing bounded SQLite embedding implementation.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from pathlib import Path

from cli_agent.context.knowledge_vector import (
    MAX_SEARCH_QUERY,
    MAX_SOURCE_BYTES,
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

    async def search(self, query: str, *, limit: int = 5, preview_chars: int = 500) -> tuple[VectorRagHit, ...]:
        """Search and verify source fingerprints before returning snippets."""
        if not isinstance(query, str) or not query.strip() or len(query) > MAX_SEARCH_QUERY:
            raise ValueError("Query must contain 1-2000 characters")
        if not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 20:
            raise ValueError("limit must be between 1 and 20")
        if not isinstance(preview_chars, int) or isinstance(preview_chars, bool) or not 0 <= preview_chars <= 2000:
            raise ValueError("preview_chars must be between 0 and 2000")

        embedder = EmbeddingProvider(
            self.config, self.admin_config.network, self.admin_config.model_credentials,
        )
        store = SQLiteKnowledgeStore(self.database)
        try:
            dimension = store.check(
                root=self.path, mode="files", provider=self.config.provider,
                model=self.config.model, endpoint=embedder.endpoint_identity,
                max_read_bytes=self.max_read_bytes,
            )
            vector = (await embedder.embed([query]))[0]
            if len(vector) != dimension:
                raise ValueError("Embedding dimensions differ from the stored index")
            hits = []
            # Inspect a bounded number of candidates to omit stale sources.
            for candidate in store.search(vector, limit=min(100, max(10, limit * 5))):
                try:
                    content = _read_source(
                        self.path, candidate["path"], mode="files",
                        max_read_bytes=self.max_read_bytes,
                    )
                except (OSError, ValueError, UnicodeError):
                    continue
                checksum = hashlib.sha256(content.encode("utf-8")).hexdigest()
                if checksum != store.source_sha(candidate["path"]):
                    continue
                hits.append(VectorRagHit(
                    path=candidate["path"],
                    score=candidate["score"],
                    preview=content[:preview_chars],
                ))
                if len(hits) >= limit:
                    break
            return tuple(hits)
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
