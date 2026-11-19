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


def test_flow_policy_rejects_symlinked_parent_even_within_workspace(tmp_path, monkeypatch):
    import asyncio

    target_dir = tmp_path / "other-concept"
    target_dir.mkdir()
    alias = tmp_path / "alias"
    try:
        alias.symlink_to(target_dir, target_is_directory=True)
    except (OSError, NotImplementedError):
        pytest.skip("Symlink creation is unsupported.")
    (tmp_path / "prompt.md").write_text("write", encoding="utf-8")
    (tmp_path / "flow.toml").write_text(
        'version = 1\n[[steps]]\nid = "build"\nprompt_file = "prompt.md"\n'
        'workspace_access = "write"\n[steps.tool_policy]\n'
        'allowed_tools = ["os__write_file"]\n'
        'write_paths = ["alias/other.md"]\n',
        encoding="utf-8",
    )
    flow = load_flow(tmp_path / "flow.toml", workspace=tmp_path)

    async def forbidden_model(*args, **kwargs):
        raise AssertionError("Model must not run with redirected write grant")

    monkeypatch.setattr(runner, "run_once", forbidden_model)
    with pytest.raises(ValueError, match="symlinks or junctions"):
        asyncio.run(run_flow(flow, workspace=tmp_path))
    assert not (target_dir / "other.md").exists()


@pytest.mark.parametrize("allowed_evidence", [False, True])
def test_delegated_tools_respect_flow_allowlist(tmp_path, monkeypatch, allowed_evidence):
    import json

    from cli_agent.state.evidence_state import EvidenceState
    from cli_agent.state.evidence_tools import ADD_EVIDENCE_TOOL_NAME
    from cli_agent.state.sub_runs import START_SUB_RUN, execute_sub_run
    import cli_agent.runtime.agent_loop as agent_loop

    permitted = {"os__read_file", START_SUB_RUN}
    if allowed_evidence:
        permitted.add(ADD_EVIDENCE_TOOL_NAME)
    tool = lambda name: {"type": "function", "function": {"name": name}}
    agent = SimpleNamespace(
        _sub_runs=[],
        _evidence_state=EvidenceState(),
        _flow_tool_allowlist=frozenset(permitted),
        _model_tools=lambda: [tool("os__read_file"), tool(START_SUB_RUN)],
        _server_instructions={},
        _active_servers=set(),
        _tool_routes={},
        _build_reference_context_message=lambda **kwargs: None,
        _main_tools_need_workspace_context=lambda names: False,
        _format_current_time=lambda now: "test time",
        model_client=SimpleNamespace(),
        _timezone=None,
        base_system_prompt="Test",
    )
    captured = []

    async def fake_run_loop(child, **kwargs):
        captured.append(kwargs["tools"])
        return json.dumps({
            "status": "partial", "summary": "No evidence required",
            "evidence_ids": [], "confidence": "low",
            "uncertainties": ["Delegated test"],
        })

    monkeypatch.setattr(agent_loop, "run_model_loop", fake_run_loop)
    result = asyncio.run(execute_sub_run(agent, {"task": "Inspect the selected artifact only"}))
    assert result["status"] == "partial"
    names = {entry["function"]["name"] for entry in captured[0]}
    assert names <= permitted
    assert (ADD_EVIDENCE_TOOL_NAME in names) is allowed_evidence
    assert START_SUB_RUN not in names



@pytest.mark.parametrize("allowed_tools", [
    (),
    ("os__read_file",),
    ("agent__start_sub_run",),
])
@pytest.mark.parametrize("source", ["okf", "chroma"])
def test_flow_policy_skips_forbidden_knowledge_preflight(allowed_tools, source):
    """A forbidden implicit retrieval must not run before the main model loop."""
    from cli_agent.runtime.agent_conversation import ConversationMixin

    class ForbiddenSession:
        async def search(self, *_args, **_kwargs):
            raise AssertionError("Knowledge search must not run")

        async def call_tool(self, *_args, **_kwargs):
            raise AssertionError("Knowledge MCP tool must not run")

    agent = SimpleNamespace(
        _flow_tool_allowlist=frozenset(allowed_tools),
        _okf_options=SimpleNamespace(required=True) if source == "okf" else None,
        _okf_enabled=True,
        _vector_config=SimpleNamespace(mode="chroma", required=True)
        if source == "chroma" else None,
        _vector_session=ForbiddenSession(),
        _knowledge_session=ForbiddenSession(),
    )
    assert asyncio.run(ConversationMixin._collect_knowledge(agent, "test")) is None



def test_internal_only_flow_policy_retains_enabled_internal_tools(tmp_path):
    from cli_agent.runtime.agent import CliAgent
    from cli_agent.state.evidence_state import EvidenceState
    from cli_agent.state.sub_runs import START_SUB_RUN
    from cli_agent.state.evidence_tools import PLAN_TOOL_NAME

    agent = CliAgent.__new__(CliAgent)
    agent._server_tools = {
        "os": [{"type": "function", "function": {"name": "os__read_file"}}],
    }
    agent._active_servers = {"os"}
    agent._disabled_tools = set()
    agent._sub_run_enabled = True
    agent._evidence_state = EvidenceState()
    agent._flow_tool_allowlist = frozenset({PLAN_TOOL_NAME, START_SUB_RUN})

    names = {tool["function"]["name"] for tool in agent._model_tools()}
    assert names == {PLAN_TOOL_NAME, START_SUB_RUN}

    agent._flow_tool_allowlist = frozenset()
    assert agent._model_tools() == []

    agent._flow_tool_allowlist = None
    names = {tool["function"]["name"] for tool in agent._model_tools()}
    assert "os__read_file" in names
    assert PLAN_TOOL_NAME in names


def test_policy_disabled_required_vector_does_not_block_okf(tmp_path, monkeypatch):
    import json
    import cli_agent.runtime.agent_conversation as conversation
    from cli_agent.runtime.agent_conversation import ConversationMixin

    class OkfSession:
        async def call_tool(self, name, arguments):
            assert name == "knowledge_index"
            return SimpleNamespace(isError=False, structuredContent={}, content=[])

    seen = []
    async def fake_loop(**kwargs):
        seen.append(kwargs)
        return json.dumps({
            "found_content": False, "selected_okf_tokens": [],
            "reason_code": "not_applicable",
        })

    async def fake_operation(awaitable, **kwargs):
        return await awaitable

    monkeypatch.setattr(conversation, "await_mcp_operation", fake_operation)
    monkeypatch.setattr(conversation, "tool_result_text", lambda result: "{}")
    monkeypatch.setattr(conversation, "_knowledge_allowed_calls", lambda result: {})

    session = OkfSession()
    agent = SimpleNamespace(
        _flow_tool_allowlist=frozenset({"okf__knowledge_index"}),
        _okf_options=SimpleNamespace(required=True, max_tool_calls=10, max_concept_reads=10),
        _okf_enabled=True,
        _vector_config=SimpleNamespace(mode="okf", required=True),
        _vector_session=SimpleNamespace(),
        _knowledge_session=session,
        _knowledge_routes={"okf__knowledge_index": (session, "knowledge_index", None)},
        _knowledge_tools=[{"function": {"name": "okf__knowledge_index"}}],
        history=[],
        _build_knowledge_system_prompt=lambda **kwargs: "Test",
        _dump_value=lambda *args: None,
        _run_model_loop=fake_loop,
    )
    assert asyncio.run(ConversationMixin._collect_knowledge(agent, "check")) is None
    assert len(seen) == 1
    assert [tool["function"]["name"] for tool in seen[0]["tools"]] == ["okf__knowledge_index"]
