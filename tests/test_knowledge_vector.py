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


def test_boolean_embeddings_and_non_finite_stored_vectors_are_rejected(tmp_path):
    import struct
    from cli_agent.knowledge_vector import _encode
    with pytest.raises(ValueError, match="ungültige"):
        _encode([True, 0.5])
    store = SQLiteKnowledgeStore(tmp_path / "corrupt.sqlite", readonly=False)
    try:
        store.create()
        store.replace_documents([("service.md", hashlib.sha256(b"doc").hexdigest(), [[1.0, 0.0]])])
        store.db.execute(
            "UPDATE chunks SET vector=?",
            (struct.pack("<2f", float("nan"), 0.0),),
        )
        store.db.commit()
        with pytest.raises(ValueError, match="ungültige"):
            store.search([1.0, 0.0])
    finally:
        store.close()


@pytest.mark.asyncio
async def test_okf_only_vector_index_rejects_raw_and_builds_reachable_concepts(tmp_path, monkeypatch):
    from cli_agent.knowledge_vector import EmbeddingProvider, update_index
    root = tmp_path / "okf"
    root.mkdir()
    (root / "index.md").write_text("# Knowledge\n[Service](service.md)", encoding="utf-8")
    (root / "service.md").write_text(
        "---\ntype: concept\ntitle: Service\n---\n# Service\nInformation", encoding="utf-8",
    )
    db = tmp_path / "index.sqlite"
    config = VectorKnowledgeConfig(mode="okf", database=str(db))
    async def embed(self, texts):
        return [[1.0, 0.0] for _ in texts]
    monkeypatch.setattr(EmbeddingProvider, "embed", embed)
    assert await build_index(config, root=root, path=db, network=NetworkConfig()) == 1
    session = VectorKnowledgeSession(config, root=root, path=db, network=NetworkConfig())
    try:
        entries = (await session.search("service"))["entries"]
        assert [x["path"] for x in entries] == ["service.md"]
    finally:
        session.close()
    stats = await update_index(config, root=root, path=db, network=NetworkConfig())
    assert (stats.added, stats.changed, stats.unchanged, stats.removed) == (0, 0, 1, 0)
    with pytest.raises(ValueError, match="OKF"):
        await build_index(VectorKnowledgeConfig(mode="raw"), root=root, path=db, network=NetworkConfig())


def test_okf_verified_read_enforces_configured_limit(tmp_path):
    from cli_agent.knowledge_vector import _read_source
    (tmp_path / "concept.md").write_text(
        "---\ntype: concept\n---\n# A\n" + "content " * 100, encoding="utf-8"
    )
    with pytest.raises(ValueError, match="Leselimit"):
        _read_source(tmp_path, "concept.md", mode="okf", max_read_bytes=64)
    assert "content" in _read_source(
        tmp_path, "concept.md", mode="okf", max_read_bytes=4096
    )


@pytest.mark.skipif(__import__("os").name != "posix", reason="POSIX symlink race regression")
def test_okf_verified_read_blocks_swapped_symlink(tmp_path, monkeypatch):
    import cli_agent.knowledge_vector as module
    root = tmp_path / "okf"
    root.mkdir()
    document = root / "concept.md"
    document.write_text("---\ntype: concept\n---\nSafe", encoding="utf-8")
    external = tmp_path / "secret.md"
    external.write_text("---\ntype: concept\n---\nPRIVATE", encoding="utf-8")
    original = module._source_file
    def swap(root_path, relative):
        validated = original(root_path, relative)
        document.unlink()
        document.symlink_to(external)
        return validated
    monkeypatch.setattr(module, "_source_file", swap)
    with pytest.raises(OSError):
        module._read_source(root, "concept.md", mode="okf")


@pytest.mark.asyncio
async def test_okf_read_limit_is_bound_to_index_metadata(tmp_path, monkeypatch):
    from cli_agent.knowledge_vector import EmbeddingProvider, update_index
    root = tmp_path / "okf"
    root.mkdir()
    (root / "index.md").write_text("[A](a.md)", encoding="utf-8")
    (root / "a.md").write_text("---\ntype: concept\n---\nA", encoding="utf-8")
    index = tmp_path / "vectors.sqlite"
    config = VectorKnowledgeConfig(mode="okf")
    async def embed(self, texts):
        return [[1.0, 0.0] for _ in texts]
    monkeypatch.setattr(EmbeddingProvider, "embed", embed)
    await build_index(config, root=root, path=index, network=NetworkConfig(), okf_max_read_bytes=512)
    with pytest.raises(ValueError, match="neu indexieren"):
        VectorKnowledgeSession(config, root=root, path=index, network=NetworkConfig(), okf_max_read_bytes=2048)
    with pytest.raises(ValueError, match="neu indexieren"):
        await update_index(config, root=root, path=index, network=NetworkConfig(), okf_max_read_bytes=2048)
