"""Read-only retrieval from externally prepared Chroma collections.

Document extraction, chunking, vector insertion, update, and deletion are
intentionally outside cli-agent. The SDK only embeds search queries using the
existing administrator-controlled embedding provider and asks Chroma for
stored chunk documents and provenance metadata.
"""
from __future__ import annotations

import asyncio
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from cli_agent.context.knowledge_vector import (
    MAX_SEARCH_QUERY,
    EmbeddingProvider,
    _encode,
)
from cli_agent.settings.admin_config import AdminConfig
from cli_agent.settings.config import VectorKnowledgeConfig

# Metadata contract for externally managed Chroma collections. These fields
# describe the query embedding space, not a credential or API key.
PROVIDER_META = "cli_agent_embedding_provider"
MODEL_META = "cli_agent_embedding_model"
ENDPOINT_META = "cli_agent_embedding_endpoint"


@dataclass(frozen=True)
class VectorRagHit:
    """A matched chunk, not the first characters of its parent document."""

    id: str
    content: str
    source: str | None
    metadata: dict[str, Any]
    distance: float

    @property
    def record_id(self) -> str:
        """Convenience alias for Chroma's record ID."""
        return self.id


@dataclass(frozen=True)
class VectorRag:
    """Read-only SDK handle to an existing Chroma collection.

    External producers own all writes and must publish a stable collection
    while cli-agent is searching. PersistentClient is not a file-level
    read-only connection; only the public SDK operations are read-only.
    """

    database: Path
    collection: str
    config: VectorKnowledgeConfig = field(repr=False)
    admin_config: AdminConfig = field(repr=False)
    _client: Any = field(repr=False, compare=False)
    _collection: Any = field(repr=False, compare=False)

    async def search(
        self,
        query: str,
        *,
        limit: int = 5,
        where: dict[str, Any] | None = None,
    ) -> tuple[VectorRagHit, ...]:
        """Return ranked Chroma chunks with their full stored texts.

        Distances come directly from Chroma (smaller is better) and are
        intentionally not transformed into misleading universal scores.
        """
        if not isinstance(query, str) or not query.strip() or len(query) > MAX_SEARCH_QUERY:
            raise ValueError("query must contain 1-2000 characters")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 20:
            raise ValueError("limit must be between 1 and 20")
        if where is not None and not isinstance(where, dict):
            raise TypeError("where must be a Chroma metadata filter dictionary")

        embedder = EmbeddingProvider(
            self.config, self.admin_config.network, self.admin_config.model_credentials,
        )
        vectors = await embedder.embed([query])
        if len(vectors) != 1:
            raise RuntimeError("Embedding provider returned an invalid query batch")
        vector = vectors[0]
        _encode(vector)  # Reject non-finite, empty or unbounded vectors.

        kwargs: dict[str, Any] = {
            "query_embeddings": [vector],
            "n_results": limit,
            "include": ["documents", "metadatas", "distances"],
        }
        if where is not None:
            kwargs["where"] = where
        # The local Chroma client is synchronous: never block the async agent
        # event loop while performing its SQLite/HNSW retrieval.
        result = await asyncio.to_thread(self._collection.query, **kwargs)

        ids = _first_query_items(result, "ids")
        documents = _first_query_items(result, "documents")
        metadatas = _first_query_items(result, "metadatas")
        distances = _first_query_items(result, "distances")
        if not (len(ids) == len(documents) == len(metadatas) == len(distances)):
            raise RuntimeError("Chroma returned misaligned search results")

        hits: list[VectorRagHit] = []
        for record_id, document, metadata, distance in zip(ids, documents, metadatas, distances):
            if not isinstance(record_id, str):
                raise RuntimeError("Chroma returned a non-string record ID")
            if isinstance(distance, bool) or not isinstance(distance, (float, int)) or not math.isfinite(distance):
                raise RuntimeError("Chroma returned an invalid vector distance")
            if metadata is None:
                metadata = {}
            if not isinstance(metadata, dict):
                raise RuntimeError("Chroma returned invalid record metadata")
            # A producer may have inserted only embeddings. Such records
            # cannot serve as grounding context and are not returned.
            if not isinstance(document, str) or not document.strip():
                continue
            source = metadata.get("source")
            if not isinstance(source, str) or not source.strip():
                source = None
            hits.append(
                VectorRagHit(
                    id=record_id, content=document, source=source,
                    metadata=dict(metadata), distance=float(distance),
                )
            )
        return tuple(hits)


def _first_query_items(result: Any, key: str) -> list[Any]:
    """Chroma query results are nested by query; SDK always submits one."""
    if not isinstance(result, dict):
        raise RuntimeError("Chroma returned an invalid search response")
    groups = result.get(key)
    if not isinstance(groups, list) or len(groups) != 1 or not isinstance(groups[0], list):
        raise RuntimeError(f"Chroma search response lacks {key!r}")
    return groups[0]


def _metadata_string(metadata: dict[str, Any], name: str) -> str | None:
    value = metadata.get(name)
    if value is None:
        return None
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Chroma collection metadata {name!r} must be a nonempty string")
    return value


def open_vector_rag(
    database: str | Path,
    *,
    collection: str = "knowledge",
    provider: str | None = None,
    model: str | None = None,
    base_url: str | None = None,
    api_key_env: str | None = None,
    admin_config: AdminConfig | None = None,
) -> VectorRag:
    """Open an existing, externally created Chroma PersistentClient collection.

    To avoid accidental use of a different embedding space, provider, model
    and endpoint must come from collection metadata or explicit arguments.
    Explicit values must agree with available metadata. Credentials are
    never taken from the database; configure the key environment explicitly.
    """
    target = Path(database).expanduser().resolve()
    if not target.is_dir():
        raise FileNotFoundError(f"Chroma database directory not found: {target}")
    # Avoid PersistentClient silently initializing an empty store when the
    # caller accidentally points to a new or wrong directory.
    if not (target / "chroma.sqlite3").is_file():
        raise FileNotFoundError(f"No existing Chroma database at: {target}")
    if not isinstance(collection, str) or not collection.strip():
        raise ValueError("collection must be a nonempty name")

    try:
        import chromadb
    except ImportError as exc:
        raise ImportError(
            "Chroma support requires the optional dependency: pip install 'cli-agent[rag]'"
        ) from exc

    # get_collection is deliberately used instead of get_or_create_collection:
    # this SDK never creates a collection or calls any mutation API.
    client = chromadb.PersistentClient(path=str(target))
    existing = client.get_collection(name=collection, embedding_function=None)
    metadata = existing.metadata or {}
    if not isinstance(metadata, dict):
        raise ValueError("Chroma collection metadata must be a dictionary")

    saved_provider = _metadata_string(metadata, PROVIDER_META)
    saved_model = _metadata_string(metadata, MODEL_META)
    saved_endpoint = _metadata_string(metadata, ENDPOINT_META)
    if provider is not None and saved_provider is not None and provider != saved_provider:
        raise ValueError("Embedding provider conflicts with Chroma collection metadata")
    if model is not None and saved_model is not None and model != saved_model:
        raise ValueError("Embedding model conflicts with Chroma collection metadata")

    actual_provider = provider or saved_provider
    actual_model = model or saved_model
    actual_endpoint = base_url or saved_endpoint
    if actual_provider not in {"ollama", "openai"}:
        raise ValueError("Specify provider='ollama' or provider='openai' or persist it in collection metadata")
    if not actual_model or not isinstance(actual_model, str):
        raise ValueError("Embedding model must be provided or persisted in collection metadata")
    if not actual_endpoint or not isinstance(actual_endpoint, str):
        raise ValueError("Embedding endpoint must be provided or persisted in collection metadata")

    policy = admin_config if admin_config is not None else AdminConfig()
    config = VectorKnowledgeConfig(
        mode="files", database=str(target),
        provider=actual_provider, model=actual_model, base_url=actual_endpoint,
        api_key_env=api_key_env,
    )
    # Validates local/network access and approved credential names at open
    # time; no query is sent until search() is called.
    embedder = EmbeddingProvider(config, policy.network, policy.model_credentials)
    if saved_endpoint is not None and embedder.endpoint_identity != _canonical_endpoint(
        saved_endpoint, config, policy,
    ):
        raise ValueError("Embedding endpoint conflicts with Chroma collection metadata")
    return VectorRag(
        database=target, collection=collection, config=config,
        admin_config=policy, _client=client, _collection=existing,
    )


def _canonical_endpoint(
    endpoint: str, config: VectorKnowledgeConfig, policy: AdminConfig,
) -> str:
    """Normalize the saved endpoint using the same security validation."""
    saved_config = VectorKnowledgeConfig(
        mode="files", provider=config.provider, model=config.model,
        base_url=endpoint, api_key_env=config.api_key_env,
    )
    return EmbeddingProvider(
        saved_config, policy.network, policy.model_credentials,
    ).endpoint_identity
