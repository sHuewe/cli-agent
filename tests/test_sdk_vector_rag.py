"""Standalone Chroma retrieval SDK contracts (external writers own all data)."""
from __future__ import annotations

import asyncio
import errno
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from cli_agent.sdk import open_vector_rag, VectorRagHit
from cli_agent.sdk.vector_rag import (
    ENDPOINT_META,
    MODEL_META,
    PROVIDER_META,
)


class FakeEmbeddingProvider:
    calls: list[list[str]] = []

    def __init__(self, config, network, credential_rules=()):
        self.config = config
        self.endpoint_identity = config.base_url.rstrip("/")

    async def embed(self, texts):
        self.calls.append(texts)
        return [[1.0, 0.0] for _ in texts]


class FakeCollection:
    def __init__(self, metadata=None, result=None):
        self.metadata = metadata or {}
        self.results = result if result is not None else {
            "ids": [["chunk-7", "chunk-8"]],
            "documents": [["OAuth2 tokens are used for authentication.", "Tokens expire after 30 minutes."]],
            "metadatas": [[{"source": "handbook.pdf", "page": 17}, {"page": 18}]],
            "distances": [[0.1, 0.25]],
        }
        self.query_calls = []

    def query(self, **kwargs):
        self.query_calls.append(kwargs)
        return self.results


class FakeClient:
    def __init__(self, collection):
        self.collection = collection
        self.read_calls = []

    def get_collection(self, **kwargs):
        self.read_calls.append(kwargs)
        if kwargs["name"] != "knowledge":
            raise ValueError("Collection not found")
        return self.collection


def make_database(tmp_path, monkeypatch, *, metadata=None, result=None):
    database = tmp_path / "external_chroma"
    database.mkdir()
    (database / "chroma.sqlite3").write_bytes(b"external fixture")
    if metadata is None:
        metadata = {
            PROVIDER_META: "ollama",
            MODEL_META: "embeddinggemma",
            ENDPOINT_META: "http://localhost:11434",
        }
    collection = FakeCollection(metadata, result=result)
    client = FakeClient(collection)
    instances = []
    def open_client(**kwargs):
        instances.append(kwargs)
        return client
    monkeypatch.setitem(sys.modules, "chromadb", SimpleNamespace(PersistentClient=open_client))
    monkeypatch.setattr("cli_agent.sdk.vector_rag.EmbeddingProvider", FakeEmbeddingProvider)
    FakeEmbeddingProvider.calls.clear()
    return database, client, collection, instances


@pytest.mark.asyncio
async def test_read_only_search_returns_real_chunks_with_metadata(tmp_path, monkeypatch):
    db, client, collection, instances = make_database(tmp_path, monkeypatch)
    rag = open_vector_rag(db)
    assert rag.database == db.resolve()
    assert instances == [{"path": str(db.resolve())}]
    assert client.read_calls == [{"name": "knowledge", "embedding_function": None}]

    hits = await rag.search("OAuth2 authentication", limit=2, where={"page": 17})
    assert len(hits) == 2
    assert isinstance(hits[0], VectorRagHit)
    assert hits[0].id == "chunk-7"
    assert hits[0].record_id == "chunk-7"
    assert hits[0].content == "OAuth2 tokens are used for authentication."
    assert hits[0].source == "handbook.pdf"
    assert hits[0].metadata == {"source": "handbook.pdf", "page": 17}
    assert hits[0].distance == 0.1
    assert hits[1].source is None
    assert collection.query_calls == [{
        "query_embeddings": [[1.0, 0.0]],
        "n_results": 2,
        "include": ["documents", "metadatas", "distances"],
        "where": {"page": 17},
    }]
    assert FakeEmbeddingProvider.calls == [["OAuth2 authentication"]]


@pytest.mark.asyncio
async def test_incomplete_chroma_records_are_skipped_and_results_checked(tmp_path, monkeypatch):
    result = {
        "ids": [["missing", "valid"]],
        "documents": [[None, "real text"]],
        "metadatas": [[None, {}]],
        "distances": [[0.0, 0.2]],
    }
    db, _, collection, _ = make_database(tmp_path, monkeypatch, result=result)
    rag = open_vector_rag(db)
    hits = await rag.search("apple")
    assert len(hits) == 1
    assert hits[0].id == "valid"
    assert hits[0].content == "real text"
    assert hits[0].source is None

    collection.results["distances"] = [[0.1]]
    with pytest.raises(RuntimeError, match="misaligned"):
        await rag.search("apple")
    collection.results["distances"] = [[float("nan"), 0.2]]
    with pytest.raises(RuntimeError, match="invalid vector distance"):
        await rag.search("apple")


def test_missing_database_is_not_created(tmp_path, monkeypatch):
    database = tmp_path / "missing"
    with pytest.raises(FileNotFoundError):
        open_vector_rag(database)
    assert not database.exists()

    database.mkdir()
    with pytest.raises(FileNotFoundError, match="No existing Chroma"):
        open_vector_rag(database)


def test_open_requires_explicit_or_stored_embedding_identity(tmp_path, monkeypatch):
    db, _, collection, _ = make_database(tmp_path, monkeypatch, metadata={})
    # Missing model settings are never guessed silently.
    with pytest.raises(ValueError, match="provider"):
        open_vector_rag(db)

    rag = open_vector_rag(
        db, provider="ollama", model="embeddinggemma",
        base_url="http://localhost:11434",
    )
    assert rag.collection == "knowledge"

    collection.metadata = {
        PROVIDER_META: "ollama",
        MODEL_META: "other-model",
        ENDPOINT_META: "http://localhost:11434",
    }
    with pytest.raises(ValueError, match="model conflicts"):
        open_vector_rag(db, model="embeddinggemma")
    with pytest.raises(ValueError, match="endpoint conflicts"):
        open_vector_rag(db, base_url="http://localhost:11435")
    with pytest.raises(ValueError, match="provider conflicts"):
        open_vector_rag(db, provider="openai")


@pytest.mark.asyncio
async def test_search_rejects_invalid_inputs(tmp_path, monkeypatch):
    db, _, _, _ = make_database(tmp_path, monkeypatch)
    rag = open_vector_rag(db)
    for query in ("", "  ", "x" * 2001):
        with pytest.raises(ValueError, match="query"):
            await rag.search(query)
    for limit in (0, 21, True, 0.5):
        with pytest.raises(ValueError, match="limit"):
            await rag.search("query", limit=limit)
    with pytest.raises(TypeError, match="where"):
        await rag.search("query", where="not a Chroma filter")


def test_no_mutation_api_is_exported(tmp_path, monkeypatch):
    import cli_agent.sdk as sdk

    db, _, _, _ = make_database(tmp_path, monkeypatch)
    rag = open_vector_rag(db)
    assert not hasattr(sdk, "build_vector_rag")
    assert not hasattr(sdk, "VectorRagBuildResult")
    assert not hasattr(rag, "add_record")
    assert not hasattr(rag, "add_chunks")
    assert not hasattr(rag, "delete_source")
    assert not hasattr(rag, "build")
