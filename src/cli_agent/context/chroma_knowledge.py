"""Read-only Chroma adapter for the agent's existing Knowledge phase.

The Chroma collection is created and populated by external pipelines.
This adapter never exposes mutation capabilities to the retrieval model.
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from cli_agent.context.knowledge_vector import MAX_SEARCH_QUERY
from cli_agent.settings.admin_config import AdminConfig
from cli_agent.settings.config import VectorKnowledgeConfig

# Bound untrusted data before it can enter the retrieval model's context.
MAX_CHROMA_CHUNK_CHARS = 16_000
MAX_CHROMA_RESULT_CHARS = 90_000


def chroma_search_event_summary(
    result: dict[str, Any], *, collection: str, initial: bool,
) -> dict[str, Any]:
    """Summarize a search for UI telemetry without exposing its source data.

    Never include query text, record IDs, chunk text, metadata or file paths.
    The API boundary will separately validate these fields before emission.
    """
    entries = result.get("entries")
    if not isinstance(entries, list):
        raise ValueError("Chroma search result does not contain a valid match list.")
    return {
        "matches": len(entries),
        "collection": collection,
        "initial": initial,
    }


@dataclass
class ChromaKnowledgeSession:
    config: VectorKnowledgeConfig
    rag: Any

    @classmethod
    def open(cls, config: VectorKnowledgeConfig, *, config_file: Path | None,
             admin_config: AdminConfig) -> "ChromaKnowledgeSession":
        # Lazy import avoids runtime -> sdk package initialization cycles.
        from cli_agent.sdk.vector_rag import open_vector_rag

        target = Path(config.database).expanduser()
        if not target.is_absolute():
            target = (config_file.parent if config_file else Path.cwd()) / target
        rag = open_vector_rag(
            database=target,
            collection=config.collection,
            provider=config.provider,
            model=config.model,
            base_url=config.base_url,
            api_key_env=config.api_key_env,
            admin_config=admin_config,
        )
        return cls(config=config, rag=rag)

    def close(self) -> None:
        # PersistentClient exposes no public close method.
        self.rag = None

    async def search(self, query: str) -> dict[str, Any]:
        if not isinstance(query, str) or not query.strip() or len(query) > MAX_SEARCH_QUERY:
            raise ValueError("Knowledge-Suchanfrage ist leer oder zu lang.")
        if self.rag is None:
            raise RuntimeError("Chroma Knowledge-Session wurde geschlossen.")
        hits = await self.rag.search(query, limit=self.config.search_limit)
        entries: list[dict[str, Any]] = []
        size = 0
        for hit in hits:
            if len(hit.content) > MAX_CHROMA_CHUNK_CHARS:
                raise ValueError("Chroma chunk exceeds the maximum context size.")
            if not isinstance(hit.id, str) or not hit.id or len(hit.id) > 256:
                raise ValueError("Chroma match has an invalid ID.")
            if hit.source is not None and len(hit.source) > 2048:
                raise ValueError("Chroma-Quelle ist zu lang.")
            if len(json.dumps(hit.metadata, ensure_ascii=False)) > 4096:
                raise ValueError("Chroma metadata are too large.")
            size += len(hit.content)
            if size > MAX_CHROMA_RESULT_CHARS:
                raise ValueError("Chroma search result exceeds the maximum context size.")
            entries.append({
                "id": hit.id, "source": hit.source,
                "content": hit.content, "metadata": hit.metadata,
                "distance": hit.distance,
            })
        return {"query": query, "entries": entries}

    async def call_tool(self, name: str, arguments: dict[str, Any]):
        try:
            if name != "knowledge_search":
                raise ValueError("Unsupported Chroma Knowledge tool.")
            return SimpleNamespace(
                structuredContent=await self.search(arguments["query"]),
                content=[], isError=False,
            )
        except (ValueError, RuntimeError, OSError, KeyError, TypeError) as exc:
            return SimpleNamespace(
                structuredContent={"error": str(exc)},
                content=[], isError=True,
            )
