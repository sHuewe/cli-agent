"""Direct MCP invocation must not enter the agent's conversation loop."""
import asyncio
from types import SimpleNamespace

import pytest
from mcp.types import CallToolResult, TextContent

from cli_agent.sdk import AgentBuilder
from cli_agent.sdk.mcp import execute_direct_tool


class Session:
    def __init__(self):
        self.calls = []

    async def call_tool(self, name, args):
        self.calls.append((name, args))
        return CallToolResult(
            content=[TextContent(type="text", text="file content")],
            structuredContent={"path": "Datei.txt"},
        )


class Runtime:
    def __init__(self, *, approval=False):
        self.history = [{"role": "user", "content": "original"}]
        self._session_approved_tools = set()
        self._evidence_state = SimpleNamespace(evidences={"e1": object()})
        self._disabled_tools = set()
        self._active_servers = {"os"}
        self.session = Session()
        self._tool_routes = {
            "os__read_file": (
                self.session, "read_file",
                SimpleNamespace(name="os", built_in=True),
            )
        }
        self.approval = approval
        self.approval_calls = []

    def _tool_is_enabled(self, name):
        return name not in self._disabled_tools

    def _tool_input_schema(self, name, *, phase):
        return {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
            "additionalProperties": False,
        }

    def _requires_approval(self, server, native_name, exposed_name=None):
        return self.approval

    async def _approve_tool_call(self, name, args):
        self.approval_calls.append((name, args))
        return not self.approval


def test_direct_mcp_call_isolated_from_history_and_evidence():
    async def scenario():
        runtime = Runtime()
        before = list(runtime.history)
        evidence = dict(runtime._evidence_state.evidences)
        result = await execute_direct_tool(runtime, "os__read_file", "Datei.txt")
        assert result.text == "file content"
        assert result.structured_content == {"path": "Datei.txt"}
        assert result.is_error is False
        assert runtime.session.calls == [("read_file", {"path": "Datei.txt"})]
        assert runtime.history == before
        assert runtime._evidence_state.evidences == evidence
    asyncio.run(scenario())


def test_direct_mcp_call_validates_before_approval_and_execution():
    async def scenario():
        runtime = Runtime(approval=True)
        with pytest.raises(ValueError, match="Invalid MCP tool arguments"):
            await execute_direct_tool(runtime, "os__read_file", {"path": 1})
        assert runtime.session.calls == []
        assert runtime.approval_calls == []
        with pytest.raises(PermissionError):
            await execute_direct_tool(runtime, "os__read_file", {"path": "Datei.txt"})
        assert runtime.approval_calls == [
            ("os__read_file", {"path": "Datei.txt"})
        ]
        assert runtime.session.calls == []
        runtime._disabled_tools.add("os__read_file")
        with pytest.raises(ValueError, match="disabled"):
            await execute_direct_tool(runtime, "os__read_file", "Datei.txt")
        runtime._disabled_tools.clear()
        runtime._active_servers.clear()
        with pytest.raises(ValueError, match="disabled"):
            await execute_direct_tool(runtime, "os__read_file", "Datei.txt")
    asyncio.run(scenario())


def test_sdk_mcp_facade_dispatches_on_worker(monkeypatch, tmp_path):
    class FakeRuntime(Runtime):
        def __init__(self):
            super().__init__()
            self._active_servers = {"os"}
        async def __aenter__(self):
            return self
        async def __aexit__(self, *_):
            return None

    runtime = FakeRuntime()
    monkeypatch.setattr(
        "cli_agent.sdk.agent.prepare_agent",
        lambda *_a, **_k: SimpleNamespace(agent=runtime),
    )

    async def scenario():
        async with AgentBuilder().with_project_config(workspace=tmp_path).build() as agent:
            result = await agent.mcp.call_tool("os__read_file", {"path": "Datei.txt"})
            assert result.text == "file content"
            assert runtime.history == [{"role": "user", "content": "original"}]
    asyncio.run(scenario())


def test_direct_mcp_rejects_internal_knowledge_reentry_without_call():
    async def scenario():
        runtime = Runtime()
        internal_session = Session()
        runtime._tool_routes["knowledge_reentry__request_additional_knowledge"] = (
            internal_session, "request_additional_knowledge",
            SimpleNamespace(name="knowledge_reentry", built_in=True),
        )
        runtime._active_servers.add("knowledge_reentry")
        with pytest.raises(ValueError, match="Internal knowledge reentry"):
            await execute_direct_tool(
                runtime, "knowledge_reentry__request_additional_knowledge",
                {"question": "Tell me more"},
            )
        assert internal_session.calls == []
    asyncio.run(scenario())


def test_direct_mcp_checks_structured_payload_size_independently(monkeypatch):
    from cli_agent.security.mcp_limits import MAX_MCP_TOOL_RESULT_CHARS

    class HugeSession:
        async def call_tool(self, name, args):
            return CallToolResult(
                content=[TextContent(type="text", text="short")],
                structuredContent={"data": "a" * (MAX_MCP_TOOL_RESULT_CHARS + 1)},
            )

    async def scenario():
        runtime = Runtime()
        runtime._tool_routes["os__read_file"] = (
            HugeSession(), "read_file",
            SimpleNamespace(name="os", built_in=True),
        )
        with pytest.raises(RuntimeError, match="Sicherheitslimit"):
            await execute_direct_tool(runtime, "os__read_file", "Datei.txt")
    asyncio.run(scenario())
