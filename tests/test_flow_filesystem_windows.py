"""Windows inventory regression checks; run on Windows CI."""
import os
from pathlib import Path

import pytest

from cli_agent.flow_filesystem_operations import inventory_directory


pytestmark = pytest.mark.skipif(os.name != "nt", reason="Windows-only inventory tests")


def test_windows_inventory_nonrecursive_and_filters(tmp_path: Path) -> None:
    folder = tmp_path / "concepts"
    folder.mkdir()
    (folder / "index.md").write_text("index", encoding="utf-8")
    (folder / "concept.md").write_text("abc", encoding="utf-8")
    (folder / "notes.txt").write_text("notes", encoding="utf-8")
    (folder / "sub").mkdir()
    (folder / "sub" / "nested.md").write_text("nested", encoding="utf-8")
    result = inventory_directory(
        tmp_path, "concepts", extensions=(".md",), exclude_names=("index.md",)
    )
    assert result["directory"] == "concepts"
    assert [entry["path"] for entry in result["files"]] == ["concepts/concept.md"]
    assert result["files"][0]["size_bytes"] == 3
    assert result["directories"] == [{"name": "sub", "path": "concepts/sub"}]


def test_windows_inventory_recursive(tmp_path: Path) -> None:
    folder = tmp_path / "concepts"
    (folder / "sub").mkdir(parents=True)
    (folder / "sub" / "nested.md").write_text("x", encoding="utf-8")
    result = inventory_directory(tmp_path, "concepts", recursive=True)
    assert [entry["path"] for entry in result["files"]] == ["concepts/sub/nested.md"]


def test_windows_inventory_rejects_reparse_entry(tmp_path: Path) -> None:
    folder = tmp_path / "concepts"
    folder.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    link = folder / "link"
    try:
        link.symlink_to(outside, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("Symlink creation requires Windows developer mode or privileges")
    with pytest.raises(ValueError, match="Reparse Point"):
        inventory_directory(tmp_path, "concepts")


def test_windows_inventory_limit_counts_filtered_entries(tmp_path: Path) -> None:
    folder = tmp_path / "concepts"
    folder.mkdir()
    (folder / "a.txt").write_text("a", encoding="utf-8")
    (folder / "b.txt").write_text("b", encoding="utf-8")
    with pytest.raises(ValueError, match="max_entries"):
        inventory_directory(tmp_path, "concepts", extensions=(".md",), max_entries=1)
