"""Regression tests for the knowledge indexing CLI entry point."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from cli_agent.context import knowledge_cli


@pytest.mark.asyncio
async def test_index_requires_vector_configuration(monkeypatch, tmp_path):
    monkeypatch.setattr(
        knowledge_cli,
        "load_config",
        lambda _: SimpleNamespace(knowledge_vector=None),
    )
    with pytest.raises(ValueError, match=r"knowledge\.vector"):
        await knowledge_cli._run_index(tmp_path / "config.toml")


@pytest.mark.asyncio
async def test_index_passes_resolved_roots_and_security_configuration(
    monkeypatch, tmp_path, capsys
):
    config_file = tmp_path / "settings" / "config.toml"
    config_file.parent.mkdir()
    root = tmp_path / "knowledge"
    root.mkdir()
    db = tmp_path / "index" / "vectors.sqlite"
    vector = object()
    network = object()
    credentials = object()
    okf = SimpleNamespace(repository="../knowledge", max_read_bytes=12345)
    config = SimpleNamespace(knowledge_vector=vector, okf=okf)
    monkeypatch.setattr(knowledge_cli, "load_config", lambda path: config)
    monkeypatch.setattr(
        knowledge_cli, "load_admin_config",
        lambda: SimpleNamespace(network=network, model_credentials=credentials),
    )
    calls = {}

    def fake_source_root(value, *, okf_root, config_file):
        calls["source_root"] = (value, okf_root, config_file)
        return root

    def fake_db_path(value, *, config_file):
        calls["db_path"] = (value, config_file)
        return db

    async def fake_build_index(value, **kwargs):
        calls["build_index"] = (value, kwargs)
        return 3

    monkeypatch.setattr(knowledge_cli, "source_root", fake_source_root)
    monkeypatch.setattr(knowledge_cli, "db_path", fake_db_path)
    monkeypatch.setattr(knowledge_cli, "build_index", fake_build_index)

    await knowledge_cli._run_index(config_file)

    assert calls["source_root"] == (
        vector, (config_file.parent / "../knowledge").resolve(), config_file
    )
    assert calls["db_path"] == (vector, config_file)
    assert calls["build_index"] == (
        vector,
        {
            "root": root, "path": db, "network": network,
            "credential_rules": credentials, "okf_max_read_bytes": 12345,
        },
    )
    assert "3 Quelldokumente" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_index_rejects_database_inside_source_tree(monkeypatch, tmp_path):
    config_file = tmp_path / "config.toml"
    root = tmp_path / "source"
    root.mkdir()
    vector = object()
    config = SimpleNamespace(
        knowledge_vector=vector,
        okf=SimpleNamespace(repository="source", max_read_bytes=100),
    )
    monkeypatch.setattr(knowledge_cli, "load_config", lambda _: config)
    monkeypatch.setattr(
        knowledge_cli, "load_admin_config",
        lambda: SimpleNamespace(network=None, model_credentials=None),
    )
    monkeypatch.setattr(knowledge_cli, "source_root", lambda *a, **k: root)
    monkeypatch.setattr(
        knowledge_cli, "db_path", lambda *a, **k: root / "index.sqlite"
    )
    async def must_not_run(*args, **kwargs):
        pytest.fail("Indexing must not start with a source-tree database")
    monkeypatch.setattr(knowledge_cli, "build_index", must_not_run)

    with pytest.raises(ValueError, match="außerhalb"):
        await knowledge_cli._run_index(config_file)


@pytest.mark.parametrize(
    ("repository", "expected"),
    [
        ("../data", "data"),
        (".", "settings"),
    ],
)
def test_source_root_placeholder_resolves_relative_paths(tmp_path, repository, expected):
    config_file = tmp_path / "settings" / "config.toml"
    config_file.parent.mkdir()
    resolved = knowledge_cli.source_root_placeholder(repository, config_file)
    assert resolved == (tmp_path / expected).resolve()


def test_source_root_placeholder_preserves_absolute_path(tmp_path):
    target = tmp_path / "knowledge"
    assert (
        knowledge_cli.source_root_placeholder(str(target), tmp_path / "config.toml")
        == target.resolve()
    )


def test_main_resolves_explicit_config_and_dispatches(monkeypatch, tmp_path):
    config_file = tmp_path / "nested" / ".." / "config.toml"
    calls = []
    async def fake_index(path):
        calls.append(path)
    monkeypatch.setattr(knowledge_cli, "_run_index", fake_index)
    monkeypatch.setattr(
        sys, "argv", ["cli-agent-knowledge", "index", "--config", str(config_file)]
    )
    knowledge_cli.main()
    assert calls == [config_file.expanduser().resolve()]


def test_main_uses_default_config(monkeypatch, tmp_path):
    config_file = tmp_path / "config.toml"
    calls = []
    async def fake_index(path):
        calls.append(path)
    monkeypatch.setattr(knowledge_cli, "_run_index", fake_index)
    monkeypatch.setattr(knowledge_cli, "default_config_file", lambda: config_file)
    monkeypatch.setattr(sys, "argv", ["cli-agent-knowledge", "index"])
    knowledge_cli.main()
    assert calls == [config_file.resolve()]
