"""Incremental vector-index regression tests using real SQLite and fake embeddings."""
from __future__ import annotations

import hashlib

import pytest

from cli_agent.context import knowledge_vector as vector
from cli_agent.settings.config import VectorKnowledgeConfig
from cli_agent.security.network_policy import NetworkConfig


def _corpus(tmp_path):
    root = tmp_path / "okf"
    root.mkdir()
    (root / "index.md").write_text(
        "# Knowledge\n[First](first.md)\n[Second](second.md)\n", encoding="utf-8"
    )
    for name in ("first", "second"):
        (root / f"{name}.md").write_text(
            f"---\ntype: concept\ntitle: {name}\n---\n# {name}\nOriginal {name} content",
            encoding="utf-8",
        )
    return root, tmp_path / "knowledge.sqlite", VectorKnowledgeConfig(mode="okf")


def _published_index(path):
    store = vector.SQLiteKnowledgeStore(path)
    try:
        rows = store.db.execute("SELECT path,sha256 FROM sources ORDER BY path").fetchall()
        chunks = store.db.execute("SELECT source,hex(vector) FROM chunks ORDER BY source,id").fetchall()
        return rows, chunks, hashlib.sha256(path.read_bytes()).hexdigest()
    finally:
        store.close()


@pytest.mark.asyncio
async def test_incremental_index_no_change_skips_embed_and_preserves_database(tmp_path, monkeypatch):
    root, path, config = _corpus(tmp_path)
    calls = []
    async def embed(self, texts):
        calls.append(tuple(texts))
        return [[1.0, 0.0] for _ in texts]
    monkeypatch.setattr(vector.EmbeddingProvider, "embed", embed)
    await vector.build_index(config, root=root, path=path, network=NetworkConfig())
    before = _published_index(path)
    calls.clear()
    stats = await vector.update_index(config, root=root, path=path, network=NetworkConfig())
    assert (stats.added, stats.changed, stats.unchanged, stats.removed) == (0, 0, 2, 0)
    assert calls == []
    assert _published_index(path) == before


@pytest.mark.asyncio
async def test_incremental_index_updates_changed_adds_new_and_removes_unlinked(tmp_path, monkeypatch):
    root, path, config = _corpus(tmp_path)
    calls = []
    async def embed(self, texts):
        calls.extend(texts)
        return [[1.0, 0.0] for _ in texts]
    monkeypatch.setattr(vector.EmbeddingProvider, "embed", embed)
    await vector.build_index(config, root=root, path=path, network=NetworkConfig())
    original = _published_index(path)
    calls.clear()
    (root / "first.md").write_text(
        "---\ntype: concept\ntitle: First\n---\n# First\nUpdated first source",
        encoding="utf-8",
    )
    (root / "third.md").write_text(
        "---\ntype: concept\ntitle: Third\n---\n# Third\nA new source",
        encoding="utf-8",
    )
    (root / "index.md").write_text(
        "# Knowledge\n[First](first.md)\n[Third](third.md)\n", encoding="utf-8",
    )
    stats = await vector.update_index(config, root=root, path=path, network=NetworkConfig())
    assert (stats.added, stats.changed, stats.unchanged, stats.removed) == (1, 1, 0, 1)
    assert len(calls) == 2
    after = _published_index(path)
    assert {row[0] for row in after[0]} == {"first.md", "third.md"}
    assert after != original


@pytest.mark.asyncio
async def test_failed_embedding_preserves_published_index(tmp_path, monkeypatch):
    root, path, config = _corpus(tmp_path)
    async def success(self, texts):
        return [[1.0, 0.0] for _ in texts]
    monkeypatch.setattr(vector.EmbeddingProvider, "embed", success)
    await vector.build_index(config, root=root, path=path, network=NetworkConfig())
    before = _published_index(path)
    (root / "first.md").write_text(
        "---\ntype: concept\ntitle: First\n---\n# First\nChanged content",
        encoding="utf-8",
    )
    async def failure(self, texts):
        raise RuntimeError("embedding service unavailable")
    monkeypatch.setattr(vector.EmbeddingProvider, "embed", failure)
    with pytest.raises(RuntimeError, match="embedding service unavailable"):
        await vector.update_index(config, root=root, path=path, network=NetworkConfig())
    assert _published_index(path) == before


@pytest.mark.asyncio
async def test_source_race_during_update_keeps_original_database(tmp_path, monkeypatch):
    root, path, config = _corpus(tmp_path)
    async def embed(self, texts):
        return [[1.0, 0.0] for _ in texts]
    monkeypatch.setattr(vector.EmbeddingProvider, "embed", embed)
    await vector.build_index(config, root=root, path=path, network=NetworkConfig())
    before = _published_index(path)
    (root / "first.md").write_text(
        "---\ntype: concept\ntitle: First\n---\n# First\nFirst revision",
        encoding="utf-8",
    )
    original_read = vector._read_source
    reads = 0
    def changed_between_passes(root_path, relative, **kwargs):
        nonlocal reads
        if relative == "first.md":
            reads += 1
            if reads == 2:
                return "---\ntype: concept\n---\nSecond revision"
        return original_read(root_path, relative, **kwargs)
    monkeypatch.setattr(vector, "_read_source", changed_between_passes)
    with pytest.raises(ValueError, match="während der Indexierung"):
        await vector.update_index(config, root=root, path=path, network=NetworkConfig())
    assert reads == 2
    assert _published_index(path) == before
