from __future__ import annotations

from cli_agent.config import McpServerConfig
from cli_agent.os_operations import Workspace


def _workspace(tmp_path):
    return Workspace.from_directory(
        tmp_path,
        McpServerConfig(
            name="os",
            config={"allow_write_files": True},
        ),
    )


def test_write_file_preserves_lf_line_endings(tmp_path) -> None:
    file_path = tmp_path / "Example.java"
    file_path.write_bytes(b"line1\nline2\n")
    workspace = _workspace(tmp_path)

    workspace.write_file("Example.java", "line1\nchanged\n")

    assert file_path.read_bytes() == b"line1\nchanged\n"


def test_write_file_preserves_crlf_line_endings(tmp_path) -> None:
    file_path = tmp_path / "Example.java"
    file_path.write_bytes(b"line1\r\nline2\r\n")
    workspace = _workspace(tmp_path)

    workspace.write_file("Example.java", "line1\nchanged\n")

    assert file_path.read_bytes() == b"line1\r\nchanged\r\n"


def test_write_file_prefers_dominant_lf_in_mixed_file(tmp_path) -> None:
    file_path = tmp_path / "Example.java"
    file_path.write_bytes(b"line1\nline2\nline3\r\n")
    workspace = _workspace(tmp_path)

    workspace.write_file("Example.java", "line1\nchanged\nline3\n")

    assert file_path.read_bytes() == b"line1\nchanged\nline3\n"


def test_write_file_prefers_dominant_crlf_in_mixed_file(tmp_path) -> None:
    file_path = tmp_path / "Example.java"
    file_path.write_bytes(b"line1\r\nline2\r\nline3\n")
    workspace = _workspace(tmp_path)

    workspace.write_file("Example.java", "line1\nchanged\nline3\n")

    assert file_path.read_bytes() == b"line1\r\nchanged\r\nline3\r\n"


def test_write_file_prefers_lf_on_mixed_line_ending_tie(tmp_path) -> None:
    file_path = tmp_path / "Example.java"
    file_path.write_bytes(b"line1\nline2\r\n")
    workspace = _workspace(tmp_path)

    workspace.write_file("Example.java", "line1\nchanged\n")

    assert file_path.read_bytes() == b"line1\nchanged\n"


def test_write_file_uses_lf_for_new_files(tmp_path) -> None:
    workspace = _workspace(tmp_path)

    workspace.write_file("Example.java", "line1\r\nline2\r\n")

    assert (tmp_path / "Example.java").read_bytes() == b"line1\nline2\n"
