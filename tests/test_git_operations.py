from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from cli_agent.git_operations import GitWorkspace, GitWorkspaceError


def _git(directory: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(directory), *args], capture_output=True, text=True, check=True)
    return result.stdout.strip()


def _init_repo(path: Path) -> str:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init")
    _git(path, "config", "user.name", "Test User")
    _git(path, "config", "user.email", "test@example.org")
    (path / "a.txt").write_text("one\n", encoding="utf-8")
    _git(path, "add", "a.txt")
    _git(path, "commit", "-m", "initial")
    return _git(path, "rev-parse", "HEAD")


def test_discovers_repository_at_workspace_root(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    workspace = GitWorkspace.from_directory(tmp_path)
    assert [repo.relative_path for repo in workspace.repositories] == ["."]


def test_discovers_nested_repositories(tmp_path: Path) -> None:
    _init_repo(tmp_path / "app")
    _init_repo(tmp_path / "lib")
    workspace = GitWorkspace.from_directory(tmp_path)
    assert [repo.relative_path for repo in workspace.repositories] == ["app", "lib"]


def test_repository_above_workspace_is_not_discovered(tmp_path: Path) -> None:
    root = tmp_path / "outer"
    _init_repo(root)
    workspace_dir = root / "subdir"
    workspace_dir.mkdir()
    workspace = GitWorkspace.from_directory(workspace_dir)
    assert workspace.repositories == ()


def test_symlink_to_repository_outside_workspace_is_not_followed(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    _init_repo(outside)
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    try:
        (workspace_dir / "repo-link").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Symlinks are unavailable on this platform")
    workspace = GitWorkspace.from_directory(workspace_dir)
    assert workspace.repositories == ()


def test_worktree_with_git_metadata_outside_workspace_is_rejected(tmp_path: Path) -> None:
    source = tmp_path / "source"
    _init_repo(source)
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    worktree = workspace_dir / "nested-worktree"
    _git(source, "worktree", "add", str(worktree))
    workspace = GitWorkspace.from_directory(workspace_dir)
    assert workspace.repositories == ()


def test_status_diff_history_and_commit_files(tmp_path: Path) -> None:
    commit = _init_repo(tmp_path)
    workspace = GitWorkspace.from_directory(tmp_path)

    assert "branch" in workspace.git_status(".")
    history = json.loads(workspace.git_file_history(".", "a.txt"))
    assert history[0]["author"]["name"] == "Test User"
    assert history[0]["commit"]["id"] == commit

    files = json.loads(workspace.git_commit_files(".", commit))
    assert files == [{"status": "added", "path": "a.txt"}]

    (tmp_path / "a.txt").write_text("two\n", encoding="utf-8")
    assert "-one" in workspace.git_diff(".", "a.txt")
    assert "+two" in workspace.git_diff(".", "a.txt")


def test_commit_argument_rejects_revision_syntax(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    workspace = GitWorkspace.from_directory(tmp_path)
    with pytest.raises(GitWorkspaceError, match="Commit-Hash"):
        workspace.git_commit_files(".", "HEAD~1")


def test_repository_and_file_paths_reject_parent_escape(tmp_path: Path) -> None:
    _init_repo(tmp_path / "app")
    workspace = GitWorkspace.from_directory(tmp_path)
    with pytest.raises(GitWorkspaceError, match="'..'"):
        workspace.git_status("../app")
    with pytest.raises(GitWorkspaceError, match="'..'"):
        workspace.git_diff("app", "../secret.txt")
