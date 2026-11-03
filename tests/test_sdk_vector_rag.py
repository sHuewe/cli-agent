"""Standalone vector RAG API and raw-file indexing contracts."""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from cli_agent.sdk import build_vector_rag, VectorRagBuildResult
from cli_agent.context.knowledge_vector import SQLiteKnowledgeStore


class FakeEmbeddingProvider:
    calls: list[list[str]] = []

    def __init__(self, config, network, credential_rules=()):
        self.endpoint_identity = "http://localhost:11434"

    async def embed(self, texts):
        self.calls.append(texts)
        return [[1.0, 0.0] if "apple" in text.lower() else [0.0, 1.0] for text in texts]


@pytest.mark.asyncio
async def test_build_search_update_is_independent_of_agent(tmp_path, monkeypatch):
    monkeypatch.setattr(
        "cli_agent.context.knowledge_vector.EmbeddingProvider",
        FakeEmbeddingProvider,
    )
    monkeypatch.setattr(
        "cli_agent.sdk.vector_rag.EmbeddingProvider",
        FakeEmbeddingProvider,
    )
    FakeEmbeddingProvider.calls = []

    root = tmp_path / "documents"
    root.mkdir()
    (root / "fruit.txt").write_text("apple and pear", encoding="utf-8")
    nested = root / "notes"
    nested.mkdir()
    (nested / "notes.md").write_text("# river and bridge", encoding="utf-8")
    (root / "skip.bin").write_bytes(b"should not be indexed")

    first = await build_vector_rag(root)
    assert isinstance(first, VectorRagBuildResult)
    assert first.total == 2
    assert first.added == 2
    assert first.rag.database.is_file()
    assert not first.rag.database.is_relative_to(root)

    results = await first.rag.search("apple")
    assert results[0].path == "fruit.txt"
    assert results[0].score == 1.0
    assert results[0].preview == "apple and pear"

    # No re-embedding when the source files are unchanged.
    FakeEmbeddingProvider.calls = []
    second = await build_vector_rag(root, incremental=True)
    assert (second.added, second.changed, second.unchanged, second.removed) == (0, 0, 2, 0)
    assert FakeEmbeddingProvider.calls == []

    (root / "fruit.txt").write_text("apple apple", encoding="utf-8")
    (nested / "notes.md").unlink()
    third = await build_vector_rag(root, incremental=True)
    assert (third.added, third.changed, third.unchanged, third.removed) == (0, 1, 0, 1)
    assert len(FakeEmbeddingProvider.calls) == 1

    store = SQLiteKnowledgeStore(third.rag.database)
    try:
        assert store.metadata()["mode"] == "files"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_source_validation_and_index_is_not_created_on_invalid_input(tmp_path):
    root = tmp_path / "docs"
    root.mkdir()
    with pytest.raises(ValueError, match="outside"):
        await build_vector_rag(root, database=root / "vectors.sqlite")
    with pytest.raises(ValueError, match="max_files"):
        await build_vector_rag(root, max_files=0)
    with pytest.raises(ValueError, match="base_url"):
        await build_vector_rag(root, provider="openai")
    with pytest.raises(ValueError, match="api_key_env"):
        await build_vector_rag(root, api_key_env="MY_KEY")


@pytest.mark.asyncio
async def test_raw_file_index_rejects_symlinks_and_limits(monkeypatch, tmp_path):
    from cli_agent.context.knowledge_vector import _index_paths, _read_source
    from cli_agent.settings.config import VectorKnowledgeConfig

    root = tmp_path / "docs"
    root.mkdir()
    (root / "a.txt").write_text("content", encoding="utf-8")
    (root / "b.md").write_text("content", encoding="utf-8")
    config = VectorKnowledgeConfig(mode="files", max_files=1)
    with pytest.raises(ValueError, match="Zu viele"):
        _index_paths(config, root, max_read_bytes=1024)

    target = tmp_path / "outside.txt"
    target.write_text("secret", encoding="utf-8")
    try:
        (root / "link.txt").symlink_to(target)
    except (OSError, NotImplementedError):
        pytest.skip("Symlinks not supported on this test platform")
    with pytest.raises(ValueError):
        _read_source(root, "link.txt", mode="files", max_read_bytes=1024)


@pytest.mark.asyncio
async def test_search_omits_stale_sources_and_limits_preview(tmp_path, monkeypatch):
    monkeypatch.setattr("cli_agent.context.knowledge_vector.EmbeddingProvider", FakeEmbeddingProvider)
    monkeypatch.setattr("cli_agent.sdk.vector_rag.EmbeddingProvider", FakeEmbeddingProvider)
    root = tmp_path / "docs"
    root.mkdir()
    file = root / "one.txt"
    file.write_text("apple here", encoding="utf-8")
    index = await build_vector_rag(root)
    file.write_text("changed since indexing", encoding="utf-8")
    assert await index.rag.search("apple") == ()
    with pytest.raises(ValueError):
        await index.rag.search("apple", limit=0)
    with pytest.raises(ValueError):
        await index.rag.search("apple", preview_chars=2001)
