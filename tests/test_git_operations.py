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
    # GitWorkspace deliberately ignores system/global Git config. Keep the test
    # repository independent of the host's core.autocrlf setting as well, so
    # commits created by the fixture and reads performed by GitWorkspace use
    # the same line-ending semantics on Linux and Windows.
    _git(path, "config", "core.autocrlf", "false")
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

    assert workspace.git_status(".").startswith("## ")
    history = json.loads(workspace.git_file_history(".", "a.txt"))
    assert history[0]["author"]["name"] == "Test User"
    assert history[0]["commit"]["id"] == commit

    files = json.loads(workspace.git_commit_files(".", commit))
    assert files == [{"status": "added", "path": "a.txt"}]

    (tmp_path / "a.txt").write_text("two\n", encoding="utf-8")
    assert "-one" in workspace.git_diff(".", "a.txt")
    assert "+two" in workspace.git_diff(".", "a.txt")


def test_git_grep_returns_structured_literal_matches_and_ignores_untracked(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "a.txt").write_text("alpha needle\nneedle.*literal\n", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "tracked.txt").write_text("needle\n", encoding="utf-8")
    (tmp_path / "untracked.txt").write_text("needle\n", encoding="utf-8")
    _git(tmp_path, "add", "a.txt", "sub/tracked.txt")
    _git(tmp_path, "commit", "-m", "add searchable content")
    workspace = GitWorkspace.from_directory(tmp_path)

    result = json.loads(workspace.git_grep(".", "needle"))
    assert result["truncated"] is False
    assert [(item["path"], item["line"]) for item in result["matches"]] == [
        ("a.txt", 1),
        ("a.txt", 2),
        ("sub/tracked.txt", 1),
    ]
    assert all(item["path"] != "untracked.txt" for item in result["matches"])

    literal = json.loads(workspace.git_grep(".", "needle.*literal"))
    assert [item["line"] for item in literal["matches"]] == [2]


def test_git_grep_supports_path_limit_no_match_and_result_limit(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "a.txt").write_text("hit\nhit\n", encoding="utf-8")
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "b.txt").write_text("hit\nhit\n", encoding="utf-8")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "grep data")
    workspace = GitWorkspace.from_directory(tmp_path)

    limited_path = json.loads(workspace.git_grep(".", "hit", path="sub"))
    assert {item["path"] for item in limited_path["matches"]} == {"sub/b.txt"}

    no_match = json.loads(workspace.git_grep(".", "does-not-exist"))
    assert no_match == {"matches": [], "truncated": False}

    limited = json.loads(workspace.git_grep(".", "hit", max_results=3))
    assert len(limited["matches"]) == 3
    assert limited["truncated"] is True


def test_git_grep_validates_arguments_and_path_escape(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    workspace = GitWorkspace.from_directory(tmp_path)
    with pytest.raises(GitWorkspaceError, match="text darf nicht leer"):
        workspace.git_grep(".", "")
    with pytest.raises(GitWorkspaceError, match="max_results"):
        workspace.git_grep(".", "one", max_results=0)
    with pytest.raises(GitWorkspaceError, match="'..'"):
        workspace.git_grep(".", "one", path="../outside")


def test_git_blame_returns_structured_committed_and_uncommitted_lines(tmp_path: Path) -> None:
    commit = _init_repo(tmp_path)
    (tmp_path / "a.txt").write_text("one\ntwo\nthree\n", encoding="utf-8")
    _git(tmp_path, "add", "a.txt")
    _git(tmp_path, "commit", "-m", "three lines")
    committed = _git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "a.txt").write_text("changed\ntwo\nthree\n", encoding="utf-8")
    workspace = GitWorkspace.from_directory(tmp_path)

    result = json.loads(workspace.git_blame(".", "a.txt", 1, 2))
    assert len(result) == 2
    assert result[0]["line"] == 1
    assert result[0]["text"] == "changed"
    assert result[0]["uncommitted"] is True
    assert result[1]["line"] == 2
    assert result[1]["text"] == "two"
    assert result[1]["uncommitted"] is False
    assert result[1]["commit"]["id"] == committed
    assert result[1]["author"]["name"] == "Test User"
    assert result[1]["author"]["email"] == "test@example.org"
    assert "T" in result[1]["date"]
    assert commit != committed


def test_git_blame_validates_range_and_path_escape(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    workspace = GitWorkspace.from_directory(tmp_path)
    with pytest.raises(GitWorkspaceError, match="gemeinsam"):
        workspace.git_blame(".", "a.txt", start_line=1)
    with pytest.raises(GitWorkspaceError, match="Ungültiger"):
        workspace.git_blame(".", "a.txt", start_line=2, end_line=1)
    with pytest.raises(GitWorkspaceError, match="höchstens"):
        workspace.git_blame(".", "a.txt", start_line=1, end_line=201)
    with pytest.raises(GitWorkspaceError, match="'..'"):
        workspace.git_blame(".", "../a.txt", 1, 1)


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
