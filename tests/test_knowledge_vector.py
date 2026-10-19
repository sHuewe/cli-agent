from __future__ import annotations

import hashlib
from contextlib import closing
import sqlite3
from pathlib import Path

import pytest

from cli_agent.config import VectorKnowledgeConfig, load_config
from cli_agent.knowledge_vector import (
    SQLiteKnowledgeStore, VectorKnowledgeSession, _chunks, build_index,
)
from cli_agent.network_policy import NetworkConfig


def test_vector_configuration_is_fully_optional(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text('[model]\nprovider = "ollama"\n', encoding="utf-8")
    assert load_config(config_path).knowledge_vector is None
    config_path.write_text(
        '[okf]\nrepository = "docs"\n'
        '[knowledge.vector]\nmode = "okf"\n'
        'database = "index.sqlite"\nprovider = "ollama"\nmodel = "embed"\n',
        encoding="utf-8",
    )
    config = load_config(config_path)
    assert config.knowledge_vector.mode == "okf"
    assert config.okf is not None


@pytest.mark.parametrize("extra", [
    'mode = "okf"\nsource = "docs"\n',
    'mode = "raw"\n',
    'mode = "okf"\nsearch_limit = 100\n',
    'mode = "okf"\nunknown_option = true\n',
])
def test_invalid_vector_configuration_is_rejected(tmp_path, extra):
    path = tmp_path / "config.toml"
    path.write_text("[knowledge.vector]\n" + extra, encoding="utf-8")
    with pytest.raises(ValueError):
        load_config(path)


def test_store_search_hybrid_and_provenance(tmp_path):
    path = tmp_path / "vectors.sqlite"
    store = SQLiteKnowledgeStore(path, readonly=False)
    try:
        store.create()
        store.replace_documents([
            ("service.md", hashlib.sha256(b"Token validation").hexdigest(), [[1.0, 0.0]]),
            ("database.md", hashlib.sha256(b"Storage").hexdigest(), [[0.0, 1.0]]),
        ])
        store.configure(root=tmp_path, mode="okf", provider="ollama", model="emb", endpoint="http://localhost:11434", dim=2)
        assert store.check(root=tmp_path, mode="okf", provider="ollama", model="emb", endpoint="http://localhost:11434") == 2
        assert store.search([1.0, 0.0], limit=1)[0]["path"] == "service.md"
        with pytest.raises(ValueError):
            store.check(root=tmp_path, mode="okf", provider="ollama", model="changed", endpoint="http://localhost:11434")
        assert store.source_sha("service.md") == hashlib.sha256(b"Token validation").hexdigest()
        schema = {
            row["name"]: {column["name"] for column in store.db.execute(f"PRAGMA table_info({row['name']})")}
            for row in store.db.execute("SELECT name FROM sqlite_master WHERE type='table'")
        }
        assert schema["sources"] == {"path", "sha256"}
        assert schema["chunks"] == {"id", "source", "vector"}
        assert schema["metadata"] == {"key", "value"}
        assert "chunks_fts" not in schema
        assert "content" not in schema["sources"]
        assert "body" not in schema["chunks"]
        with pytest.raises(ValueError):
            store.search([0.0, 0.0])
        with pytest.raises(ValueError):
            store.search([1.0])

    finally:
        store.close()




def test_chunking_bounded():
    text = "A" * 4000
    chunks = _chunks(text)
    assert len(chunks) > 2
    assert chunks[0] != ""
    assert chunks[-1]


@pytest.mark.asyncio
@pytest.mark.parametrize("provider", ["ollama", "openai"])
async def test_embedding_provider_contracts(monkeypatch, provider):
    import httpx
    from cli_agent.knowledge_vector import EmbeddingProvider

    seen = []
    class Response:
        def raise_for_status(self):
            pass

        def json(self):
            if provider == "ollama":
                return {"embeddings": [[1.0, 2.0], [3.0, 4.0]]}
            return {"data": [
                {"index": 1, "embedding": [3.0, 4.0]},
                {"index": 0, "embedding": [1.0, 2.0]},
            ]}

    class Client:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            pass

        async def post(self, url, **kwargs):
            seen.append((url, kwargs["json"]))
            return Response()

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: Client())
    config = VectorKnowledgeConfig(
        provider=provider, model="embed", base_url=(
            "http://localhost:11434/v1" if provider == "openai"
            else "http://localhost:11434"
        ),
    )
    embedding = EmbeddingProvider(config, NetworkConfig())
    result = await embedding.embed(["a", "b"])
    assert result == [[1.0, 2.0], [3.0, 4.0]]
    assert seen[0][0].endswith("/embeddings" if provider == "openai" else "/api/embed")


@pytest.mark.asyncio
async def test_embedding_rejects_invalid_vectors(monkeypatch):
    import httpx
    from cli_agent.knowledge_vector import EmbeddingProvider
    class Response:
        def raise_for_status(self):
            pass
        def json(self):
            return {"embeddings": [[float("nan")]]}
    class Client:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            pass
        async def post(self, *args, **kwargs):
            return Response()
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: Client())
    provider = EmbeddingProvider(VectorKnowledgeConfig(), NetworkConfig())
    with pytest.raises(ValueError):
        await provider.embed(["input"])
    with pytest.raises(ValueError):
        await provider.embed([])


def test_okf_indexing_only_reachable_valid_concepts(tmp_path):
    from cli_agent.knowledge_vector import _okf_paths
    (tmp_path / "index.md").write_text("# Knowledge\n[Service](service.md)", encoding="utf-8")
    (tmp_path / "service.md").write_text(
        "---\ntype: concept\ntitle: Service\n---\n# Service",
        encoding="utf-8",
    )
    (tmp_path / "hidden.md").write_text(
        "---\ntype: concept\n---\nNot in index",
        encoding="utf-8",
    )
    assert _okf_paths(tmp_path, max_files=10) == ["service.md"]


def test_vectors_map_to_concepts_and_use_best_chunk_score(tmp_path):
    store = SQLiteKnowledgeStore(tmp_path / "index.sqlite", readonly=False)
    try:
        store.create()
        store.replace_documents([
            ("first.md", hashlib.sha256(b"first").hexdigest(), [[1.0, 0.0], [0.0, 1.0]]),
            ("second.md", hashlib.sha256(b"second").hexdigest(), [[-1.0, 0.0]]),
        ])
        hits = store.search([1.0, 0.0], limit=2)
        assert [hit["path"] for hit in hits] == ["first.md", "second.md"]
        assert len(hits) == 2
        assert hits[0]["score"] == 1.0
    finally:
        store.close()


def test_old_schema_version_rejected(tmp_path):
    store = SQLiteKnowledgeStore(tmp_path / "old.sqlite", readonly=False)
    try:
        store.create()
        store.configure(root=tmp_path, mode="okf", provider="ollama", model="embed", endpoint="http://localhost:11434", dim=2)
        store.db.execute("UPDATE metadata SET value='2' WHERE key='schema'")
        store.db.commit()
        with pytest.raises(ValueError, match="neu indexieren"):
            store.check(root=tmp_path, mode="okf", provider="ollama", model="embed", endpoint="http://localhost:11434")
    finally:
        store.close()






def test_embedding_endpoint_metadata_rejects_changed_host(tmp_path):
    store = SQLiteKnowledgeStore(tmp_path / "index.sqlite", readonly=False)
    try:
        store.create()
        store.configure(
            root=tmp_path, mode="okf", provider="openai", model="embed",
            endpoint="https://first.example/v1", dim=2,
        )
        assert store.check(
            root=tmp_path, mode="okf", provider="openai", model="embed",
            endpoint="https://first.example/v1",
        ) == 2
        with pytest.raises(ValueError, match="neu indexieren"):
            store.check(
                root=tmp_path, mode="okf", provider="openai", model="embed",
                endpoint="https://second.example/v1",
            )
    finally:
        store.close()


def test_embedding_endpoint_identity_normalization():
    from cli_agent.knowledge_vector import _embedding_endpoint_identity
    assert _embedding_endpoint_identity("HTTPS://EXAMPLE.ORG:443/v1/") == "https://example.org/v1"
    assert _embedding_endpoint_identity("http://localhost:11434/") == "http://localhost:11434"
    with pytest.raises(ValueError):
        _embedding_endpoint_identity("https://example.org/v1?secret=x")





@pytest.mark.skipif(__import__("os").name == "nt", reason="Windows filenames cannot contain ?")
def test_readonly_sqlite_uri_handles_query_and_fragment_in_filename(tmp_path):
    path = tmp_path / "vectors?source#one%.sqlite"
    writer = SQLiteKnowledgeStore(path, readonly=False)
    try:
        writer.create()
        writer.configure(
            root=tmp_path, mode="okf", provider="ollama",
            model="embed", endpoint="http://localhost:11434", dim=2,
        )
        writer.db.commit()
    finally:
        writer.close()
    reader = SQLiteKnowledgeStore(path)
    try:
        assert reader.check(
            root=tmp_path, mode="okf", provider="ollama",
            model="embed", endpoint="http://localhost:11434",
        ) == 2
    finally:
        reader.close()
    assert not (tmp_path / "vectors").exists()



@pytest.mark.asyncio
@pytest.mark.parametrize("indices", [
    [0, 0], [1, 2], [-1, 1], [True, 1], ["0", 1], [0, None],
])
async def test_openai_embedding_rejects_bad_indices(monkeypatch, indices):
    import httpx
    from cli_agent.knowledge_vector import EmbeddingProvider
    class Response:
        def raise_for_status(self):
            pass
        def json(self):
            return {"data": [{"index": ix, "embedding": [1.0, 0.0]} for ix in indices]}
    class Client:
        async def __aenter__(self):
            return self
        async def __aexit__(self, *args):
            return False
        async def post(self, *args, **kwargs):
            return Response()
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kwargs: Client())
    provider = EmbeddingProvider(
        VectorKnowledgeConfig(provider="openai", model="embed", base_url="http://localhost:11434/v1"),
        NetworkConfig(),
    )
    with pytest.raises(RuntimeError, match="Indizes"):
        await provider.embed(["first", "second"])
