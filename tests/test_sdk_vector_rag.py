"""Standalone vector RAG API and raw-file indexing contracts."""
from __future__ import annotations

import asyncio
from uuid import UUID
from pathlib import Path

import pytest

from cli_agent.sdk import build_vector_rag, open_vector_rag, VectorRagBuildResult
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


def test_rag_file_inventory_propagates_walk_errors(monkeypatch, tmp_path):
    from cli_agent.context.knowledge_vector import _file_paths
    from cli_agent.context import knowledge_vector

    root = tmp_path / "docs"
    root.mkdir()
    (root / "present.md").write_text("content", encoding="utf-8")
    original_walk = knowledge_vector.os.walk

    def interrupted_walk(*args, **kwargs):
        yield from original_walk(*args, **kwargs)
        error = PermissionError("access denied")
        assert kwargs.get("onerror") is not None
        kwargs["onerror"](error)

    monkeypatch.setattr(knowledge_vector.os, "walk", interrupted_walk)
    with pytest.raises(PermissionError, match="access denied"):
        _file_paths(root, max_files=10)


@pytest.mark.asyncio
async def test_rag_incremental_walk_failure_preserves_existing_database(tmp_path, monkeypatch):
    from cli_agent.context import knowledge_vector

    monkeypatch.setattr(knowledge_vector, "EmbeddingProvider", FakeEmbeddingProvider)
    monkeypatch.setattr("cli_agent.sdk.vector_rag.EmbeddingProvider", FakeEmbeddingProvider)
    root = tmp_path / "docs"
    root.mkdir()
    (root / "first.txt").write_text("apple", encoding="utf-8")
    build = await build_vector_rag(root)
    database = build.rag.database
    before = database.read_bytes()
    original_walk = knowledge_vector.os.walk

    def interrupted_walk(*args, **kwargs):
        iterator = original_walk(*args, **kwargs)
        yield next(iterator)
        kwargs["onerror"](PermissionError("subdirectory inaccessible"))

    monkeypatch.setattr(knowledge_vector.os, "walk", interrupted_walk)
    with pytest.raises(PermissionError, match="subdirectory inaccessible"):
        await build_vector_rag(root, incremental=True)
    assert database.read_bytes() == before



@pytest.mark.asyncio
async def test_open_index_add_record_with_optional_id_and_source(tmp_path, monkeypatch):
    monkeypatch.setattr("cli_agent.context.knowledge_vector.EmbeddingProvider", FakeEmbeddingProvider)
    monkeypatch.setattr("cli_agent.sdk.vector_rag.EmbeddingProvider", FakeEmbeddingProvider)
    root = tmp_path / "documents"
    root.mkdir()
    built = await build_vector_rag(root)
    assert built.total == 0
    rag = open_vector_rag(built.rag.database)
    assert await rag.search("apple") == ()

    auto_id = await rag.add_record("apple information", source="crm/customer/17")
    UUID(auto_id)  # No ID supplied: a valid UUID was generated.
    assert auto_id != await rag.add_record("river information")
    hits = await open_vector_rag(built.rag.database).search("apple")
    assert hits[0].record_id == auto_id
    assert hits[0].path == f"record://{auto_id}"
    assert hits[0].source == "crm/customer/17"
    assert hits[0].preview == "apple information"


@pytest.mark.asyncio
async def test_upsert_reuses_vectors_if_text_unchanged_and_updates_source(tmp_path, monkeypatch):
    monkeypatch.setattr("cli_agent.context.knowledge_vector.EmbeddingProvider", FakeEmbeddingProvider)
    monkeypatch.setattr("cli_agent.sdk.vector_rag.EmbeddingProvider", FakeEmbeddingProvider)
    FakeEmbeddingProvider.calls = []
    root = tmp_path / "documents"
    root.mkdir()
    rag = (await build_vector_rag(root)).rag
    assert await rag.add_record(content="apple", id="item-a", source="one") == "item-a"
    assert len(FakeEmbeddingProvider.calls) == 1
    assert await rag.add_record(content="apple", id="item-a", source="two") == "item-a"
    assert len(FakeEmbeddingProvider.calls) == 1
    assert (await rag.search("apple"))[0].source == "two"
    assert await rag.add_record(content="river", id="item-a") == "item-a"
    assert len(FakeEmbeddingProvider.calls) == 3  # initial embedding, query, updated embedding
    assert (await rag.search("river"))[0].source is None
    assert (await rag.search("river"))[0].preview == "river"


@pytest.mark.asyncio
async def test_inline_records_survive_file_update_and_full_rebuild(tmp_path, monkeypatch):
    monkeypatch.setattr("cli_agent.context.knowledge_vector.EmbeddingProvider", FakeEmbeddingProvider)
    monkeypatch.setattr("cli_agent.sdk.vector_rag.EmbeddingProvider", FakeEmbeddingProvider)
    root = tmp_path / "documents"
    root.mkdir()
    (root / "old.md").write_text("river flows", encoding="utf-8")
    built = await build_vector_rag(root)
    await built.rag.add_record("apple data", id="record-one", source="external-feed")
    (root / "old.md").unlink()
    (root / "new.txt").write_text("new river", encoding="utf-8")

    updated = await build_vector_rag(root, incremental=True)
    assert (updated.added, updated.changed, updated.removed) == (1, 0, 1)
    hits = await open_vector_rag(built.rag.database).search("apple")
    assert hits[0].record_id == "record-one"
    assert hits[0].source == "external-feed"

    rebuilt = await build_vector_rag(root, incremental=False)
    reopened = open_vector_rag(rebuilt.rag.database)
    hits = await reopened.search("apple")
    assert hits[0].record_id == "record-one"
    assert hits[0].source == "external-feed"

    (root / "new.txt").unlink()
    await build_vector_rag(root)
    assert (await open_vector_rag(rebuilt.rag.database).search("apple"))[0].record_id == "record-one"


@pytest.mark.asyncio
async def test_failed_record_embedding_does_not_change_old_record(tmp_path, monkeypatch):
    from cli_agent.sdk.vector_rag import EmbeddingProvider

    monkeypatch.setattr("cli_agent.context.knowledge_vector.EmbeddingProvider", FakeEmbeddingProvider)
    monkeypatch.setattr("cli_agent.sdk.vector_rag.EmbeddingProvider", FakeEmbeddingProvider)
    root = tmp_path / "docs"
    root.mkdir()
    rag = (await build_vector_rag(root)).rag
    await rag.add_record("apple existing", id="customer")

    async def fail_embedding(self, _texts):
        raise RuntimeError("embedding service unavailable")

    monkeypatch.setattr(FakeEmbeddingProvider, "embed", fail_embedding)
    with pytest.raises(RuntimeError, match="embedding service unavailable"):
        await rag.add_record("different content", id="customer", source="other")
    store = SQLiteKnowledgeStore(rag.database)
    try:
        assert store.sdk_record("customer")["content"] == "apple existing"
        assert store.sdk_record("customer")["source"] is None
    finally:
        store.close()


@pytest.mark.asyncio
async def test_record_input_rejection_and_invalid_open(tmp_path, monkeypatch):
    monkeypatch.setattr("cli_agent.context.knowledge_vector.EmbeddingProvider", FakeEmbeddingProvider)
    monkeypatch.setattr("cli_agent.sdk.vector_rag.EmbeddingProvider", FakeEmbeddingProvider)
    root = tmp_path / "docs"
    root.mkdir()
    rag = (await build_vector_rag(root)).rag
    with pytest.raises(ValueError, match="content"):
        await rag.add_record(" ")
    with pytest.raises(ValueError, match="source"):
        await rag.add_record("apple", source="")
    with pytest.raises(ValueError, match="id"):
        await rag.add_record("apple", id="\n")
    with pytest.raises(ValueError, match="content"):
        await rag.add_record("a" * (rag.max_read_bytes + 1))
    with pytest.raises(FileNotFoundError):
        open_vector_rag(tmp_path / "missing.sqlite")
