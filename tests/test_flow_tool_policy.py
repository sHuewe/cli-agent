"""Per-step LLM tool allowlist and exact OS write-target regressions."""
import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest

from cli_agent.platform.os_operations import Workspace, WorkspaceError
from cli_agent.settings.config import McpServerConfig
import cli_agent.workflows.flow as runner
from cli_agent.workflows.flow import load_flow, run_flow


def test_scoped_workspace_write_and_mkdir_do_not_touch_other_concepts(tmp_path):
    target = tmp_path / "generated/doctrines/a/target.md"
    alien = tmp_path / "generated/doctrines/a/other.md"
    directory = target.parent
    workspace = Workspace.from_directory(
        tmp_path, McpServerConfig(name="os", config={"allow_write_files": True}),
        scoped_write_files=(target,),
        scoped_mkdir_dirs=(directory,),
    )
    workspace.make_directory("generated/doctrines/a")
    assert directory.is_dir()
    workspace.write_file("generated/doctrines/a/target.md", "# target")
    assert target.read_text() == "# target"
    with pytest.raises(WorkspaceError, match="scoped OS mutation"):
        workspace.write_file("generated/doctrines/a/other.md", "# alien")
    with pytest.raises(WorkspaceError, match="scoped OS mutation"):
        workspace.make_directory("generated/doctrines/b")
    assert not alien.exists()


def test_scoped_workspace_blocks_symlinked_target(tmp_path):
    actual = tmp_path / "real"
    actual.mkdir()
    aliased = tmp_path / "alias"
    try:
        aliased.symlink_to(actual, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("symlink unavailable on host")
    with pytest.raises(WorkspaceError, match="symlinks"):
        Workspace.from_directory(
            tmp_path, McpServerConfig(name="os"),
            scoped_write_files=(aliased / "target.md",),
        )


def test_flow_tool_policy_options_are_resolved_and_without_extra_tools(tmp_path, monkeypatch):
    (tmp_path / "prompt.md").write_text("Create one concept", encoding="utf-8")
    (tmp_path / "flow.toml").write_text("""
version = 1

[[steps]]
id = "build"
prompt_file = "prompt.md"
response_format = "json"
workspace_access = "write"
output = "result.json"
[steps.tool_policy]
allowed_tools = ["os__read_file", "os__write_file", "os__make_directory"]
write_paths = ["output/concept.md"]
""", encoding="utf-8")
    seen = []

    async def fake_run_once(options, *, dependencies=None):
        seen.append(options)
        return SimpleNamespace(answer='{"status":"ok"}', web_context_statuses=())

    monkeypatch.setattr(runner, "run_once", fake_run_once)
    asyncio.run(run_flow(load_flow(tmp_path / "flow.toml", workspace=tmp_path),
                         workspace=tmp_path))
    assert len(seen) == 1
    options = seen[0]
    assert options.tool_allowlist == (
        "os__read_file", "os__write_file", "os__make_directory"
    )
    assert options.scoped_write_files == (tmp_path / "output/concept.md",)
    assert options.scoped_mkdir_dirs == (tmp_path / "output",)


@pytest.mark.parametrize("policy", [
    'allowed_tools = ["os__write_file"]',
    'allowed_tools = ["os__write_file"]\nwrite_paths = ["" ]',
    'allowed_tools = ["os__read_file"]\nwrite_paths = ["output/file.md"]',
])
def test_invalid_or_unauthorized_policy_is_not_executed(tmp_path, policy):
    (tmp_path / "prompt.md").write_text("test", encoding="utf-8")
    (tmp_path / "flow.toml").write_text(
        'version = 1\n[[steps]]\nid = "x"\nprompt_file = "prompt.md"\n'
        'workspace_access = "write"\n[steps.tool_policy]\n' + policy,
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="tool_policy"):
        load_flow(tmp_path / "flow.toml", workspace=tmp_path)


def test_tool_policy_rejects_parent_traversal_at_runtime(tmp_path):
    (tmp_path / "prompt.md").write_text("test", encoding="utf-8")
    (tmp_path / "flow.toml").write_text(
        'version = 1\n[[steps]]\nid = "x"\nprompt_file = "prompt.md"\n'
        'workspace_access = "write"\n[steps.tool_policy]\n'
        'allowed_tools = ["os__write_file"]\nwrite_paths = ["../outside.md"]',
        encoding="utf-8",
    )
    flow = load_flow(tmp_path / "flow.toml", workspace=tmp_path)
    with pytest.raises(ValueError, match="tool_policy"):
        asyncio.run(run_flow(flow, workspace=tmp_path))
