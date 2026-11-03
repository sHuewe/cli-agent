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



@pytest.mark.asyncio
async def test_add_record_to_existing_index_without_sdk_records_table(tmp_path, monkeypatch):
    # Standalone indexes created by the initial version of PR #281 do not
    # contain sdk_records; the first upsert should migrate them in place.
    monkeypatch.setattr("cli_agent.context.knowledge_vector.EmbeddingProvider", FakeEmbeddingProvider)
    monkeypatch.setattr("cli_agent.sdk.vector_rag.EmbeddingProvider", FakeEmbeddingProvider)
    root = tmp_path / "docs"
    root.mkdir()
    rag = (await build_vector_rag(root)).rag
    import sqlite3
    with sqlite3.connect(rag.database) as db:
        db.execute("DROP TABLE sdk_records")
    opened = open_vector_rag(rag.database)
    assert await opened.add_record("apple", id="legacy", source="import") == "legacy"
    hits = await open_vector_rag(rag.database).search("apple")
    assert hits[0].record_id == "legacy"
    assert hits[0].source == "import"



@pytest.mark.asyncio
async def test_inline_write_waits_for_incremental_index_publication(tmp_path, monkeypatch):
    """An inline upsert must never race with replacing the live SQLite file."""
    import hashlib
    from cli_agent.context import knowledge_vector
    from cli_agent.sdk import open_vector_rag

    monkeypatch.setattr(knowledge_vector, "EmbeddingProvider", FakeEmbeddingProvider)
    monkeypatch.setattr("cli_agent.sdk.vector_rag.EmbeddingProvider", FakeEmbeddingProvider)
    root = tmp_path / "docs"
    root.mkdir()
    document = root / "doc.md"
    document.write_text("apple initial", encoding="utf-8")
    first = await build_vector_rag(root)

    started = asyncio.Event()
    release = asyncio.Event()
    original_embed = FakeEmbeddingProvider.embed

    async def slow_embed(self, texts):
        if "river updated" in texts[0]:
            started.set()
            await release.wait()
        return await original_embed(self, texts)

    monkeypatch.setattr(FakeEmbeddingProvider, "embed", slow_embed)
    document.write_text("river updated", encoding="utf-8")
    update_task = asyncio.create_task(build_vector_rag(root, incremental=True))
    writer_task = None
    try:
        await asyncio.wait_for(started.wait(), timeout=5)
        writer_task = asyncio.create_task(first.rag.add_record("apple inline", id="manual", source="crm"))
        await asyncio.sleep(0.05)
        assert not writer_task.done(), "Writer must wait for index replacement"
    finally:
        release.set()

    await asyncio.wait_for(update_task, timeout=10)
    assert writer_task is not None
    assert await asyncio.wait_for(writer_task, timeout=10) == "manual"

    store = SQLiteKnowledgeStore(first.rag.database)
    try:
        assert store.source_sha("doc.md") == hashlib.sha256(b"river updated").hexdigest()
        assert store.sdk_record("manual")["source"] == "crm"
    finally:
        store.close()
    rag = open_vector_rag(first.rag.database)
    assert any(hit.path == "doc.md" for hit in await rag.search("river"))


@pytest.mark.asyncio
async def test_first_inline_record_validates_embedding_dimension_across_batches(tmp_path, monkeypatch):
    from cli_agent.context import knowledge_vector

    monkeypatch.setattr(knowledge_vector, "EmbeddingProvider", FakeEmbeddingProvider)
    monkeypatch.setattr("cli_agent.sdk.vector_rag.EmbeddingProvider", FakeEmbeddingProvider)
    root = tmp_path / "docs"
    root.mkdir()
    rag = (await build_vector_rag(root)).rag
    calls = 0

    async def incompatible_batches(self, texts):
        nonlocal calls
        calls += 1
        return [[1.0, 0.0] if calls == 1 else [1.0, 0.0, 0.0] for _ in texts]

    monkeypatch.setattr(FakeEmbeddingProvider, "embed", incompatible_batches)
    # More than MAX_BATCH chunks makes multiple embedding requests.
    with pytest.raises(ValueError, match="dimensions differ"):
        await rag.add_record("apple " * 12000, id="first", source="imported")
    assert calls >= 2

    store = SQLiteKnowledgeStore(rag.database)
    try:
        assert store.sdk_record("first") is None
        assert store.source_sha("record://first") is None
        assert store.metadata()["dimension"] == "0"
    finally:
        store.close()


@pytest.mark.asyncio
async def test_readonly_search_without_lockfile(tmp_path, monkeypatch):
    """Search must not create a lockfile when a database is deployed read-only."""
    from cli_agent.context import knowledge_vector

    monkeypatch.setattr(knowledge_vector, "EmbeddingProvider", FakeEmbeddingProvider)
    monkeypatch.setattr("cli_agent.sdk.vector_rag.EmbeddingProvider", FakeEmbeddingProvider)
    root = tmp_path / "docs"
    root.mkdir()
    (root / "document.txt").write_text("apple information", encoding="utf-8")
    rag = (await build_vector_rag(root)).rag
    lockfile = Path(str(rag.database) + ".lock")
    assert lockfile.is_file()
    lockfile.unlink()
    assert (await rag.search("apple"))[0].path == "document.txt"
    assert not lockfile.exists()


@pytest.mark.asyncio
async def test_readonly_search_when_sidecar_access_denied(tmp_path, monkeypatch):
    """Sharing a readable index cannot require permissions on its lockfile."""
    import errno
    from cli_agent.context import knowledge_vector, vector_index_lock

    monkeypatch.setattr(knowledge_vector, "EmbeddingProvider", FakeEmbeddingProvider)
    monkeypatch.setattr("cli_agent.sdk.vector_rag.EmbeddingProvider", FakeEmbeddingProvider)
    root = tmp_path / "docs"
    root.mkdir()
    (root / "document.txt").write_text("apple information", encoding="utf-8")
    rag = (await build_vector_rag(root)).rag
    original_open = vector_index_lock.os.open

    def denied_lock_open(path, *args, **kwargs):
        if str(path).endswith(".sqlite.lock"):
            raise PermissionError(errno.EACCES, "Access denied", str(path))
        return original_open(path, *args, **kwargs)

    monkeypatch.setattr(vector_index_lock.os, "open", denied_lock_open)
    assert (await rag.search("apple"))[0].path == "document.txt"


@pytest.mark.asyncio
async def test_shared_search_blocks_rebuild_when_lockfile_available(tmp_path, monkeypatch):
    from cli_agent.context import knowledge_vector
    from cli_agent.context.vector_index_lock import vector_index_lock

    monkeypatch.setattr(knowledge_vector, "EmbeddingProvider", FakeEmbeddingProvider)
    monkeypatch.setattr("cli_agent.sdk.vector_rag.EmbeddingProvider", FakeEmbeddingProvider)
    root = tmp_path / "docs"
    root.mkdir()
    (root / "document.txt").write_text("apple information", encoding="utf-8")
    rag = (await build_vector_rag(root)).rag
    task = None
    async with vector_index_lock(rag.database, shared=True):
        task = asyncio.create_task(build_vector_rag(root))
        await asyncio.sleep(0.05)
        assert not task.done()
    assert task is not None
    await asyncio.wait_for(task, timeout=10)
