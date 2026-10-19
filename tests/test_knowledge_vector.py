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
            ("service.md", "Token validation", [("Bearer token validation", [1.0, 0.0])]),
            ("database.md", "Storage", [("SQL table", [0.0, 1.0])]),
        ])
        store.configure(root=tmp_path, mode="raw", provider="ollama", model="emb", dim=2)
        assert store.check(root=tmp_path, mode="raw", provider="ollama", model="emb") == 2
        assert store.search([1.0, 0.0], "Bearer token", limit=1)[0]["path"] == "service.md"
        with pytest.raises(ValueError):
            store.check(root=tmp_path, mode="raw", provider="ollama", model="changed")
        assert store.source_sha("service.md") == hashlib.sha256(b"Token validation").hexdigest()
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
