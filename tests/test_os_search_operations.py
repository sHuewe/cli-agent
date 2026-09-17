from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from cli_agent import os_operations
from cli_agent.config import McpServerConfig
from cli_agent.os_operations import Workspace, WorkspaceError


def _workspace(path: Path) -> Workspace:
    return Workspace.from_directory(
        path,
        McpServerConfig(name="os", config={"allow_write_files": True}),
    )


def test_search_text_finds_literal_text_recursively_and_returns_lines(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("first\nneedle here\n", encoding="utf-8")
    (tmp_path / "src" / "b.txt").write_text("needle.*literal\n", encoding="utf-8")
    (tmp_path / "binary.bin").write_bytes(b"needle")

    result = json.loads(_workspace(tmp_path).search_text(".", "needle"))

    assert result["truncated"] is False
    assert sorted(
        (item["path"], item["line"], item["text"]) for item in result["matches"]
    ) == [
        ("src/a.py", 2, "needle here"),
        ("src/b.txt", 1, "needle.*literal"),
    ]
    literal = json.loads(_workspace(tmp_path).search_text(".", "needle.*literal"))
    assert [item["path"] for item in literal["matches"]] == ["src/b.txt"]


def test_search_text_supports_file_scope_limit_and_argument_validation(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("hit\nhit\nhit\n", encoding="utf-8")
    workspace = _workspace(tmp_path)

    result = json.loads(workspace.search_text("a.txt", "hit", max_results=2))
    assert len(result["matches"]) == 2
    assert result["truncated"] is True

    exact = json.loads(workspace.search_text("a.txt", "hit", max_results=3))
    assert len(exact["matches"]) == 3
    assert exact["truncated"] is False

    with pytest.raises(WorkspaceError, match="text darf nicht leer"):
        workspace.search_text(".", "")
    with pytest.raises(WorkspaceError, match="max_results"):
        workspace.search_text(".", "hit", 0)
    with pytest.raises(WorkspaceError, match="'..'"):
        workspace.search_text("../outside", "hit")


def test_search_and_find_do_not_expose_sensitive_or_oversized_files(tmp_path: Path) -> None:
    (tmp_path / "visible.txt").write_text("needle", encoding="utf-8")
    (tmp_path / ".env").write_text("needle", encoding="utf-8")
    (tmp_path / "service.log").write_text("needle", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("needle", encoding="utf-8")
    (tmp_path / "large.txt").write_text("needle" + "x" * 1_000_001, encoding="utf-8")
    workspace = _workspace(tmp_path)

    search = json.loads(workspace.search_text(".", "needle"))
    assert [item["path"] for item in search["matches"]] == ["visible.txt"]

    found = json.loads(workspace.find_files(".", "*"))
    assert "visible.txt" in found["files"]
    assert ".env" not in found["files"]
    assert "service.log" not in found["files"]
    assert ".git/config" not in found["files"]


def test_find_files_matches_names_and_workspace_relative_paths(tmp_path: Path) -> None:
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "one.py").write_text("", encoding="utf-8")
    (tmp_path / "src" / "two.txt").write_text("", encoding="utf-8")
    workspace = _workspace(tmp_path)

    by_name = json.loads(workspace.find_files(".", "*.py"))
    assert by_name == {"files": ["src/one.py"], "truncated": False}

    by_path = json.loads(workspace.find_files(".", "src/*.txt"))
    assert by_path == {"files": ["src/two.txt"], "truncated": False}

    exact = json.loads(workspace.find_files(".", "*", max_results=2))
    assert len(exact["files"]) == 2
    assert exact["truncated"] is False

    with pytest.raises(WorkspaceError, match="pattern darf nicht leer"):
        workspace.find_files(".", "")
    with pytest.raises(WorkspaceError, match="'..'"):
        workspace.find_files("../outside", "*.txt")


def test_file_info_returns_bounded_metadata_and_rejects_sensitive_paths(tmp_path: Path) -> None:
    file_path = tmp_path / "a.txt"
    file_path.write_text("abc", encoding="utf-8")
    (tmp_path / "folder").mkdir()
    workspace = _workspace(tmp_path)

    file_info = json.loads(workspace.file_info("a.txt"))
    assert file_info["path"] == "a.txt"
    assert file_info["type"] == "file"
    assert file_info["size_bytes"] == 3
    assert "T" in file_info["modified"]

    dir_info = json.loads(workspace.file_info("folder"))
    assert dir_info["type"] == "directory"
    assert dir_info["size_bytes"] is None

    (tmp_path / ".env").write_text("secret", encoding="utf-8")
    with pytest.raises(WorkspaceError, match="Secret-/Credential"):
        workspace.file_info(".env")
    with pytest.raises(WorkspaceError, match="'..'"):
        workspace.file_info("../outside.txt")


def test_move_file_renames_and_can_replace_regular_destination(tmp_path: Path) -> None:
    (tmp_path / "source.txt").write_text("new", encoding="utf-8")
    (tmp_path / "target.txt").write_text("old", encoding="utf-8")
    workspace = _workspace(tmp_path)

    result = workspace.move_file("source.txt", "target.txt")

    assert "target.txt" in result
    assert not (tmp_path / "source.txt").exists()
    assert (tmp_path / "target.txt").read_text(encoding="utf-8") == "new"


def test_move_file_rejects_parent_escape_sensitive_and_missing_parent(tmp_path: Path) -> None:
    (tmp_path / "source.txt").write_text("original", encoding="utf-8")
    workspace = _workspace(tmp_path)

    with pytest.raises(WorkspaceError, match="'..'"):
        workspace.move_file("source.txt", "../outside.txt")
    with pytest.raises(WorkspaceError, match="Ändern"):
        workspace.move_file("source.txt", ".env")
    with pytest.raises(WorkspaceError, match="Zielordner"):
        workspace.move_file("source.txt", "missing/target.txt")
    assert (tmp_path / "source.txt").read_text(encoding="utf-8") == "original"


def test_move_file_rejects_source_symlink_even_when_target_is_inside_workspace(tmp_path: Path) -> None:
    target = tmp_path / "target.txt"
    target.write_text("original", encoding="utf-8")
    try:
        (tmp_path / "link.txt").symlink_to(target)
    except OSError:
        pytest.skip("Symlinks are unavailable on this platform")

    workspace = _workspace(tmp_path)
    with pytest.raises(WorkspaceError, match="Symlinks|Junctions"):
        workspace.move_file("link.txt", "renamed.txt")

    assert target.read_text(encoding="utf-8") == "original"
    assert (tmp_path / "link.txt").is_symlink()
    assert not (tmp_path / "renamed.txt").exists()


def test_recursive_tools_do_not_follow_directory_symlink_outside_workspace(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "inside.txt").write_text("needle", encoding="utf-8")
    (outside / "secret.txt").write_text("needle", encoding="utf-8")
    try:
        (root / "external").symlink_to(outside, target_is_directory=True)
    except OSError:
        pytest.skip("Symlinks are unavailable on this platform")

    workspace = _workspace(root)
    search = json.loads(workspace.search_text(".", "needle"))
    found = json.loads(workspace.find_files(".", "*.txt"))

    assert [item["path"] for item in search["matches"]] == ["inside.txt"]
    assert found["files"] == ["inside.txt"]
    with pytest.raises(WorkspaceError, match="außerhalb"):
        workspace.search_text("external", "needle")
    with pytest.raises(WorkspaceError, match="außerhalb"):
        workspace.find_files("external", "*.txt")
    with pytest.raises(WorkspaceError, match="außerhalb"):
        workspace.file_info("external/secret.txt")


def test_move_file_cannot_use_symlink_to_escape_workspace(tmp_path: Path) -> None:
    root = tmp_path / "workspace"
    outside = tmp_path / "outside"
    root.mkdir()
    outside.mkdir()
    (root / "source.txt").write_text("inside", encoding="utf-8")
    (outside / "outside.txt").write_text("outside", encoding="utf-8")
    try:
        (root / "outside-link").symlink_to(outside, target_is_directory=True)
        (root / "source-link.txt").symlink_to(outside / "outside.txt")
    except OSError:
        pytest.skip("Symlinks are unavailable on this platform")

    workspace = _workspace(root)
    with pytest.raises(WorkspaceError, match="außerhalb|Symlinks|Junctions"):
        workspace.move_file("source.txt", "outside-link/moved.txt")
    with pytest.raises(WorkspaceError, match="außerhalb|Symlinks|Junctions"):
        workspace.move_file("source-link.txt", "moved.txt")
    assert (root / "source.txt").read_text(encoding="utf-8") == "inside"
    assert (outside / "outside.txt").read_text(encoding="utf-8") == "outside"


def test_new_read_tools_skip_or_reject_hardlinked_files(tmp_path: Path) -> None:
    source = tmp_path / "source.txt"
    alias = tmp_path / "alias.txt"
    source.write_text("needle", encoding="utf-8")
    try:
        os.link(source, alias)
    except OSError:
        pytest.skip("Hardlinks are unavailable on this platform")
    workspace = _workspace(tmp_path)

    search = json.loads(workspace.search_text(".", "needle"))
    found = json.loads(workspace.find_files(".", "*.txt"))
    assert search["matches"] == []
    assert found["files"] == []
    with pytest.raises(WorkspaceError, match="Hardlinks"):
        workspace.file_info("source.txt")
    with pytest.raises(WorkspaceError, match="Hardlinks"):
        workspace.move_file("source.txt", "moved.txt")


@pytest.mark.parametrize("prefix, needle, suffix", [
    ("x" * 6000, "needle", "y" * 6000),
    ("x" * 6000, "needle", "end"),
    ("", "needle", "y" * 6000),
    ("x" * 3998, "needle", "y" * 6000),
    ("ä" * 6000, "gesucht😀", "ö" * 6000),
    ("x" * 6000, "n" * 4096, "y" * 6000),
], ids=["middle", "end", "start", "old-cutoff", "unicode", "long-query"])
def test_search_text_keeps_match_in_long_line_excerpt(
    tmp_path: Path, prefix: str, needle: str, suffix: str
) -> None:
    line = prefix + needle + suffix
    (tmp_path / "long.txt").write_text("first\n" + line + "\n", encoding="utf-8")

    result = json.loads(_workspace(tmp_path).search_text("long.txt", needle))

    assert result["truncated"] is False
    match = result["matches"][0]
    assert match["line"] == 2
    assert match["column"] == len(prefix) + 1
    assert needle in match["text"]
    assert match["text_truncated"] is True
    assert len(match["text"]) <= max(4000, len(needle))
    start = match["text_start_column"] - 1
    assert match["text"] == line[start:start + len(match["text"])]
    assert match["text"][match["column"] - match["text_start_column"]:].startswith(needle)


def test_search_text_short_line_returns_first_match_column(tmp_path: Path) -> None:
    (tmp_path / "short.txt").write_bytes(b"first needle, second needle\n")

    result = json.loads(_workspace(tmp_path).search_text("short.txt", "needle"))

    assert result["matches"] == [{
        "path": "short.txt", "line": 1, "column": 7,
        "text": "first needle, second needle",
    }]


def test_search_text_discards_partial_matches_from_invalid_utf8_file(
    tmp_path: Path,
) -> None:
    (tmp_path / "invalid.txt").write_bytes(
        b"needle must be discarded\n" + b"x" * 9_000 + b"\xff"
    )

    result = json.loads(_workspace(tmp_path).search_text("invalid.txt", "needle"))

    assert result == {"matches": [], "truncated": False}


def test_search_text_stops_at_file_scan_budget(tmp_path: Path, monkeypatch) -> None:
    for index in range(4):
        (tmp_path / f"{index}.txt").write_text("no match", encoding="utf-8")
    monkeypatch.setattr(os_operations, "MAX_SEARCH_SCAN_FILES", 2)

    result = json.loads(_workspace(tmp_path).search_text(".", "needle"))

    assert result == {
        "matches": [],
        "truncated": True,
        "truncation_reason": "scan_budget",
    }


def test_search_text_stops_at_byte_scan_budget(tmp_path: Path, monkeypatch) -> None:
    (tmp_path / "a.txt").write_text("a" * 8, encoding="utf-8")
    (tmp_path / "b.txt").write_text("b" * 8, encoding="utf-8")
    monkeypatch.setattr(os_operations, "MAX_SEARCH_SCAN_BYTES", 10)

    result = json.loads(_workspace(tmp_path).search_text(".", "needle"))

    assert result == {
        "matches": [],
        "truncated": True,
        "truncation_reason": "scan_budget",
    }


def test_find_files_stops_at_scan_budget(tmp_path: Path, monkeypatch) -> None:
    for index in range(4):
        (tmp_path / f"{index}.txt").write_text("x", encoding="utf-8")
    monkeypatch.setattr(os_operations, "MAX_FIND_SCAN_FILES", 2)

    result = json.loads(_workspace(tmp_path).find_files(".", "*.py"))

    assert result == {
        "files": [],
        "truncated": True,
        "truncation_reason": "scan_budget",
    }
