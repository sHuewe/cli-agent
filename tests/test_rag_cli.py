from __future__ import annotations

import argparse
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from cli_agent.rag_cli import build_parser, init_vector_db, main


def test_rag_help_and_required_arguments(capsys):
    parser = build_parser()
    assert "init-vector-db" in parser.format_help()
    with pytest.raises(SystemExit) as error:
        parser.parse_args(["init-vector-db"])
    assert error.value.code == 2
    assert "required" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_init_vector_db_calls_shared_indexer(tmp_path, monkeypatch, capsys):
    repo = tmp_path / "okf"
    repo.mkdir()
    (repo / "index.md").write_text("# Knowledge", encoding="utf-8")
    index = tmp_path / "vectors.sqlite"
    called = AsyncMock(return_value=2)
    monkeypatch.setattr("cli_agent.rag_cli.build_index", called)
    monkeypatch.setattr("cli_agent.rag_cli.load_admin_config",
                        lambda: argparse.Namespace(network=object(), model_credentials=()))
    args = build_parser().parse_args([
        "init-vector-db", "--okf-repo", str(repo),
        "--embedding-model", "embeddinggemma",
        "--sqlite-file", str(index),
    ])
    assert await init_vector_db(args) == 2
    kwargs = called.await_args.kwargs
    assert kwargs["root"] == repo.resolve()
    assert kwargs["path"] == index.resolve()
    assert called.await_args.args[0].mode == "okf"
    assert called.await_args.args[0].provider == "ollama"
    assert "2 OKF-Concepts" in capsys.readouterr().out


@pytest.mark.asyncio
async def test_openai_provider_can_be_selected(tmp_path, monkeypatch):
    repo = tmp_path / "okf"
    repo.mkdir()
    (repo / "index.md").write_text("# Knowledge", encoding="utf-8")
    called = AsyncMock(return_value=1)
    monkeypatch.setattr("cli_agent.rag_cli.build_index", called)
    monkeypatch.setattr("cli_agent.rag_cli.load_admin_config",
                        lambda: argparse.Namespace(network=object(), model_credentials=()))
    args = build_parser().parse_args([
        "init-vector-db", "--okf-repo", str(repo), "--embedding-model", "embed-1",
        "--sqlite-file", str(tmp_path / "index.sqlite"),
        "--provider", "openai", "--base-url", "https://example.org/v1",
        "--api-key-env", "EMBEDDING_KEY",
    ])
    await init_vector_db(args)
    vector = called.await_args.args[0]
    assert vector.provider == "openai"
    assert vector.api_key_env == "EMBEDDING_KEY"
    assert vector.base_url == "https://example.org/v1"


@pytest.mark.asyncio
@pytest.mark.parametrize("extras", [
    ["--provider", "openai"],
    ["--api-key-env", "SECRET"],
    ["--max-files", "0"],
])
async def test_invalid_options_fail_before_indexing(tmp_path, extras):
    repo = tmp_path / "okf"
    repo.mkdir()
    args = build_parser().parse_args([
        "init-vector-db", "--okf-repo", str(repo),
        "--embedding-model", "embeddinggemma",
        "--sqlite-file", str(tmp_path / "index.sqlite"), *extras,
    ])
    with pytest.raises(ValueError):
        await init_vector_db(args)


@pytest.mark.asyncio
async def test_index_inside_okf_root_rejected(tmp_path):
    repo = tmp_path / "okf"
    repo.mkdir()
    args = build_parser().parse_args([
        "init-vector-db", "--okf-repo", str(repo),
        "--embedding-model", "embeddinggemma",
        "--sqlite-file", str(repo / "vectors.sqlite"),
    ])
    with pytest.raises(ValueError, match="außerhalb"):
        await init_vector_db(args)


def test_main_prints_usage_error_without_traceback(capsys):
    with pytest.raises(SystemExit) as error:
        main(["init-vector-db", "--okf-repo", "/nonexistent",
              "--embedding-model", "embed", "--sqlite-file", "vectors.sqlite"])
    assert error.value.code == 1
    assert "cli-agent-rag:" in capsys.readouterr().err
