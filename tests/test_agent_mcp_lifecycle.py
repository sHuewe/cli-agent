from __future__ import annotations

import asyncio
from contextlib import AsyncExitStack
from pathlib import Path
from types import SimpleNamespace

import pytest

from cli_agent.admin_config import McpPolicy
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
        server = McpServerConfig(name="broken", command="broken")
        agent = make_agent(
            tmp_path,
            (server,),
            mcp_policy=McpPolicy(allow_untrusted_stdio=True),
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


def test_start_server_rejects_duplicate_connection(tmp_path: Path) -> None:
    async def exercise() -> None:
        agent = make_agent(
            tmp_path,
            mcp_policy=McpPolicy(allow_untrusted_stdio=True),
        )
        agent._sessions["external"] = object()
        parent = AsyncExitStack()
        await parent.__aenter__()
        try:
            with pytest.raises(RuntimeError, match="bereits verbunden"):
                await agent._start_server(
                    McpServerConfig(name="external", command="unused"),
                    parent,
                )
        finally:
            await parent.aclose()
    asyncio.run(exercise())


def test_start_server_registers_tools_and_instructions(tmp_path: Path) -> None:
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
        agent = make_agent(
            tmp_path,
            mcp_policy=McpPolicy(allow_untrusted_stdio=True),
        )
        parent = AsyncExitStack()
        await parent.__aenter__()

        async def connect(_stack, _config):
            return Session(), "Use carefully"

        agent._connect_server = connect
        config = McpServerConfig(name="docs", command="unused")
        await agent._start_server(config, parent)
        assert "docs__search" in agent._tool_routes
        assert agent._server_tools["docs"][0]["function"]["name"] == "docs__search"
        assert agent._server_instructions["docs"] == "Use carefully"
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
        agent._knowledge_session = object()
        agent.history.append({"role": "user", "content": "x"})
        await agent.close()
        assert agent._exit_stack is None
        assert agent._sessions == {}
        assert agent._active_servers == set()
        assert agent._knowledge_session is None
        assert agent.history == []
        await agent.close()
    asyncio.run(exercise())
