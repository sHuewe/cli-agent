from __future__ import annotations

import pytest

from cli_agent.config import McpServerConfig
from cli_agent.os_operations import Workspace, WorkspaceError


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


@pytest.mark.parametrize("filename", [".env", ".env.production", "credentials.json", "server.pem"])
def test_read_file_rejects_sensitive_files(tmp_path, filename) -> None:
    (tmp_path / filename).write_text("secret", encoding="utf-8")
    with pytest.raises(WorkspaceError, match="Secret-/Credential"):
        _workspace(tmp_path).read_file(filename)


def test_read_file_rejects_oversized_text(tmp_path) -> None:
    (tmp_path / "large.txt").write_text("x" * 1_000_001, encoding="utf-8")
    with pytest.raises(WorkspaceError, match="Leselimit"):
        _workspace(tmp_path).read_file("large.txt")


def test_copy_file_rejects_sensitive_source(tmp_path) -> None:
    (tmp_path / "credentials.json").write_text("secret", encoding="utf-8")
    with pytest.raises(WorkspaceError, match="Secret-/Credential"):
        _workspace(tmp_path).copy_file("credentials.json", "copy.txt")


@pytest.mark.parametrize("relative_path", [".git/config", ".cli-agent/history.json", "service.log"])
def test_read_file_rejects_sensitive_project_artifacts(tmp_path, relative_path) -> None:
    file_path = tmp_path / relative_path
    file_path.parent.mkdir(parents=True, exist_ok=True)
    file_path.write_text("sensitive", encoding="utf-8")
    with pytest.raises(WorkspaceError, match="Secret-/Credential"):
        _workspace(tmp_path).read_file(relative_path)


@pytest.mark.parametrize("path", [r"C:\\outside.txt", r"D:relative.txt"])
def test_workspace_rejects_windows_drive_paths(tmp_path, path: str) -> None:
    with pytest.raises(WorkspaceError, match="relativ"):
        _workspace(tmp_path).read_file(path)


@pytest.mark.parametrize("relative_path", [".env", ".env.local", ".git/config", ".cli-agent/history.json", "server.pem", "service.log"])
def test_write_file_rejects_sensitive_destinations(tmp_path, relative_path) -> None:
    path = tmp_path / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    with pytest.raises(WorkspaceError, match="Ändern"):
        _workspace(tmp_path).write_file(relative_path, "replacement")
    assert not path.exists()


@pytest.mark.parametrize("relative_path", [".env", ".git/HEAD", ".cli-agent/history.json", "service.log"])
def test_delete_file_rejects_sensitive_files(tmp_path, relative_path) -> None:
    path = tmp_path / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("original", encoding="utf-8")
    with pytest.raises(WorkspaceError, match="Ändern"):
        _workspace(tmp_path).delete_file(relative_path)
    assert path.read_text(encoding="utf-8") == "original"


@pytest.mark.parametrize("destination", [".env", ".git/config", ".cli-agent/result.txt", "service.log"])
def test_copy_file_rejects_sensitive_destinations(tmp_path, destination) -> None:
    (tmp_path / "source.txt").write_text("safe", encoding="utf-8")
    path = tmp_path / destination
    path.parent.mkdir(parents=True, exist_ok=True)
    with pytest.raises(WorkspaceError, match="Ändern"):
        _workspace(tmp_path).copy_file("source.txt", destination)
    assert not path.exists()


@pytest.mark.parametrize("directory", [".git", ".cli-agent", ".ssh", ".aws"])
def test_make_directory_rejects_sensitive_directories(tmp_path, directory) -> None:
    with pytest.raises(WorkspaceError, match="Ändern"):
        _workspace(tmp_path).make_directory(directory)
    assert not (tmp_path / directory).exists()
