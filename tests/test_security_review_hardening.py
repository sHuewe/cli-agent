from __future__ import annotations

import asyncio
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

from cli_agent.agent import CliAgent
from cli_agent.config import McpServerConfig
from cli_agent.okf_mcp_server.repository import OkfRepository
from cli_agent.okf_mcp_server.repository_support import OkfRepositoryError
from cli_agent.os_operations import Workspace, WorkspaceError
from cli_agent.web_context import WebContext
from cli_agent.web_context_agent import WebContextCliAgent, _url_for_log


class RecordingModel:
    model = "test"
    base_url = "test://model"

    def __init__(self) -> None:
        self.calls = []

    async def chat(self, messages, tools, **kwargs):
        self.calls.append((messages.copy(), tools.copy()))
        return {"role": "assistant", "content": "ok"}


def _workspace(path: Path) -> Workspace:
    return Workspace.from_directory(
        path,
        McpServerConfig(name="os", config={"allow_write_files": True}),
    )


def _make_hardlink(source: Path, alias: Path) -> None:
    try:
        os.link(source, alias)
    except (OSError, NotImplementedError):
        pytest.skip("Hardlinks sind in dieser Testumgebung nicht verfügbar")


def test_workspace_read_rejects_hardlink_alias(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside.txt"
    outside.write_text("secret", encoding="utf-8")
    alias = tmp_path / "notes.txt"
    _make_hardlink(outside, alias)

    with pytest.raises(WorkspaceError, match="Hardlinks"):
        _workspace(tmp_path).read_file("notes.txt")


def test_workspace_write_rejects_existing_hardlink_target(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside.txt"
    outside.write_text("original", encoding="utf-8")
    alias = tmp_path / "notes.txt"
    _make_hardlink(outside, alias)

    with pytest.raises(WorkspaceError, match="Hardlinks"):
        _workspace(tmp_path).write_file("notes.txt", "replacement")
    assert outside.read_text(encoding="utf-8") == "original"


def test_workspace_copy_rejects_hardlink_source(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside.txt"
    outside.write_text("secret", encoding="utf-8")
    alias = tmp_path / "notes.txt"
    _make_hardlink(outside, alias)

    with pytest.raises(WorkspaceError, match="Hardlinks"):
        _workspace(tmp_path).copy_file("notes.txt", "copy.txt")
    assert not (tmp_path / "copy.txt").exists()


def test_okf_rejects_markdown_hardlink(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-outside.md"
    outside.write_text("---\ntype: concept\n---\nsecret", encoding="utf-8")
    alias = tmp_path / "concept.md"
    _make_hardlink(outside, alias)

    with pytest.raises(OkfRepositoryError, match="Hardlinks"):
        OkfRepository.from_directory(tmp_path).knowledge_read("concept.md")


def test_context_dump_rejects_symlink_directory(tmp_path: Path) -> None:
    outside = tmp_path.parent / f"{tmp_path.name}-dump-outside"
    outside.mkdir()
    link = tmp_path / ".cli-agent"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("Symlinks sind in dieser Testumgebung nicht verfügbar")

    agent = CliAgent(tmp_path, RecordingModel(), (), dump_llm_context=True)
    agent._exit_stack = SimpleNamespace()
    with pytest.raises(RuntimeError, match="Symlink|Reparse"):
        asyncio.run(agent.ask("Hallo"))
    assert list(outside.iterdir()) == []


def test_context_dump_rejects_dangling_symlink_file(tmp_path: Path) -> None:
    dump = tmp_path / ".cli-agent"
    dump.mkdir()
    outside = tmp_path.parent / f"{tmp_path.name}-outside-history.json"
    link = dump / "history.json"
    try:
        link.symlink_to(outside)
    except (OSError, NotImplementedError):
        pytest.skip("Symlinks sind in dieser Testumgebung nicht verfügbar")

    assert not link.exists()
    agent = CliAgent(tmp_path, RecordingModel(), (), dump_llm_context=True)
    agent._exit_stack = SimpleNamespace()
    with pytest.raises(RuntimeError, match="Symlink|Reparse"):
        asyncio.run(agent.ask("Hallo"))
    assert not outside.exists()


def test_context_dump_rejects_hardlinked_existing_dump_file(tmp_path: Path) -> None:
    dump = tmp_path / ".cli-agent"
    dump.mkdir()
    outside = tmp_path.parent / f"{tmp_path.name}-history.json"
    outside.write_text("original", encoding="utf-8")
    _make_hardlink(outside, dump / "history.json")

    agent = CliAgent(tmp_path, RecordingModel(), (), dump_llm_context=True)
    agent._exit_stack = SimpleNamespace()
    with pytest.raises(RuntimeError, match="Hardlinks"):
        asyncio.run(agent.ask("Hallo"))
    assert outside.read_text(encoding="utf-8") == "original"


def test_url_for_log_removes_query_and_fragment() -> None:
    value = _url_for_log("https://docs.example/path?token=secret&x=1#anchor")
    assert value == "https://docs.example/path"
    assert "secret" not in value
    assert "token" not in value


def test_web_context_log_does_not_include_query_secret(tmp_path: Path, monkeypatch, caplog) -> None:
    async def fake_fetch(url: str, **_: object) -> WebContext:
        return WebContext(
            requested_url=url,
            final_url="https://docs.example/final?sig=topsecret",
            title="Docs",
            content="content",
            fetched_at="2026-09-13T18:00:00+00:00",
        )

    monkeypatch.setattr("cli_agent.web_context_agent.fetch_web_context", fake_fetch)
    agent = WebContextCliAgent(tmp_path, RecordingModel(), ())
    agent._exit_stack = SimpleNamespace()
    caplog.set_level("INFO", logger="cli_agent.web_context")

    asyncio.run(agent.ask("add_web_context https://docs.example/start?token=supersecret"))

    assert "supersecret" not in caplog.text
    assert "topsecret" not in caplog.text
    assert "https://docs.example/start" in caplog.text
    assert "https://docs.example/final" in caplog.text
