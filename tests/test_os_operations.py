from __future__ import annotations

import pytest
from pathlib import Path

from cli_agent import os_operations

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


def test_list_files_empty_sorted_and_relative(tmp_path):
    workspace = _workspace(tmp_path)
    assert workspace.list_files(".") == "(Ordner ist leer)"
    (tmp_path / "nested").mkdir()
    (tmp_path / "nested" / "z.txt").write_text("z")
    (tmp_path / "nested" / "A.txt").write_text("a")
    (tmp_path / "nested" / "folder").mkdir()
    assert workspace.list_files("nested").splitlines() == [
        "directory\tnested/folder", "file\tnested/A.txt", "file\tnested/z.txt",
    ]


def test_copy_overwrite_and_delete(tmp_path):
    workspace = _workspace(tmp_path)
    source = tmp_path / "source.txt"
    source.write_bytes(b"original\r\n")
    assert "copied.txt" in workspace.copy_file("source.txt", "copied.txt")
    assert (tmp_path / "copied.txt").read_bytes() == source.read_bytes()
    source.write_bytes(b"changed\n")
    workspace.copy_file("source.txt", "copied.txt")
    assert (tmp_path / "copied.txt").read_bytes() == b"changed\n"
    assert "copied.txt" in workspace.delete_file("copied.txt")
    assert not (tmp_path / "copied.txt").exists()
    assert source.exists()


def test_make_nested_directory_is_idempotent(tmp_path):
    workspace = _workspace(tmp_path)
    assert "Ordner erstellt" in workspace.make_directory("one/two")
    (tmp_path / "one/two/keep.txt").write_text("keep")
    assert "existiert bereits" in workspace.make_directory("one/two")
    assert (tmp_path / "one/two/keep.txt").read_text() == "keep"


@pytest.mark.parametrize("operation,args", [
    ("list_files", ("file.txt",)), ("read_file", ("folder",)),
    ("delete_file", ("folder",)), ("copy_file", ("folder", "copy.txt")),
    ("copy_file", ("file.txt", "folder")), ("write_file", ("folder", "text")),
    ("make_directory", ("file.txt",)),
])
def test_operations_reject_wrong_path_kind(tmp_path, operation, args):
    (tmp_path / "file.txt").write_text("original")
    (tmp_path / "folder").mkdir()
    with pytest.raises(WorkspaceError, match="kein"):
        getattr(_workspace(tmp_path), operation)(*args)
    assert (tmp_path / "file.txt").read_text() == "original"
    assert (tmp_path / "folder").is_dir()


@pytest.mark.parametrize("operation,args", [
    ("copy_file", ("file.txt", "missing/copy.txt")),
    ("write_file", ("missing/new.txt", "text")),
])
def test_file_mutations_require_existing_parent(tmp_path, operation, args):
    (tmp_path / "file.txt").write_text("original")
    with pytest.raises(WorkspaceError, match="Zielordner"):
        getattr(_workspace(tmp_path), operation)(*args)
    assert not (tmp_path / "missing").exists()


@pytest.mark.parametrize("path", ["", "  ", None])
def test_workspace_rejects_empty_paths(tmp_path, path):
    with pytest.raises(WorkspaceError, match="leer"):
        _workspace(tmp_path).resolve_path(path)


def test_missing_workspace_is_rejected(tmp_path):
    with pytest.raises(WorkspaceError, match="Workspace existiert nicht"):
        _workspace(tmp_path / "missing")


def test_symlink_escape_is_rejected_for_reads_and_writes(tmp_path):
    root = tmp_path / "workspace"
    root.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("original")
    (root / "link.txt").symlink_to(outside)
    workspace = _workspace(root)
    with pytest.raises(WorkspaceError, match="außerhalb"):
        workspace.read_file("link.txt")
    with pytest.raises(WorkspaceError, match="außerhalb"):
        workspace.write_file("link.txt", "changed")
    assert outside.read_text() == "original"


def test_empty_and_invalid_utf8_files(tmp_path):
    path = tmp_path / "text.txt"
    path.write_bytes(b"")
    assert _workspace(tmp_path).read_file(path.name) == "(Empty file)"
    path.write_bytes(b"\xff\xfe")
    with pytest.raises(WorkspaceError, match="UTF-8"):
        _workspace(tmp_path).read_file(path.name)


def test_write_preserves_cr_line_endings(tmp_path):
    path = tmp_path / "text.txt"
    path.write_bytes(b"one\rtwo\r")
    _workspace(tmp_path).write_file(path.name, "one\nchanged\n")
    assert path.read_bytes() == b"one\rchanged\r"


@pytest.mark.parametrize("operation,args,target,method,message", [
    ("read_file", ("file.txt",), Path, "read_text", "gelesen"),
    ("write_file", ("file.txt", "replacement"), Path, "write_text", "geschrieben"),
    ("delete_file", ("file.txt",), Path, "unlink", "gelöscht"),
    ("copy_file", ("file.txt", "copy.txt"), os_operations.shutil, "copy2", "kopiert"),
    ("make_directory", ("new",), Path, "mkdir", "erstellt"),
    ("read_file", ("file.txt",), os_operations, "regular_file_has_multiple_links", "Dateimetadaten"),
])
def test_io_failures_are_workspace_errors(tmp_path, monkeypatch, operation, args, target, method, message):
    path = tmp_path / "file.txt"
    path.write_bytes(b"original")

    def denied(*args, **kwargs):
        raise PermissionError("access denied")

    with monkeypatch.context() as patch:
        patch.setattr(target, method, denied)
        with pytest.raises(WorkspaceError, match=message):
            getattr(_workspace(tmp_path), operation)(*args)
    assert path.read_bytes() == b"original"


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


@pytest.mark.parametrize(
    "filename",
    [
        "main.go",
        "index.jsx",
        "module.mjs",
        "component.vue",
        "app.php",
        "task.rb",
        "lib.rs",
        "Program.cs",
        "script.ps1",
        "schema.graphql",
        "deployment.tf",
        "styles.scss",
        "README.adoc",
        "data.tsv",
        ".editorconfig",
        ".dockerignore",
        "Makefile",
    ],
)
def test_read_file_accepts_common_text_file_types(tmp_path, filename) -> None:
    (tmp_path / filename).write_text("readable content", encoding="utf-8")

    assert _workspace(tmp_path).read_file(filename) == "readable content"


def test_read_file_still_rejects_unknown_file_types(tmp_path) -> None:
    (tmp_path / "artifact.unknown").write_text("text content", encoding="utf-8")

    with pytest.raises(WorkspaceError, match="Dateityp"):
        _workspace(tmp_path).read_file("artifact.unknown")


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


def test_write_file_rejects_workspace_internal_symlink_alias(tmp_path) -> None:
    target = tmp_path / "important.txt"
    target.write_text("original", encoding="utf-8")
    (tmp_path / "harmless.txt").symlink_to(target)

    with pytest.raises(WorkspaceError, match="Symlinks oder Junctions"):
        _workspace(tmp_path).write_file("harmless.txt", "changed")

    assert target.read_text(encoding="utf-8") == "original"


def test_delete_file_rejects_workspace_internal_symlink_alias(tmp_path) -> None:
    target = tmp_path / "important.txt"
    target.write_text("original", encoding="utf-8")
    alias = tmp_path / "harmless.txt"
    alias.symlink_to(target)

    with pytest.raises(WorkspaceError, match="Symlinks oder Junctions"):
        _workspace(tmp_path).delete_file("harmless.txt")

    assert target.read_text(encoding="utf-8") == "original"
    assert alias.is_symlink()


def test_copy_file_rejects_workspace_internal_symlink_source(tmp_path) -> None:
    target = tmp_path / "important.txt"
    target.write_text("secret-content", encoding="utf-8")
    (tmp_path / "harmless.txt").symlink_to(target)

    with pytest.raises(WorkspaceError, match="Symlinks oder Junctions"):
        _workspace(tmp_path).copy_file("harmless.txt", "copy.txt")

    assert not (tmp_path / "copy.txt").exists()


def test_copy_file_rejects_workspace_internal_symlink_destination(tmp_path) -> None:
    source = tmp_path / "source.txt"
    source.write_text("replacement", encoding="utf-8")
    target = tmp_path / "important.txt"
    target.write_text("original", encoding="utf-8")
    (tmp_path / "harmless.txt").symlink_to(target)

    with pytest.raises(WorkspaceError, match="Symlinks oder Junctions"):
        _workspace(tmp_path).copy_file("source.txt", "harmless.txt")

    assert target.read_text(encoding="utf-8") == "original"


def test_mutations_reject_symlinked_parent_directory(tmp_path) -> None:
    real_dir = tmp_path / "real"
    real_dir.mkdir()
    alias_dir = tmp_path / "alias"
    alias_dir.symlink_to(real_dir, target_is_directory=True)
    (real_dir / "existing.txt").write_text("original", encoding="utf-8")

    workspace = _workspace(tmp_path)

    with pytest.raises(WorkspaceError, match="Symlinks oder Junctions"):
        workspace.write_file("alias/new.txt", "new")
    with pytest.raises(WorkspaceError, match="Symlinks oder Junctions"):
        workspace.delete_file("alias/existing.txt")
    with pytest.raises(WorkspaceError, match="Symlinks oder Junctions"):
        workspace.make_directory("alias/new-dir")

    assert not (real_dir / "new.txt").exists()
    assert (real_dir / "existing.txt").read_text(encoding="utf-8") == "original"
    assert not (real_dir / "new-dir").exists()


def test_list_files_hides_sensitive_entries(tmp_path) -> None:
    (tmp_path / "visible.txt").write_text("visible", encoding="utf-8")
    (tmp_path / ".env.production").write_text("SECRET=value", encoding="utf-8")
    (tmp_path / ".git").mkdir()
    (tmp_path / ".git" / "config").write_text("sensitive", encoding="utf-8")

    listing = _workspace(tmp_path).list_files(".")

    assert "visible.txt" in listing
    assert ".env.production" not in listing
    assert ".git" not in listing


def test_list_files_rejects_sensitive_directory(tmp_path) -> None:
    (tmp_path / ".git").mkdir()

    with pytest.raises(WorkspaceError, match="geschützten"):
        _workspace(tmp_path).list_files(".git")


def test_active_workspace_config_is_protected_across_read_operations(tmp_path) -> None:
    config_path = tmp_path / "custom-agent.toml"
    config_path.write_text('Authorization = "secret-sentinel"\n', encoding="utf-8")
    (tmp_path / "normal.toml").write_text('value = "secret-sentinel"\n', encoding="utf-8")
    workspace = Workspace.from_directory(
        tmp_path,
        McpServerConfig(name="os", config={"allow_write_files": True}),
        protected_paths=(config_path,),
    )

    with pytest.raises(WorkspaceError, match="geschützten"):
        workspace.read_file("custom-agent.toml")
    with pytest.raises(WorkspaceError, match="geschützten"):
        workspace.file_info("custom-agent.toml")
    with pytest.raises(WorkspaceError, match="geschützten"):
        workspace.search_text("custom-agent.toml", "secret-sentinel")

    found = workspace.find_files(".", "*.toml")
    assert "normal.toml" in found
    assert "custom-agent.toml" not in found

    searched = workspace.search_text(".", "secret-sentinel")
    assert "normal.toml" in searched
    assert "custom-agent.toml" not in searched


def test_active_workspace_config_is_hidden_from_listing_and_copy(tmp_path) -> None:
    config_path = tmp_path / "custom-agent.toml"
    config_path.write_text("secret", encoding="utf-8")
    workspace = Workspace.from_directory(
        tmp_path,
        McpServerConfig(name="os", config={"allow_write_files": True}),
        protected_paths=(config_path,),
    )

    assert "custom-agent.toml" not in workspace.list_files(".")
    with pytest.raises(WorkspaceError, match="geschützten"):
        workspace.copy_file("custom-agent.toml", "copy.toml")
    assert not (tmp_path / "copy.toml").exists()


@pytest.mark.parametrize(
    "operation,args",
    [
        ("write_file", ("custom-agent.toml", "changed")),
        ("delete_file", ("custom-agent.toml",)),
        ("move_file", ("custom-agent.toml", "moved.toml")),
        ("copy_file", ("normal.toml", "custom-agent.toml")),
    ],
)
def test_active_workspace_config_rejects_mutations(tmp_path, operation, args) -> None:
    config_path = tmp_path / "custom-agent.toml"
    config_path.write_text("original", encoding="utf-8")
    (tmp_path / "normal.toml").write_text("normal", encoding="utf-8")
    workspace = Workspace.from_directory(
        tmp_path,
        McpServerConfig(name="os", config={"allow_write_files": True}),
        protected_paths=(config_path,),
    )

    with pytest.raises(WorkspaceError, match="geschützten"):
        getattr(workspace, operation)(*args)

    assert config_path.read_text(encoding="utf-8") == "original"


def test_protected_config_outside_workspace_does_not_affect_workspace(tmp_path) -> None:
    workspace_dir = tmp_path / "workspace"
    workspace_dir.mkdir()
    outside_config = tmp_path / "config.toml"
    outside_config.write_text("outside", encoding="utf-8")
    (workspace_dir / "config.toml").write_text("project file", encoding="utf-8")

    workspace = Workspace.from_directory(
        workspace_dir,
        McpServerConfig(name="os", config={"allow_write_files": True}),
        protected_paths=(outside_config,),
    )

    assert workspace.read_file("config.toml") == "project file"
