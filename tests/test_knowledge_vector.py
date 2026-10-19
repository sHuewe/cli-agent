from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from cli_agent.config import VectorKnowledgeConfig, load_config
from cli_agent.knowledge_vector import (
    SQLiteKnowledgeStore, VectorKnowledgeSession, _chunks, _source_file,
    _raw_paths, build_index,
)
from cli_agent.network_policy import NetworkConfig


def test_vector_configuration_is_fully_optional(tmp_path):
    config_path = tmp_path / "config.toml"
    config_path.write_text('[model]\nprovider = "ollama"\n', encoding="utf-8")
    assert load_config(config_path).knowledge_vector is None
    config_path.write_text(
        '[knowledge.vector]\nmode = "raw"\nsource = "docs"\n'
        'database = "index.sqlite"\nprovider = "ollama"\nmodel = "embed"\n',
        encoding="utf-8",
    )
    config = load_config(config_path)
    assert config.knowledge_vector.mode == "raw"
    assert config.okf is None


@pytest.mark.parametrize("extra", [
    'mode = "okf"\nsource = "docs"\n',
    'mode = "raw"\n',
    'mode = "raw"\nsource = "docs"\nsearch_limit = 100\n',
    'mode = "raw"\nsource = "docs"\nunknown_option = true\n',
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
        store.configure(root=tmp_path, mode="raw", provider="ollama", model="emb", dim=2)
        assert store.check(root=tmp_path, mode="raw", provider="ollama", model="emb") == 2
        assert store.search([1.0, 0.0], limit=1)[0]["path"] == "service.md"
        with pytest.raises(ValueError):
            store.check(root=tmp_path, mode="raw", provider="ollama", model="changed")
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


def test_source_containment_and_hardlinks(tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    (root / "document.md").write_text("safe", encoding="utf-8")
    assert _source_file(root, "document.md").read_text() == "safe"
    for name in ("../outside.md", "/etc/passwd", "sub/../../outside.md", "C:/windows"):
        with pytest.raises(ValueError):
            _source_file(root, name)
    link = root / "alias.md"
    link.symlink_to(root / "document.md")
    with pytest.raises(ValueError):
        _source_file(root, "alias.md")


@pytest.mark.asyncio
async def test_index_and_read_fail_closed_on_changes(tmp_path, monkeypatch):
    root = tmp_path / "docs"
    root.mkdir()
    (root / "sample.md").write_text("# Service\nToken validation", encoding="utf-8")
    index_path = tmp_path / "index.sqlite"
    config = VectorKnowledgeConfig(
        mode="raw", source=str(root), database=str(index_path),
        provider="ollama", model="embeddinggemma",
    )

    async def fake_embed(self, texts):
        return [[1.0, 0.0] if "Token" in text else [0.0, 1.0] for text in texts]

    from cli_agent.knowledge_vector import EmbeddingProvider
    monkeypatch.setattr(EmbeddingProvider, "embed", fake_embed)
    assert await build_index(config, root=root, path=index_path, network=NetworkConfig()) == 1
    with sqlite3.connect(index_path) as db:
        assert db.execute("SELECT COUNT(*) FROM chunks").fetchone()[0] > 0
        assert db.execute("SELECT COUNT(*) FROM sources").fetchone()[0] == 1
        assert db.execute("SELECT name FROM sqlite_master WHERE name='chunks_fts'").fetchone() is None
        # No document or chunk text, even inside stored schema definitions.
        assert b"Token validation" not in index_path.read_bytes()
        assert b"# Service" not in index_path.read_bytes()
    session = VectorKnowledgeSession(config, root=root, path=index_path, network=NetworkConfig())
    try:
        result = await session.search("Token validation")
        assert result["entries"][0]["path"] == "sample.md"
        read = await session.call_tool("knowledge_read", {"path": "sample.md"})
        assert read.structuredContent["kind"] == "concept"
        (root / "sample.md").write_text("Now modified", encoding="utf-8")
        assert (await session.search("Token validation"))["entries"] == []
        assert (await session.call_tool("knowledge_read", {"path": "sample.md"})).isError
    finally:
        session.close()


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
        store.configure(root=tmp_path, mode="okf", provider="ollama", model="embed", dim=2)
        store.db.execute("UPDATE metadata SET value='1' WHERE key='schema'")
        store.db.commit()
        with pytest.raises(ValueError, match="neu indexieren"):
            store.check(root=tmp_path, mode="okf", provider="ollama", model="embed")
    finally:
        store.close()
