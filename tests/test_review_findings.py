from __future__ import annotations

import json
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


def test_worktree_git_reads_reject_internal_symlink_parents(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    tracked_directory = repo / "tracked"
    tracked_directory.mkdir()
    (tracked_directory / "a.txt").write_text("committed\n", encoding="utf-8")
    _git(repo, "add", "tracked/a.txt")
    _git(repo, "commit", "-m", "add nested file")
    workspace = GitWorkspace.from_directory(tmp_path)

    (tracked_directory / "a.txt").unlink()
    tracked_directory.rmdir()
    target_directory = repo / "untracked"
    target_directory.mkdir()
    (target_directory / "a.txt").write_text("internal secret\n", encoding="utf-8")
    try:
        tracked_directory.symlink_to(target_directory, target_is_directory=True)
    except OSError:
        pytest.skip("Symlinks are unavailable on this platform")

    for operation in (
        lambda: workspace.git_status("repo"),
        lambda: workspace.git_diff("repo", "tracked/a.txt"),
        lambda: workspace.git_grep("repo", "internal secret"),
        lambda: workspace.git_blame("repo", "tracked/a.txt", 1, 1),
    ):
        with pytest.raises(GitWorkspaceError, match="Symlinks|Reparse"):
            operation()


@pytest.mark.parametrize(
    ("repository_name", "filename"),
    [(" repo", " report.txt"), ("repo ", "report.txt ")],
    ids=["leading-space", "trailing-space"],
)
def test_git_paths_preserve_edge_whitespace(
    tmp_path: Path,
    repository_name: str,
    filename: str,
) -> None:
    if os.name == "nt" and (repository_name.endswith(" ") or filename.endswith(" ")):
        pytest.skip("Windows normalizes trailing spaces in filesystem paths")
    repo = tmp_path / repository_name
    _init_repo(repo)
    (repo / filename).write_text("literal whitespace needle\n", encoding="utf-8")
    _git(repo, "add", "--", filename)
    _git(repo, "commit", "-m", "add whitespace path")
    workspace = GitWorkspace.from_directory(tmp_path)

    grep = json.loads(
        workspace.git_grep(
            repository_name,
            "literal whitespace needle",
            path=filename,
        )
    )
    blame = json.loads(workspace.git_blame(repository_name, filename, 1, 1))

    assert grep["matches"] == [
        {"path": filename, "line": 1, "text": "literal whitespace needle"}
    ]
    assert blame[0]["text"] == "literal whitespace needle"


def test_git_rejects_repository_path_redirected_after_discovery(
    tmp_path: Path,
) -> None:
    original = tmp_path / "original"
    replacement = tmp_path / "replacement"
    _init_repo(original)
    _init_repo(replacement)
    workspace = GitWorkspace.from_directory(tmp_path)
    original.rename(tmp_path / "archived")
    try:
        original.symlink_to(replacement, target_is_directory=True)
    except OSError:
        pytest.skip("Symlinks are unavailable on this platform")

    with pytest.raises(GitWorkspaceError, match="Symlinks|Reparse"):
        workspace.git_log("original")


@pytest.mark.skipif(os.name == "nt", reason="Windows paths must be valid Unicode")
def test_worktree_git_reads_reject_non_utf8_tracked_paths(tmp_path: Path) -> None:
    workspace_root = tmp_path / "workspace"
    repo = workspace_root / "repo"
    outside = tmp_path / "outside.txt"
    _init_repo(repo)
    outside.write_text("committed\n", encoding="utf-8")
    filename = os.fsdecode(b"invalid-\xff.txt")
    os.link(os.fsencode(outside), os.fsencode(repo) + b"/invalid-\xff.txt")
    _git(repo, "add", "--", filename)
    _git(repo, "commit", "-m", "add non-UTF-8 path")
    workspace = GitWorkspace.from_directory(workspace_root)
    outside.write_text("outside secret\n", encoding="utf-8")

    with pytest.raises(GitWorkspaceError, match="UTF-8"):
        workspace.git_diff("repo")


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="Platform has no FIFO support")
def test_worktree_git_reads_reject_special_files(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    workspace = GitWorkspace.from_directory(tmp_path)
    tracked = repo / "a.txt"
    tracked.unlink()
    os.mkfifo(tracked)

    with pytest.raises(GitWorkspaceError, match="reguläre"):
        workspace.git_status("repo")
