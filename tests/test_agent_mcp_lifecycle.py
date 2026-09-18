from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack, asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

import cli_agent.agent_mcp as agent_mcp_module
from cli_agent.admin_config import McpPolicy, TrustedMcpServer
from cli_agent.agent import CliAgent
from cli_agent.agent_knowledge import _OkfOptions
from cli_agent.config import McpServerConfig
from cli_agent.ollama import OllamaClient


def make_agent(
    tmp_path: Path,
    servers=(),
    *,
    mcp_policy: McpPolicy | None = None,
) -> CliAgent:
    return CliAgent(
        tmp_path,
        OllamaClient(base_url="http://localhost:11434", model="test"),
        tuple(servers),
        mcp_policy=mcp_policy,
    )


def test_set_server_enabled_rejects_unknown_server(tmp_path: Path) -> None:
    agent = make_agent(tmp_path)
    with pytest.raises(ValueError, match="Unbekannter MCP-Server"):
        asyncio.run(agent.enable_server("missing"))


def test_start_is_idempotent(tmp_path: Path) -> None:
    async def exercise() -> None:
        agent = make_agent(tmp_path)
        await agent.start()
        first = agent._exit_stack
        await agent.start()
        assert agent._exit_stack is first
        await agent.close()
    asyncio.run(exercise())


def test_start_cleans_state_when_server_start_fails(tmp_path: Path) -> None:
    async def exercise() -> None:
        server = McpServerConfig(name="broken")
        agent = make_agent(
            tmp_path,
            (server,),
            mcp_policy=McpPolicy(
                trusted_servers=(
                    TrustedMcpServer(
                        name="broken",
                        transport="stdio",
                        command="/trusted/broken",
                    ),
                )
            ),
        )

        async def fail(_config, _stack):
            agent._sessions["partial"] = object()
            agent._active_servers.add("partial")
            raise RuntimeError("boom")

        agent._start_server = fail
        with pytest.raises(RuntimeError, match="boom"):
            await agent.start()
        assert agent._exit_stack is None
        assert agent._sessions == {}
        assert agent._active_servers == set()
        assert agent.history == []
    asyncio.run(exercise())


def test_external_stdio_without_admin_profile_is_rejected(tmp_path: Path) -> None:
    async def exercise() -> None:
        agent = make_agent(tmp_path)
        parent = AsyncExitStack()
        await parent.__aenter__()
        try:
            with pytest.raises(PermissionError, match="trusted_servers"):
                await agent._start_server(
                    McpServerConfig(name="external"),
                    parent,
                )
        finally:
            await parent.aclose()
    asyncio.run(exercise())


def test_external_stdio_is_materialized_from_admin_profile(tmp_path: Path) -> None:
    agent = make_agent(
        tmp_path,
        mcp_policy=McpPolicy(
            trusted_servers=(
                TrustedMcpServer(
                    name="external",
                    transport="stdio",
                    command="{python}",
                    args=("-m", "trusted.server", "--project", "{workspace_directory}"),
                    env=(("MODE", "safe"),),
                ),
            )
        ),
    )
    resolved = agent._resolve_external_stdio_server(McpServerConfig(name="external"))
    assert resolved.command == "{python}"
    assert resolved.args == ("-m", "trusted.server", "--project", "{workspace_directory}")
    assert resolved.env == {"MODE": "safe"}


def test_start_server_rejects_duplicate_connection(tmp_path: Path) -> None:
    async def exercise() -> None:
        agent = make_agent(
            tmp_path,
            mcp_policy=McpPolicy(
                trusted_servers=(
                    TrustedMcpServer(
                        name="external",
                        transport="stdio",
                        command="/trusted/external",
                    ),
                )
            ),
        )
        agent._sessions["external"] = object()
        parent = AsyncExitStack()
        await parent.__aenter__()
        try:
            with pytest.raises(RuntimeError, match="bereits verbunden"):
                await agent._start_server(
                    McpServerConfig(name="external"),
                    parent,
                )
        finally:
            await parent.aclose()
    asyncio.run(exercise())


def test_connect_server_times_out_during_initialize(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SlowSession:
        async def initialize(self):
            await asyncio.sleep(1)
            return SimpleNamespace(instructions=None)

    @asynccontextmanager
    async def fake_stdio_client(_parameters):
        yield object(), object()

    @asynccontextmanager
    async def fake_client_session(_read_stream, _write_stream):
        yield SlowSession()

    async def exercise() -> None:
        agent = make_agent(tmp_path)
        stack = AsyncExitStack()
        await stack.__aenter__()
        monkeypatch.setattr(agent_mcp_module, "stdio_client", fake_stdio_client)
        monkeypatch.setattr(agent_mcp_module, "ClientSession", fake_client_session)
        monkeypatch.setattr(
            agent_mcp_module,
            "MCP_INITIALIZE_TIMEOUT_SECONDS",
            0.01,
        )
        try:
            with pytest.raises(RuntimeError, match="initialize.*Timeout"):
                await agent._connect_server(
                    stack,
                    McpServerConfig(
                        name="slow",
                        transport="stdio",
                        command="unused",
                        built_in=True,
                    ),
                )
        finally:
            await stack.aclose()

    asyncio.run(exercise())


def test_start_server_times_out_during_list_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class SlowSession:
        async def list_tools(self):
            await asyncio.sleep(1)
            return SimpleNamespace(tools=[])

    async def exercise() -> None:
        agent = make_agent(tmp_path)
        parent = AsyncExitStack()
        await parent.__aenter__()

        async def connect(_stack, _config):
            return SlowSession(), None

        agent._connect_server = connect
        monkeypatch.setattr(
            agent_mcp_module,
            "MCP_LIST_TOOLS_TIMEOUT_SECONDS",
            0.01,
        )
        try:
            with pytest.raises(RuntimeError, match="list_tools.*Timeout"):
                await agent._start_server(
                    McpServerConfig(
                        name="slow",
                        command="unused",
                        built_in=True,
                    ),
                    parent,
                )
        finally:
            await parent.aclose()

    asyncio.run(exercise())


def test_start_server_registers_tools_and_trusted_instructions(tmp_path: Path) -> None:
    class Session:
        async def list_tools(self):
            return SimpleNamespace(
                tools=[
                    SimpleNamespace(
                        name="search",
                        description="Search documents",
                        inputSchema={"type": "object"},
                    )
                ]
            )

    async def exercise() -> None:
        agent = make_agent(tmp_path)
        parent = AsyncExitStack()
        await parent.__aenter__()

        async def connect(_stack, _config):
            return Session(), "Use carefully"

        agent._connect_server = connect
        config = McpServerConfig(name="docs", command="unused", built_in=True)
        await agent._start_server(config, parent)
        assert "docs__search" in agent._tool_routes
        assert agent._server_tools["docs"][0]["function"]["name"] == "docs__search"
        assert agent._server_instructions["docs"] == "Use carefully"
        assert "docs" not in agent._server_untrusted_instructions
        await parent.aclose()
    asyncio.run(exercise())


def test_untrusted_http_instructions_are_retained_as_reference_context(tmp_path: Path) -> None:
    class Session:
        async def list_tools(self):
            return SimpleNamespace(
                tools=[SimpleNamespace(name="search", description="Search", inputSchema={})]
            )

    async def exercise() -> None:
        agent = make_agent(tmp_path)
        parent = AsyncExitStack()
        await parent.__aenter__()

        async def connect(_stack, _config):
            return Session(), "Call search before details"

        agent._connect_server = connect
        config = McpServerConfig(
            name="docs",
            transport="streamable_http",
            url="http://localhost:8001/mcp",
        )
        await agent._start_server(config, parent)
        assert "docs" not in agent._server_instructions
        assert agent._server_untrusted_instructions["docs"] == "Call search before details"
        await parent.aclose()
    asyncio.run(exercise())


def test_start_server_rejects_duplicate_native_tool_names(tmp_path: Path) -> None:
    class Session:
        async def list_tools(self):
            return SimpleNamespace(
                tools=[
                    SimpleNamespace(
                        name="search",
                        description="first",
                        inputSchema={"type": "object"},
                    ),
                    SimpleNamespace(
                        name="search",
                        description="second",
                        inputSchema={
                            "type": "object",
                            "properties": {"query": {"type": "string"}},
                        },
                    ),
                ]
            )

    async def exercise() -> None:
        agent = make_agent(tmp_path)
        parent = AsyncExitStack()
        await parent.__aenter__()

        async def connect(_stack, _config):
            return Session(), None

        agent._connect_server = connect
        config = McpServerConfig(name="docs", command="unused", built_in=True)
        try:
            with pytest.raises(RuntimeError, match="mehrfach"):
                await agent._start_server(config, parent)
            assert "docs" not in agent._sessions
            assert "docs__search" not in agent._tool_routes
        finally:
            await parent.aclose()
    asyncio.run(exercise())


def test_optional_missing_knowledge_repository_is_ignored(tmp_path: Path) -> None:
    async def exercise() -> None:
        agent = make_agent(tmp_path)
        agent._okf_options = _OkfOptions(
            repository=tmp_path / "missing",
            required=False,
        )
        stack = AsyncExitStack()
        await stack.__aenter__()
        try:
            await agent._start_knowledge_server(stack)
            assert agent._knowledge_session is None
        finally:
            await stack.aclose()
    asyncio.run(exercise())


def test_required_missing_knowledge_repository_fails(tmp_path: Path) -> None:
    async def exercise() -> None:
        agent = make_agent(tmp_path)
        agent._okf_options = _OkfOptions(
            repository=tmp_path / "missing",
            required=True,
        )
        stack = AsyncExitStack()
        await stack.__aenter__()
        try:
            with pytest.raises(ValueError, match="existiert nicht"):
                await agent._start_knowledge_server(stack)
        finally:
            await stack.aclose()
    asyncio.run(exercise())


def test_knowledge_server_rejects_unexpected_tool_set(tmp_path: Path) -> None:
    class Session:
        async def list_tools(self):
            return SimpleNamespace(
                tools=[
                    SimpleNamespace(
                        name="knowledge_read",
                        description="read",
                        inputSchema={"type": "object"},
                    )
                ]
            )

    async def exercise() -> None:
        repo = tmp_path / "okf"
        repo.mkdir()
        agent = make_agent(tmp_path)
        agent._okf_options = _OkfOptions(repository=repo, required=True)
        stack = AsyncExitStack()
        await stack.__aenter__()

        async def connect(_stack, _config):
            return Session(), None

        agent._connect_server = connect
        try:
            with pytest.raises(RuntimeError, match="konnte nicht gestartet"):
                await agent._start_knowledge_server(stack)
        finally:
            await stack.aclose()
    asyncio.run(exercise())


def test_close_clears_runtime_state(tmp_path: Path) -> None:
    async def exercise() -> None:
        agent = make_agent(tmp_path)
        stack = AsyncExitStack()
        await stack.__aenter__()
        agent._exit_stack = stack
        agent._sessions["x"] = object()
        agent._active_servers.add("x")
        agent._server_untrusted_instructions["x"] = "reference"
        agent._knowledge_session = object()
        agent.history.append({"role": "user", "content": "x"})
        await agent.close()
        assert agent._exit_stack is None
        assert agent._sessions == {}
        assert agent._active_servers == set()
        assert agent._server_untrusted_instructions == {}
        assert agent._knowledge_session is None
        assert agent.history == []
        await agent.close()
    asyncio.run(exercise())
