from __future__ import annotations

import pytest
from cli_agent.platform import vision_analysis
from cli_agent.platform.os_operations import Workspace, WorkspaceError
from cli_agent.settings.config import McpServerConfig


def _workspace(tmp_path):
    return Workspace.from_directory(tmp_path, McpServerConfig(name="os"))


def test_combined_context(tmp_path):
    (tmp_path / "architecture.md").write_text("# System\nA connects to B.", encoding="utf-8")
    content = vision_analysis._build_reference_context(
        _workspace(tmp_path), context="Extra detail", context_path="architecture.md"
    )
    assert "Extra detail" in content
    assert "# System" in content


def test_large_markdown(tmp_path):
    text = "# Heading\n" + "Information.\n" * 10000
    (tmp_path / "large.md").write_text(text, encoding="utf-8")
    assert vision_analysis._read_markdown_context(_workspace(tmp_path), "large.md") == text


def test_context_path_guards(tmp_path):
    (tmp_path / "a.txt").write_text("Text", encoding="utf-8")
    (tmp_path / "a.md").write_text("Text", encoding="utf-8")
    (tmp_path / "alias.md").symlink_to("a.md")
    for path in ("../a.md", "a.txt", "alias.md"):
        with pytest.raises(WorkspaceError):
            vision_analysis._read_markdown_context(_workspace(tmp_path), path)


def test_context_size_limits(tmp_path):
    (tmp_path / "large.md").write_bytes(b"a" * vision_analysis.MAX_CONTEXT_FILE_BYTES)
    with pytest.raises(WorkspaceError):
        vision_analysis._build_reference_context(
            _workspace(tmp_path), context="more", context_path="large.md"
        )
    with pytest.raises(WorkspaceError):
        vision_analysis._build_reference_context(
            _workspace(tmp_path),
            context="a" * (vision_analysis.MAX_RAW_CONTEXT_BYTES + 1),
            context_path=None,
        )
