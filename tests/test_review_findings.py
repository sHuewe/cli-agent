from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from cli_agent.agent import CliAgent
from cli_agent.config import McpServerConfig
from cli_agent.git_operations import GitWorkspace, GitWorkspaceError
from cli_agent.ollama import OllamaClient


def _agent(tmp_path: Path) -> CliAgent:
    return CliAgent(
        tmp_path,
        OllamaClient(base_url="http://localhost:11434", model="test"),
        (),
    )


def _git(directory: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(directory), *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init")
    _git(path, "config", "user.name", "Test User")
    _git(path, "config", "user.email", "test@example.org")
    _git(path, "config", "core.autocrlf", "false")
    (path / "a.txt").write_text("one\n", encoding="utf-8")
    _git(path, "add", "a.txt")
    _git(path, "commit", "-m", "initial")


def test_built_in_move_file_requires_approval_when_os_write_is_enabled(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    writable = McpServerConfig(
        name="os",
        built_in=True,
        config={"allow_write_files": True},
    )
    read_only = McpServerConfig(name="os", built_in=True)

    assert agent._requires_approval(writable, "move_file", "os__move_file") is True
    assert agent._requires_approval(read_only, "move_file", "os__move_file") is False


def test_git_environment_disables_lazy_fetch_and_drops_inherited_git_values(monkeypatch) -> None:
    monkeypatch.setenv("GIT_ASKPASS", "untrusted-helper")
    monkeypatch.setenv("GIT_NO_LAZY_FETCH", "0")

    environment = GitWorkspace._environment()

    assert environment["GIT_NO_LAZY_FETCH"] == "1"
    assert "GIT_ASKPASS" not in environment


def test_name_status_parser_preserves_delimiter_characters_in_paths() -> None:
    output = (
        "A\x00has\ttab.txt\x00"
        "M\x00has\nnewline.txt\x00"
        "R100\x00old\"name\\file.txt\x00new\tname.txt\x00"
    )

    assert GitWorkspace._name_status_records(output) == [
        {"status": "added", "path": "has\ttab.txt"},
        {"status": "modified", "path": "has\nnewline.txt"},
        {
            "status": "renamed",
            "old_path": 'old"name\\file.txt',
            "path": "new\tname.txt",
        },
    ]


def test_repository_with_local_content_filter_is_not_exposed(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _git(repo, "config", "filter.evil.clean", "arbitrary-command")
    (repo / ".gitattributes").write_text("*.txt filter=evil\n", encoding="utf-8")

    workspace = GitWorkspace.from_directory(tmp_path)

    assert workspace.repositories == ()


def test_repository_with_local_config_include_is_not_exposed(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    included = tmp_path / "outside-config"
    included.write_text("[filter \"evil\"]\n\tclean = arbitrary-command\n", encoding="utf-8")
    _git(repo, "config", "include.path", str(included))

    workspace = GitWorkspace.from_directory(tmp_path)

    assert workspace.repositories == ()


def test_worktree_git_reads_reject_tracked_hardlinks(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    outside = tmp_path / "outside.txt"
    _init_repo(repo)
    outside.write_text("outside secret\n", encoding="utf-8")
    tracked = repo / "a.txt"
    tracked.unlink()
    try:
        os.link(outside, tracked)
    except OSError:
        pytest.skip("Hardlinks are unavailable on this platform")

    workspace = GitWorkspace.from_directory(tmp_path)
    assert [item.relative_path for item in workspace.repositories] == ["repo"]

    with pytest.raises(GitWorkspaceError, match="Hardlinks"):
        workspace.git_status("repo")
    with pytest.raises(GitWorkspaceError, match="Hardlinks"):
        workspace.git_diff("repo", "a.txt")
    with pytest.raises(GitWorkspaceError, match="Hardlinks"):
        workspace.git_grep("repo", "outside secret")
    with pytest.raises(GitWorkspaceError, match="Hardlinks"):
        workspace.git_blame("repo", "a.txt", 1, 1)
