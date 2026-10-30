from pathlib import Path

import pytest

from cli_agent.config import McpServerConfig
from cli_agent.mcp_result_artifacts import McpResultArtifacts
from cli_agent.os_operations import AllowedPath, Workspace, WorkspaceError
from cli_agent.mcp_path_policy import read_path_policy, write_path_policy


def _ws(root, allowed=(), protected=(), write=True):
    return Workspace.from_directory(
        root, McpServerConfig(name="test", config={"allow_write_files": write}),
        allowed_paths=allowed, protected_paths=protected,
    )


def test_session_artifact_isolation_and_access(tmp_path):
    first = McpResultArtifacts(tmp_path, ())
    second = McpResultArtifacts(tmp_path, ())
    file_a = first.save('{"rows":[1]}', structured=True)
    file_b = second.save('{"rows":[2]}', structured=True)
    assert file_a.startswith(".cli-agent/mcp-data-")
    assert file_b.startswith(".cli-agent/mcp-data-")
    assert first.session_directory != second.session_directory
    os_workspace = _ws(tmp_path)
    assert ".cli-agent" not in os_workspace.list_files(".")
    for file in (file_a, file_b):
        with pytest.raises(WorkspaceError):
            os_workspace.resolve_readable_file(file)
    reader_a = _ws(tmp_path, (AllowedPath(first.session_directory, "read"),))
    assert reader_a.resolve_readable_file(file_a).read_text() == '{"rows":[1]}'
    with pytest.raises(WorkspaceError):
        reader_a.resolve_readable_file(file_b)
    with pytest.raises(WorkspaceError):
        reader_a.resolve_writable_file(file_a)
    writer_a = _ws(tmp_path, (AllowedPath(first.session_directory, "write"),))
    assert writer_a.resolve_writable_file(file_a).is_file()
    with pytest.raises(WorkspaceError):
        writer_a.resolve_readable_file(file_b)
    second.cleanup()
    first.cleanup()
    assert not first.session_directory.exists()
    assert not second.session_directory.exists()


def test_allowed_paths_do_not_escape_workspace(tmp_path):
    outside = tmp_path.parent / "elsewhere"
    outside.mkdir(exist_ok=True)
    with pytest.raises(WorkspaceError):
        _ws(tmp_path, (AllowedPath(outside, "write"),))
    with pytest.raises(WorkspaceError):
        _ws(tmp_path, (AllowedPath(Path("../escape"), "read"),))
    with pytest.raises(WorkspaceError):
        _ws(tmp_path, (AllowedPath(tmp_path, "write"),))


def test_allowed_paths_reject_symlink_and_sensitive_children(tmp_path):
    parent = tmp_path / ".cli-agent"
    own = parent / "mcp-data-test"
    own.mkdir(parents=True)
    (own / "plain.json").write_text("{}")
    (own / ".env").write_text("secret")
    (own / ".ssh").mkdir()
    (own / ".ssh" / "id.json").write_text("secret")
    (own / "sub").mkdir()
    (own / "sub" / "data.json").write_text("{}")
    (own / "link").symlink_to(own / "sub", target_is_directory=True)
    ws = _ws(tmp_path, (AllowedPath(own, "write"),))
    assert ws.resolve_readable_file(".cli-agent/mcp-data-test/plain.json").is_file()
    for name in (".env", ".ssh/id.json", "link/data.json"):
        with pytest.raises(WorkspaceError):
            ws.resolve_readable_file(f".cli-agent/mcp-data-test/{name}")
    with pytest.raises(WorkspaceError):
        _ws(tmp_path, (AllowedPath(own / "link", "read"),))


def test_nested_explicit_protection_not_overridden(tmp_path):
    own = tmp_path / ".cli-agent" / "mcp-data-test"
    own.mkdir(parents=True)
    locked = own / "locked.json"
    locked.write_text("{}")
    ws = _ws(tmp_path, (AllowedPath(own, "write"),), (locked,))
    with pytest.raises(WorkspaceError):
        ws.resolve_readable_file(".cli-agent/mcp-data-test/locked.json")


def test_session_artifact_parent_symlink_rejected(tmp_path):
    other = tmp_path.parent / "other-dumps"
    other.mkdir(exist_ok=True)
    (tmp_path / ".cli-agent").symlink_to(other, target_is_directory=True)
    with pytest.raises(WorkspaceError):
        McpResultArtifacts(tmp_path, ()).save("{}", structured=True)


def test_allowed_path_manifest_round_trip(tmp_path):
    policy = tmp_path / "policy.json"
    write_path_policy(policy, {
        "protected_paths": [str(tmp_path / ".cli-agent")],
        "mutation_protected_paths": [],
        "allowed_paths": [{"path": str(tmp_path / ".cli-agent" / "mcp-data-1"), "access": "read"}],
    })
    restored = read_path_policy(policy)
    assert restored["allowed_paths"] == ({
        "path": str(tmp_path / ".cli-agent" / "mcp-data-1"), "access": "read"
    },)
    with pytest.raises(ValueError):
        write_path_policy(tmp_path / "invalid.json", {
            "protected_paths": [], "mutation_protected_paths": [],
            "allowed_paths": [{"path": "/x", "access": "admin"}],
        })
