from __future__ import annotations

import os
import subprocess
from pathlib import Path

import pytest

from cli_agent.git_operations import GitWorkspaceError
from cli_agent.git_runtime import RuntimeGitWorkspace


def _git(directory: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(directory), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _init_repo(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init")
    _git(path, "config", "user.name", "Test User")
    _git(path, "config", "user.email", "test@example.org")
    _git(path, "config", "core.autocrlf", "false")
    (path / "a.txt").write_bytes(b"one\n")
    _git(path, "add", "a.txt")
    _git(path, "commit", "-m", "initial")


def test_current_branch_does_not_walk_git_metadata_tree(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_repo(tmp_path)
    workspace = RuntimeGitWorkspace.from_directory(tmp_path)

    def fail_if_called(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("recursive metadata scan must not run")

    monkeypatch.setattr(RuntimeGitWorkspace, "_validate_metadata_tree", fail_if_called)
    assert workspace.git_current_branch(".") == _git(tmp_path, "branch", "--show-current")


def test_status_does_not_scan_every_tracked_worktree_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    _init_repo(tmp_path)
    workspace = RuntimeGitWorkspace.from_directory(tmp_path)

    def fail_if_called(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("status must not validate file contents")

    monkeypatch.setattr(workspace, "_validate_worktree_files", fail_if_called)
    assert workspace.git_status(".").startswith("## ")


def test_content_returning_tools_keep_worktree_alias_validation(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    outside = tmp_path / "outside.txt"
    _init_repo(repo)
    outside.write_bytes(b"outside secret\n")
    tracked = repo / "a.txt"
    tracked.unlink()
    try:
        os.link(outside, tracked)
    except OSError:
        pytest.skip("Hardlinks are unavailable on this platform")

    workspace = RuntimeGitWorkspace.from_directory(tmp_path)
    # Status reports metadata only and therefore does not need to read/return
    # the aliased file content.
    assert workspace.git_status("repo").startswith("## ")
    for operation in (
        lambda: workspace.git_diff("repo", "a.txt"),
        lambda: workspace.git_grep("repo", "outside secret", path="a.txt"),
        lambda: workspace.git_blame("repo", "a.txt", 1, 1),
    ):
        with pytest.raises(GitWorkspaceError, match="Hardlinks"):
            operation()


def test_repository_redirect_after_discovery_is_rejected(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    replacement = tmp_path / "replacement"
    _init_repo(repo)
    _init_repo(replacement)
    workspace = RuntimeGitWorkspace.from_directory(tmp_path)
    repo.rename(tmp_path / "archived")
    try:
        repo.symlink_to(replacement, target_is_directory=True)
    except OSError:
        pytest.skip("Symlinks are unavailable on this platform")

    with pytest.raises(GitWorkspaceError, match="Symlinks|Reparse"):
        workspace.git_current_branch("repo")


def test_symlinked_git_control_path_is_rejected(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    workspace = RuntimeGitWorkspace.from_directory(tmp_path)
    head = repo / ".git" / "HEAD"
    saved = repo / ".git" / "HEAD.saved"
    head.rename(saved)
    try:
        head.symlink_to(saved)
    except OSError:
        saved.rename(head)
        pytest.skip("Symlinks are unavailable on this platform")

    with pytest.raises(GitWorkspaceError, match="Symlinks|Reparse"):
        workspace.git_current_branch("repo")


def test_repository_content_filter_remains_rejected(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    _git(repo, "config", "filter.evil.clean", "arbitrary-command")
    (repo / ".gitattributes").write_text("*.txt filter=evil\n", encoding="utf-8")

    assert RuntimeGitWorkspace.from_directory(tmp_path).repositories == ()
