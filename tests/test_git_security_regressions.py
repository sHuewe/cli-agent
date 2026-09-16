from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from cli_agent.git_operations import GitWorkspace, GitWorkspaceError


def _git(directory: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(directory), *args],
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


def _init_repo(path: Path, content: bytes = b"one\n") -> str:
    path.mkdir(parents=True, exist_ok=True)
    _git(path, "init")
    _git(path, "config", "user.name", "Test User")
    _git(path, "config", "user.email", "test@example.org")
    _git(path, "config", "core.autocrlf", "false")
    (path / "a.txt").write_bytes(content)
    _git(path, "add", "a.txt")
    _git(path, "commit", "-m", "initial")
    return _git(path, "rev-parse", "HEAD")


def _symlink(link: Path, target: Path, *, directory: bool = False) -> None:
    try:
        link.symlink_to(target, target_is_directory=directory)
    except OSError:
        pytest.skip("Symlinks are unavailable on this platform")


def test_blame_never_executes_repository_textconv(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    workspace = GitWorkspace.from_directory(tmp_path)
    # A harmless marker proves whether Git actually ran the configured command.
    (tmp_path / ".gitattributes").write_text("a.txt diff=evil\n", encoding="utf-8")
    _git(tmp_path, "config", "diff.evil.textconv", "echo executed > textconv-ran; cat")

    result = json.loads(workspace.git_blame(".", "a.txt"))

    assert not (tmp_path / "textconv-ran").exists()
    assert [item["text"] for item in result] == ["one"]


@pytest.mark.parametrize(
    "content, expected",
    [
        (b"one\rtwo\r", ["one\rtwo\r"]),
        (b"one\rtwo\nthree\n", ["one\rtwo", "three"]),
        (b"one\r\ntwo\r\n", ["one\r", "two\r"]),
        ("one\v\f\x1c\x1d\x1e\x85\u2028\u2029two\n".encode(),
         ["one\v\f\x1c\x1d\x1e\x85\u2028\u2029two"]),
        (b"\n", [""]),
        (b"one", ["one"]),
    ],
)
def test_blame_preserves_source_line_separators(
    tmp_path: Path, content: bytes, expected: list[str]
) -> None:
    _init_repo(tmp_path, content)
    workspace = GitWorkspace.from_directory(tmp_path)

    result = json.loads(workspace.git_blame(".", "a.txt"))

    assert [item["text"] for item in result] == expected
    assert [item["line"] for item in result] == list(range(1, len(expected) + 1))


@pytest.mark.parametrize("committed_empty", [True, False])
def test_blame_returns_empty_list_for_empty_tracked_file(
    tmp_path: Path, committed_empty: bool
) -> None:
    _init_repo(tmp_path, b"" if committed_empty else b"one\n")
    workspace = GitWorkspace.from_directory(tmp_path)
    (tmp_path / "a.txt").write_bytes(b"")

    assert json.loads(workspace.git_blame(".", "a.txt")) == []
    with pytest.raises(GitWorkspaceError):
        workspace.git_blame(".", "a.txt", 1, 1)


def test_blame_does_not_hide_missing_or_untracked_empty_files(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    workspace = GitWorkspace.from_directory(tmp_path)
    (tmp_path / "empty.txt").write_bytes(b"")
    for path in ("empty.txt", "missing.txt"):
        with pytest.raises(GitWorkspaceError):
            workspace.git_blame(".", path)


def test_blame_keeps_default_limit_and_accepts_explicit_range(tmp_path: Path) -> None:
    _init_repo(tmp_path, b"line\n" * 202)
    workspace = GitWorkspace.from_directory(tmp_path)
    with pytest.raises(GitWorkspaceError, match="mehr als 200"):
        workspace.git_blame(".", "a.txt")
    result = json.loads(workspace.git_blame(".", "a.txt", 200, 202))
    assert [item["line"] for item in result] == [200, 201, 202]


@pytest.mark.parametrize(
    "method, args",
    [
        ("git_status", ()),
        ("git_current_branch", ()),
        ("git_branches", ()),
        ("git_diff", ()),
        ("git_diff_staged", ()),
        ("git_log", ()),
        ("git_file_history", ("a.txt",)),
        ("git_grep", ("one",)),
        ("git_blame", ("a.txt",)),
    ],
)
def test_git_tools_reject_gitdir_replacement_after_discovery(
    tmp_path: Path, method: str, args: tuple[str, ...]
) -> None:
    repo = tmp_path / "workspace"
    outside = tmp_path / "outside"
    _init_repo(repo)
    _init_repo(outside, b"outside secret\n")
    workspace = GitWorkspace.from_directory(repo)
    (repo / ".git").rename(repo / "original-git")
    (repo / ".git").write_text(f"gitdir: {outside / '.git'}\n", encoding="utf-8")

    with pytest.raises(GitWorkspaceError, match="außerhalb"):
        getattr(workspace, method)(".", *args)


@pytest.mark.parametrize("target", ["root", "objects", "common"])
def test_git_log_revalidates_all_repository_directories(
    tmp_path: Path, target: str
) -> None:
    repo = tmp_path / "workspace" / "repo"
    outside = tmp_path / "outside"
    _init_repo(repo)
    _init_repo(outside, b"outside secret\n")
    candidate = repo
    if target == "common":
        candidate = repo.parent / "linked"
        _git(repo, "worktree", "add", str(candidate))
    workspace = GitWorkspace.from_directory(repo.parent)
    if target == "root":
        repo.rename(repo.with_name("original"))
        _symlink(repo, outside, directory=True)
    elif target == "objects":
        objects = repo / ".git" / "objects"
        objects.rename(objects.with_name("original-objects"))
        _symlink(objects, outside / ".git" / "objects", directory=True)
    else:
        common_file = repo / ".git" / "worktrees" / "linked" / "commondir"
        common_file.write_text(str(outside / ".git") + "\n", encoding="utf-8")

    with pytest.raises(GitWorkspaceError, match="außerhalb|Symlinks"):
        workspace.git_log(candidate.name)


def test_git_revalidates_object_alternates_after_discovery(tmp_path: Path) -> None:
    repo = tmp_path / "workspace"
    outside = tmp_path / "outside"
    _init_repo(repo)
    _init_repo(outside, b"outside secret\n")
    workspace = GitWorkspace.from_directory(repo)
    (repo / ".git" / "objects" / "info" / "alternates").write_text(
        str(outside / ".git" / "objects") + "\n", encoding="utf-8", newline="\n"
    )

    with pytest.raises(GitWorkspaceError, match="außerhalb"):
        workspace.git_log(".")


def test_git_rejects_transitive_outside_object_alternates(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    repo = root / "repo"
    alternate = root / "alternate"
    outside = tmp_path / "outside"
    _init_repo(repo)
    _init_repo(outside, b"outside secret\n")
    (alternate / "info").mkdir(parents=True)
    (repo / ".git" / "objects" / "info" / "alternates").write_text(
        "../../../alternate\n", encoding="utf-8", newline="\n"
    )
    (alternate / "info" / "alternates").write_text(
        str(outside / ".git" / "objects") + "\n", encoding="utf-8", newline="\n"
    )

    assert GitWorkspace.from_directory(root).repositories == ()


def test_git_allows_internal_alternate_cycles(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    alternate = tmp_path / "alternate"
    commit = _init_repo(repo)
    (alternate / "info").mkdir(parents=True)
    (repo / ".git" / "objects" / "info" / "alternates").write_text(
        "../../../alternate\n", encoding="utf-8", newline="\n"
    )
    (alternate / "info" / "alternates").write_text(
        "../repo/.git/objects\n", encoding="utf-8", newline="\n"
    )
    workspace = GitWorkspace.from_directory(tmp_path)

    assert json.loads(workspace.git_log("repo"))[0]["id"] == commit


def test_git_grep_preserves_embedded_carriage_return(tmp_path: Path) -> None:
    _init_repo(tmp_path, b"one\rtwo\n")
    result = json.loads(GitWorkspace.from_directory(tmp_path).git_grep(".", "one"))
    assert result["matches"] == [{"path": "a.txt", "line": 1, "text": "one\rtwo"}]


@pytest.mark.skipif(os.name == "nt", reason="Windows filenames cannot contain CR")
def test_git_commit_files_preserves_carriage_return_in_filename(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    (tmp_path / "has\rcarriage.txt").write_bytes(b"text\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "add file")
    commit = _git(tmp_path, "rev-parse", "HEAD")
    files = json.loads(GitWorkspace.from_directory(tmp_path).git_commit_files(".", commit))
    assert {"status": "added", "path": "has\rcarriage.txt"} in files


def test_blame_does_not_read_configured_external_ignore_revs_file(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    secret = tmp_path / "secret.txt"
    secret.write_text("OUTSIDE_SENTINEL\n", encoding="utf-8")
    workspace = GitWorkspace.from_directory(repo)
    _git(repo, "config", "blame.ignoreRevsFile", str(secret))

    with pytest.raises(GitWorkspaceError, match="Blame-Ignore") as error:
        workspace.git_blame(".", "a.txt")
    assert "OUTSIDE_SENTINEL" not in str(error.value)
    assert GitWorkspace.from_directory(repo).repositories == ()


def test_git_rejects_symlinked_object_shards(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    _init_repo(repo)
    commit = _init_repo(outside, b"outside secret\n")
    workspace = GitWorkspace.from_directory(repo)
    for shard in (outside / ".git" / "objects").iterdir():
        target = repo / ".git" / "objects" / shard.name
        if len(shard.name) == 2 and not target.exists():
            _symlink(target, shard, directory=True)

    with pytest.raises(GitWorkspaceError, match="Symlinks"):
        workspace.git_commit_info(".", commit)
    assert GitWorkspace.from_directory(repo).repositories == ()


@pytest.mark.parametrize("metadata", ["index", "HEAD", "refs/heads/alias"])
@pytest.mark.parametrize("alias", ["symlink", "hardlink"])
def test_git_rejects_metadata_file_aliases(
    tmp_path: Path, metadata: str, alias: str
) -> None:
    repo = tmp_path / "repo"
    commit = _init_repo(repo)
    workspace = GitWorkspace.from_directory(repo)
    target = repo / ".git" / metadata
    outside = tmp_path / "outside-metadata"
    outside.write_bytes(target.read_bytes() if target.exists() else (commit + "\n").encode())
    target.unlink(missing_ok=True)
    if alias == "symlink":
        _symlink(target, outside)
    else:
        try:
            os.link(outside, target)
        except OSError:
            pytest.skip("Hardlinks are unavailable on this platform")

    with pytest.raises(GitWorkspaceError, match="Symlinks|Hardlinks"):
        workspace.git_log(".")


def test_git_grep_does_not_recurse_into_unvalidated_submodules(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    _init_repo(repo)
    _init_repo(outside, b"outside secret\n")
    _git(repo, "-c", "protocol.file.allow=always", "submodule", "add", outside.as_uri(), "sub")
    _git(repo, "commit", "-am", "add submodule")
    workspace = GitWorkspace.from_directory(repo)
    _git(repo, "config", "submodule.recurse", "true")
    (repo / "sub" / ".git").write_text(f"gitdir: {outside / '.git'}\n", encoding="utf-8")

    assert json.loads(workspace.git_grep(".", "outside secret")) == {
        "matches": [], "truncated": False,
    }
    with pytest.raises(GitWorkspaceError, match="außerhalb"):
        workspace.git_grep("sub", "outside secret")


@pytest.mark.skipif(os.name == "nt", reason="Windows filenames cannot contain '*' or ':'")
@pytest.mark.parametrize("filename", ["[ab].txt", "*.txt", ":(glob)*.txt"])
def test_git_grep_treats_filenames_as_literal_paths(tmp_path: Path, filename: str) -> None:
    _init_repo(tmp_path)
    (tmp_path / filename).write_bytes(b"needle\n")
    _git(tmp_path, "add", ".")
    _git(tmp_path, "commit", "-m", "literal path")

    result = json.loads(GitWorkspace.from_directory(tmp_path).git_grep(".", "needle"))
    assert result["matches"] == [{"path": filename, "line": 1, "text": "needle"}]


def test_blame_rejects_external_worktree_mailmap(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    workspace = GitWorkspace.from_directory(repo)
    outside = tmp_path / "outside-mailmap"
    outside.write_text("Outside Secret <secret@example.org> <test@example.org>\n", encoding="utf-8")
    _symlink(repo / ".mailmap", outside)

    with pytest.raises(GitWorkspaceError, match="außerhalb"):
        workspace.git_blame(".", "a.txt")


def test_git_supports_valid_internal_linked_worktree(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    commit = _init_repo(repo)
    linked = tmp_path / "linked"
    _git(repo, "worktree", "add", str(linked))
    workspace = GitWorkspace.from_directory(tmp_path)

    assert json.loads(workspace.git_log("linked"))[0]["id"] == commit
    assert json.loads(workspace.git_blame("linked", "a.txt"))[0]["text"] == "one"


def test_git_supports_safe_gitdir_relocation_after_discovery(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    commit = _init_repo(repo)
    workspace = GitWorkspace.from_directory(tmp_path)
    metadata = tmp_path / "metadata"
    (repo / ".git").rename(metadata)
    (repo / ".git").write_text("gitdir: ../metadata\n", encoding="utf-8")

    assert json.loads(workspace.git_log("repo"))[0]["id"] == commit


def test_git_rejects_filters_added_after_discovery(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    workspace = GitWorkspace.from_directory(tmp_path)
    _git(tmp_path, "config", "filter.evil.clean", "echo executed > filter-ran; cat")
    (tmp_path / ".gitattributes").write_text("a.txt filter=evil\n", encoding="utf-8")

    with pytest.raises(GitWorkspaceError, match="Content-Filter"):
        workspace.git_status(".")
    assert not (tmp_path / "filter-ran").exists()


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="Platform has no FIFO support")
def test_git_rejects_metadata_fifo_without_opening_it(tmp_path: Path) -> None:
    _init_repo(tmp_path)
    workspace = GitWorkspace.from_directory(tmp_path)
    config = tmp_path / ".git" / "config"
    config.unlink()
    os.mkfifo(config)

    with pytest.raises(GitWorkspaceError, match="Nicht reguläre"):
        workspace.git_log(".")


def test_git_rejects_excessive_metadata_before_reading_history(tmp_path: Path, monkeypatch) -> None:
    _init_repo(tmp_path)
    workspace = GitWorkspace.from_directory(tmp_path)
    monkeypatch.setattr("cli_agent.git_operations.MAX_METADATA_ENTRIES", 1)

    with pytest.raises(GitWorkspaceError, match="Zu viele Git-Metadateneinträge"):
        workspace.git_log(".")


def test_git_rejects_excessive_alternates(tmp_path: Path, monkeypatch) -> None:
    repo = tmp_path / "repo"
    _init_repo(repo)
    alternate = tmp_path / "alternate"
    alternate.mkdir()
    workspace = GitWorkspace.from_directory(tmp_path)
    (repo / ".git" / "objects" / "info" / "alternates").write_text(
        str(alternate) + "\n", encoding="utf-8", newline="\n"
    )
    monkeypatch.setattr("cli_agent.git_operations.MAX_OBJECT_DIRECTORIES", 1)

    with pytest.raises(GitWorkspaceError, match="Zu viele Git-Alternate"):
        workspace.git_log("repo")


def test_git_environment_disallows_all_transports(monkeypatch) -> None:
    monkeypatch.setenv("GIT_ALLOW_PROTOCOL", "file:ssh:https")
    monkeypatch.setenv("GIT_LITERAL_PATHSPECS", "0")
    monkeypatch.setenv("GIT_TERMINAL_PROMPT", "1")
    env = GitWorkspace._environment()
    assert env["GIT_ALLOW_PROTOCOL"] == ""
    assert env["GIT_LITERAL_PATHSPECS"] == "1"
    assert env["GIT_TERMINAL_PROMPT"] == "0"


@pytest.mark.skipif(os.name == "nt", reason="Windows filenames cannot contain quotes")
def test_git_rejects_quoted_alternate_path_ambiguity(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    outside = tmp_path / "outside"
    _init_repo(repo)
    _init_repo(outside, b"outside secret\n")
    workspace = GitWorkspace.from_directory(repo)
    objects = repo / ".git" / "objects"
    quoted = '"' + str(outside / ".git" / "objects") + '"'
    # A literal-path validator sees this decoy directory inside the workspace,
    # whereas Git interprets the same bytes as a quoted absolute outside path.
    (objects / quoted).mkdir(parents=True)
    (objects / "info" / "alternates").write_text(quoted + "\n", encoding="utf-8")

    with pytest.raises(GitWorkspaceError, match="C-quotierte"):
        workspace.git_log(".")
