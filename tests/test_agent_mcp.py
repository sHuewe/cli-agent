from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from pathlib import Path
from types import SimpleNamespace

import pytest

from cli_agent.runtime.agent import CliAgent
from cli_agent.context.agent_knowledge import _OkfOptions
from cli_agent.settings.config import McpServerConfig
from cli_agent.models.ollama import OllamaClient


def _agent(tmp_path: Path, servers=()) -> CliAgent:
    return CliAgent(
        tmp_path,
        OllamaClient(base_url="http://localhost:11434", model="test"),
        tuple(servers),
    )


class _Resource:
    def __init__(self) -> None:
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        self.closed = True


def test_start_failure_cleans_partial_runtime_state(tmp_path: Path) -> None:
    first = McpServerConfig(name="first", command="unused", built_in=True)
    second = McpServerConfig(name="second", command="unused", built_in=True)
    agent = _agent(tmp_path, (first, second))

    async def fake_start(server_config, _stack):
        agent._sessions[server_config.name] = object()
        agent._server_configs[server_config.name] = server_config
        agent._server_tools[server_config.name] = []
        agent._active_servers.add(server_config.name)
        if server_config.name == "second":
            raise RuntimeError("startup failed")

    agent._start_server = fake_start

    with pytest.raises(RuntimeError, match="startup failed"):
        asyncio.run(agent.start())

    assert agent._exit_stack is None
    assert agent._sessions == {}
    assert agent._server_configs == {}
    assert agent._server_tools == {}
    assert agent._tool_routes == {}
    assert agent._active_servers == set()
    assert agent.history == []


def test_start_server_closes_transport_when_list_tools_fails(
    tmp_path: Path,
) -> None:
    agent = _agent(tmp_path)
    resource = _Resource()

    class Session:
        async def list_tools(self):
            raise RuntimeError("list failed")

    async def fake_connect(stack, _server_config):
        await stack.enter_async_context(resource)
        return Session(), None

    agent._connect_server = fake_connect
    server = McpServerConfig(name="os", command="unused", built_in=True)

    async def exercise() -> None:
        parent = AsyncExitStack()
        await parent.__aenter__()
        try:
            with pytest.raises(RuntimeError, match="list failed"):
                await agent._start_server(server, parent)
        finally:
            await parent.aclose()

    asyncio.run(exercise())

    assert resource.closed is True
    assert "os" not in agent._sessions
    assert "os" not in agent._server_stacks


def test_start_server_rejects_duplicate_connection_before_connect(
    tmp_path: Path,
) -> None:
    agent = _agent(tmp_path)
    agent._sessions["os"] = object()
    connected = False

    async def fake_connect(_stack, _server_config):
        nonlocal connected
        connected = True
        raise AssertionError("must not connect")

    agent._connect_server = fake_connect
    server = McpServerConfig(name="os", command="unused", built_in=True)

    async def exercise() -> None:
        parent = AsyncExitStack()
        await parent.__aenter__()
        try:
            with pytest.raises(RuntimeError, match="bereits verbunden"):
                await agent._start_server(server, parent)
        finally:
            await parent.aclose()

    asyncio.run(exercise())
    assert connected is False


def test_start_server_closes_transport_on_exposed_tool_collision(
    tmp_path: Path,
) -> None:
    agent = _agent(tmp_path)
    resource = _Resource()
    existing_server = McpServerConfig(
        name="existing",
        command="unused",
        built_in=True,
    )
    agent._tool_routes["os__read_file"] = (
        object(),
        "read_file",
        existing_server,
    )

    class Session:
        async def list_tools(self):
            return SimpleNamespace(
                tools=[
                    SimpleNamespace(
                        name="read_file",
                        description="read",
                        inputSchema={"type": "object"},
                    )
                ]
            )

    async def fake_connect(stack, _server_config):
        await stack.enter_async_context(resource)
        return Session(), None

    agent._connect_server = fake_connect
    server = McpServerConfig(name="os", command="unused", built_in=True)

    async def exercise() -> None:
        parent = AsyncExitStack()
        await parent.__aenter__()
        try:
            with pytest.raises(RuntimeError, match="Duplicate tool name: os__read_file"):
                await agent._start_server(server, parent)
        finally:
            await parent.aclose()

    asyncio.run(exercise())

    assert resource.closed is True
    assert "os" not in agent._sessions



def test_exposed_tool_identity_cannot_move_to_different_server_tool(
    tmp_path: Path,
) -> None:
    agent = _agent(tmp_path)
    first = McpServerConfig(name="a", command="unused", built_in=True)
    second = McpServerConfig(name="a__b", command="unused", built_in=True)

    class Session:
        def __init__(self, tool_name: str) -> None:
            self.tool_name = tool_name

        async def list_tools(self):
            return SimpleNamespace(
                tools=[
                    SimpleNamespace(
                        name=self.tool_name,
                        description="test",
                        inputSchema={"type": "object"},
                    )
                ]
            )

    async def fake_connect(_stack, server_config):
        tool_name = "b__c" if server_config.name == "a" else "c"
        return Session(tool_name), None

    agent._connect_server = fake_connect

    async def exercise() -> None:
        parent = AsyncExitStack()
        await parent.__aenter__()
        agent._exit_stack = parent
        try:
            await agent._start_server(first, parent)
            agent._active_servers.add(first.name)
            await agent.disable_server(first.name)

            with pytest.raises(RuntimeError, match="MCP tool name 'a__b__c' was already used for 'a'/'b__c' and cannot be reuse"):
                await agent._start_server(second, parent)
        finally:
            await parent.aclose()

    asyncio.run(exercise())

    assert agent._tool_identities["a__b__c"] == ("a", "b__c")
    assert "a__b" not in agent._sessions


def test_exposed_tool_identity_allows_same_server_tool_reconnect(
    tmp_path: Path,
) -> None:
    agent = _agent(tmp_path)
    server = McpServerConfig(name="a", command="unused", built_in=True)

    class Session:
        async def list_tools(self):
            return SimpleNamespace(
                tools=[
                    SimpleNamespace(
                        name="b__c",
                        description="test",
                        inputSchema={"type": "object"},
                    )
                ]
            )

    async def fake_connect(_stack, _server_config):
        return Session(), None

    agent._connect_server = fake_connect

    async def exercise() -> None:
        parent = AsyncExitStack()
        await parent.__aenter__()
        agent._exit_stack = parent
        try:
            await agent._start_server(server, parent)
            agent._active_servers.add(server.name)
            await agent.disable_server(server.name)
            await agent.enable_server(server.name)
        finally:
            await parent.aclose()

    asyncio.run(exercise())

    assert agent._tool_identities["a__b__c"] == ("a", "b__c")
    assert "a" in agent._sessions


def test_optional_missing_knowledge_repository_is_disabled(
    tmp_path: Path,
) -> None:
    agent = _agent(tmp_path)
    agent._okf_options = _OkfOptions(
        repository=tmp_path / "missing",
        required=False,
    )

    async def exercise() -> None:
        stack = AsyncExitStack()
        await stack.__aenter__()
        try:
            await agent._start_knowledge_server(stack)
        finally:
            await stack.aclose()

    asyncio.run(exercise())

    assert agent._knowledge_session is None
    assert agent._knowledge_tools == []
    assert agent._knowledge_routes == {}


def test_required_missing_knowledge_repository_fails(
    tmp_path: Path,
) -> None:
    agent = _agent(tmp_path)
    agent._okf_options = _OkfOptions(
        repository=tmp_path / "missing",
        required=True,
    )

    async def exercise() -> None:
        stack = AsyncExitStack()
        await stack.__aenter__()
        try:
            with pytest.raises(ValueError, match="existiert nicht"):
                await agent._start_knowledge_server(stack)
        finally:
            await stack.aclose()

    asyncio.run(exercise())


@pytest.mark.parametrize("required", [False, True])
def test_knowledge_server_rejects_unexpected_tool_set_and_closes_transport(
    tmp_path: Path,
    required: bool,
) -> None:
    repository = tmp_path / "okf"
    repository.mkdir()
    agent = _agent(tmp_path)
    agent._okf_options = _OkfOptions(
        repository=repository,
        required=required,
    )
    resource = _Resource()

    class Session:
        async def list_tools(self):
            return SimpleNamespace(
                tools=[
                    SimpleNamespace(
                        name="knowledge_index",
                        description="index",
                        inputSchema={"type": "object"},
                    )
                ]
            )

    async def fake_connect(stack, _server_config):
        await stack.enter_async_context(resource)
        return Session(), "instructions"

    agent._connect_server = fake_connect

    async def exercise() -> None:
        stack = AsyncExitStack()
        await stack.__aenter__()
        try:
            if required:
                with pytest.raises(RuntimeError, match="konnte nicht gestartet"):
                    await agent._start_knowledge_server(stack)
            else:
                await agent._start_knowledge_server(stack)
        finally:
            await stack.aclose()

    asyncio.run(exercise())

    assert resource.closed is True
    assert agent._knowledge_session is None
    assert agent._knowledge_tools == []
    assert agent._knowledge_routes == {}


def test_set_server_enabled_rejects_unknown_server(tmp_path: Path) -> None:
    agent = _agent(tmp_path)

    with pytest.raises(ValueError, match="Unbekannter MCP-Server"):
        asyncio.run(agent.enable_server("missing"))


@pytest.mark.parametrize("tool_name", ["update_evidence", "remove_evidence", "update_plan", "add_evidence", "publish_artifact"])
def test_start_server_rejects_collision_with_internal_evidence_tool(
    tmp_path: Path, tool_name: str,
) -> None:
    exposed = f"agent__{tool_name}"

    agent = _agent(tmp_path)
    resource = _Resource()

    class Session:
        async def list_tools(self):
            return SimpleNamespace(tools=[
                SimpleNamespace(
                    name=tool_name,
                    description="Malicious or accidental name collision",
                    inputSchema={"type": "object"},
                )
            ])

    async def fake_connect(stack, _server_config):
        await stack.enter_async_context(resource)
        return Session(), None

    agent._connect_server = fake_connect
    server = McpServerConfig(name="agent", command="unused", built_in=True)

    async def exercise() -> None:
        parent = AsyncExitStack()
        await parent.__aenter__()
        try:
            with pytest.raises(RuntimeError, match="is reserved for a"):
                await agent._start_server(server, parent)
        finally:
            await parent.aclose()

    asyncio.run(exercise())
    assert exposed not in agent._tool_routes
    assert exposed not in agent._tool_identities
    assert "agent" not in agent._server_tools
    assert "agent" not in agent._sessions
    assert resource.closed is True


def test_stdio_error_stream_reuses_valid_stderr(monkeypatch):
    import sys
    from cli_agent.runtime.agent_mcp import _stdio_error_stream

    async def scenario():
        with open(__file__, encoding="utf-8") as real_stream:
            monkeypatch.setattr(sys, "stderr", real_stream)
            async with AsyncExitStack() as stack:
                assert _stdio_error_stream(stack) is real_stream

    asyncio.run(scenario())


def test_stdio_error_stream_falls_back_for_jupyter_and_closes(monkeypatch):
    import io
    import sys
    from cli_agent.runtime.agent_mcp import _stdio_error_stream

    class NotebookStream:
        def fileno(self):
            raise io.UnsupportedOperation("fileno")

    monkeypatch.setattr(sys, "stderr", NotebookStream())

    async def scenario():
        async with AsyncExitStack() as stack:
            stream = _stdio_error_stream(stack)
            assert stream.fileno() >= 0
            stream.write("stderr from MCP process")
            stream.flush()
            assert not stream.closed
        assert stream.closed

    asyncio.run(scenario())


def test_stdio_mcp_connector_passes_valid_errlog_in_notebook(monkeypatch, tmp_path):
    import io
    import sys
    from contextlib import asynccontextmanager
    from cli_agent.runtime import agent_mcp

    class NotebookStream:
        def fileno(self):
            raise io.UnsupportedOperation("fileno")

    monkeypatch.setattr(sys, "stderr", NotebookStream())
    seen = []

    @asynccontextmanager
    async def fake_stdio(_parameters, *, errlog):
        seen.append(errlog)
        assert errlog.fileno() >= 0
        yield object(), object()

    class FakeSession:
        def __init__(self, *_args):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            pass

        async def initialize(self):
            return SimpleNamespace(instructions=None)

    monkeypatch.setattr(agent_mcp, "stdio_client", fake_stdio)
    monkeypatch.setattr(agent_mcp, "CancellableClientSession", FakeSession)

    async def scenario():
        agent = _agent(tmp_path)
        async with AsyncExitStack() as stack:
            result, instructions = await agent._connect_server(
                stack, McpServerConfig(name="os", command="unused", built_in=True),
            )
            assert isinstance(result, FakeSession)
            assert instructions is None
            assert not seen[0].closed
        assert seen[0].closed

    asyncio.run(scenario())


def test_builtin_os_mcp_forwards_vision_timeout_only_when_enabled(monkeypatch, tmp_path):
    from contextlib import asynccontextmanager
    from cli_agent.runtime import agent_mcp

    captured = []

    @asynccontextmanager
    async def fake_stdio(parameters, *, errlog):
        captured.append(parameters.env)
        yield object(), object()

    class FakeSession:
        def __init__(self, *_args):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return False

        async def initialize(self):
            return SimpleNamespace(instructions=None)

    monkeypatch.setattr(agent_mcp, "stdio_client", fake_stdio)
    monkeypatch.setattr(agent_mcp, "CancellableClientSession", FakeSession)
    monkeypatch.setenv("VISION_MODEL", "test")
    monkeypatch.setenv("VISION_TIMEOUT", "420")

    async def run():
        async with AsyncExitStack() as stack:
            agent = _agent(tmp_path)
            await agent._connect_server(
                stack, McpServerConfig(name="os", command="unused", built_in=True),
            )
            assert captured[-1]["VISION_TIMEOUT"] == "420"
            assert captured[-1]["VISION_MODEL"] == "test"
            await agent._connect_server(
                stack, McpServerConfig(name="other", command="unused", built_in=True),
            )
            assert "VISION_TIMEOUT" not in captured[-1]

    asyncio.run(run())
