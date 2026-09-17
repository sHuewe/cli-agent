from __future__ import annotations

from pathlib import Path

import pytest

from cli_agent.config import McpServerConfig
from cli_agent.os_operations import Workspace, WorkspaceError


def _workspace(path: Path) -> Workspace:
    return Workspace.from_directory(
        path,
        McpServerConfig(name="os", config={"allow_write_files": True}),
    )


def test_recursive_search_tools_reject_internal_symlink_root(tmp_path: Path) -> None:
    target = tmp_path / "target"
    target.mkdir()
    (target / "inside.txt").write_text("needle\n", encoding="utf-8")
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("Symlinks are unavailable on this platform")

    workspace = _workspace(tmp_path)

    with pytest.raises(WorkspaceError, match="Symlinks|Junctions"):
        workspace.search_text("alias", "needle")
    with pytest.raises(WorkspaceError, match="Symlinks|Junctions"):
        workspace.find_files("alias", "*.txt")


def test_search_text_rejects_multiline_literals(tmp_path: Path) -> None:
    (tmp_path / "a.txt").write_text("first\nsecond\n", encoding="utf-8")
    workspace = _workspace(tmp_path)

    with pytest.raises(WorkspaceError, match="Zeilenumbrüche"):
        workspace.search_text("a.txt", "first\nsecond")
    with pytest.raises(WorkspaceError, match="Zeilenumbrüche"):
        workspace.search_text("a.txt", "\r")
