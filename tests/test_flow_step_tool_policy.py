"""Step-level OS tool and exact-path write policy security tests."""
from pathlib import Path

import pytest

from cli_agent.workflows.flow import load_flow
from cli_agent.settings.config import McpServerConfig
from cli_agent.platform.os_operations import Workspace, WorkspaceError
from cli_agent.platform.execution import os_mcp_server_config


def _flow(tmp_path, policy, *, access="write"):
    (tmp_path / "prompt.md").write_text("demo", encoding="utf-8")
    (tmp_path / "flow.toml").write_text(
        "version = 1\n[[steps]]\nid = \"concept\"\n"
        'prompt_file = "prompt.md"\n'
        f'workspace_access = "{access}"\n'
        + policy, encoding="utf-8"
    )
    return load_flow(tmp_path / "flow.toml", workspace=tmp_path)


def test_allowed_tools_and_write_targets_are_parsed(tmp_path):
    flow = _flow(tmp_path, '''
[steps.tool_policy]
allowed_tools = ["os__read_file", "os__write_file", "os__make_directory"]
write_paths = ["out/concept.md"]
mkdir_paths = ["out"]
''')
    assert flow.steps[0].tool_policy == {
        "allowed_tools": ["os__read_file", "os__write_file", "os__make_directory"],
        "write_paths": ["out/concept.md"], "mkdir_paths": ["out"],
    }


@pytest.mark.parametrize("policy", [
    '[steps.tool_policy]\nallowed_tools = ["os__write_file"]\n',
    '[steps.tool_policy]\nallowed_tools = ["os__write_file"]\nwrite_paths = [123]\n',
    '[steps.tool_policy]\nallowed_tools = ["os__write_file", "os__write_file"]\nwrite_paths = ["out/concept.md"]\n',
    '[steps.tool_policy]\nallowed_tools = ["os__write_file"]\nwrite_paths = ["out/concept.md"]\nunknown = true\n',
])
def test_rejects_invalid_policy(tmp_path, policy):
    with pytest.raises(ValueError):
        _flow(tmp_path, policy)


def test_rejects_write_path_when_step_is_read_only(tmp_path):
    with pytest.raises(ValueError, match="workspace_access=write"):
        _flow(tmp_path, '''
[steps.tool_policy]
allowed_tools = ["os__write_file"]
write_paths = ["out.md"]
''', access="read")


def test_workspace_scope_blocks_every_mutation_except_declared_targets(tmp_path):
    output = tmp_path / "out" / "concept.md"
    tmp_path.joinpath("other.md").write_text("original", encoding="utf-8")
    workspace = Workspace.from_directory(
        tmp_path,
        McpServerConfig(name="os", config={"allow_write_files": True}),
        scoped_write_files=(output,),
        scoped_mkdir_dirs=(tmp_path / "out",),
    )
    workspace.make_directory("out")
    workspace.write_file("out/concept.md", "good")
    assert output.read_text() == "good"
    for operation, args in (
        ("write_file", ("other.md", "corrupt")),
        ("delete_file", ("other.md",)),
        ("copy_file", ("other.md", "copy.md")),
        ("move_file", ("other.md", "moved.md")),
        ("make_directory", ("unrelated",)),
    ):
        with pytest.raises(WorkspaceError, match="scope"):
            getattr(workspace, operation)(*args)
    assert (tmp_path / "other.md").read_text() == "original"


def test_scope_forbids_symlinked_parent_and_external_targets(tmp_path):
    nested = tmp_path / "nested"
    nested.mkdir()
    outside = tmp_path.parent / "outside-flow"
    outside.mkdir(exist_ok=True)
    (nested / "alias").symlink_to(outside, target_is_directory=True)
    with pytest.raises(WorkspaceError, match="symlink"):
        Workspace.from_directory(
            tmp_path, McpServerConfig(name="os"),
            scoped_write_files=(nested / "alias" / "escape.md",),
        )


def test_scope_is_exported_in_server_configuration(tmp_path):
    server = os_mcp_server_config(
        "write", scoped_write_files=(tmp_path / "concept.md",),
        scoped_mkdir_dirs=(tmp_path / "out",),
    )
    assert "--scoped-writes" in server.literal_args
    assert str(tmp_path / "concept.md") in server.literal_args
    assert str(tmp_path / "out") in server.literal_args


def test_empty_scope_blocks_all_writes(tmp_path):
    workspace = Workspace.from_directory(
        tmp_path, McpServerConfig(name="os", config={"allow_write_files": True}),
        scoped_write_files=(), scoped_mkdir_dirs=(),
    )
    with pytest.raises(WorkspaceError, match="scope"):
        workspace.write_file("new.md", "no")
    with pytest.raises(WorkspaceError, match="scope"):
        workspace.make_directory("out")


def test_assert_file_operation_rechecks_existing_output_and_rejects_missing(tmp_path):
    import asyncio
    import json
    from cli_agent.workflows.flow import run_flow

    target = tmp_path / "output" / "concept.md"
    target.parent.mkdir()
    (tmp_path / "flow.toml").write_text('''
version = 1
[[steps]]
id = "assert"
output = "state/file-check.json"
overwrite_output = true
[steps.filesystem_operation]
kind = "assert_file"
path = "output/concept.md"
''', encoding="utf-8")
    flow = load_flow(tmp_path / "flow.toml", workspace=tmp_path)

    with pytest.raises((ValueError, FileNotFoundError)):
        asyncio.run(run_flow(flow, workspace=tmp_path))

    target.write_text("# Concept", encoding="utf-8")
    asyncio.run(run_flow(flow, workspace=tmp_path))
    report = json.loads((tmp_path / "state/file-check.json").read_text())
    assert report["status"] == "ok"
    assert report["path"] == "output/concept.md"

    target.unlink()
    with pytest.raises((ValueError, FileNotFoundError)):
        asyncio.run(run_flow(flow, workspace=tmp_path))
