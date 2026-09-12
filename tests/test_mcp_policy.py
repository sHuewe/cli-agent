from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from cli_agent.admin_config import McpPolicy
from cli_agent.agent import CliAgent
from cli_agent.config import McpServerConfig


class ToolModel:
    model = "test"
    base_url = "test://model"

    def __init__(self, tool_name: str) -> None:
        self.responses = [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "call-1",
                        "function": {"name": tool_name, "arguments": {"query": "x"}},
                    }
                ],
            },
            {"role": "assistant", "content": "done"},
        ]

    async def chat(self, _messages, _tools, *, think=None):
        return self.responses.pop(0)


class FakeSession:
    def __init__(self) -> None:
        self.calls = []

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return SimpleNamespace(content=[], isError=False)


def _connected_external_agent(
    tmp_path: Path,
    *,
    policy: McpPolicy,
    approval_callback,
) -> tuple[CliAgent, FakeSession]:
    server = McpServerConfig(name="continuous", command="unused")
    model = ToolModel("continuous__search")
    agent = CliAgent(
        tmp_path,
        model,
        (server,),
        mcp_policy=policy,
        approval_callback=approval_callback,
    )
    session = FakeSession()
    agent._exit_stack = SimpleNamespace()
    agent._active_servers = {"continuous"}
    agent._server_tools = {
        "continuous": [{"function": {"name": "continuous__search"}}]
    }
    agent._tool_routes = {
        "continuous__search": (session, "search", server),
    }
    return agent, session


def test_external_tool_without_auto_approval_uses_callback(tmp_path: Path) -> None:
    approvals = []

    async def approve(name, arguments):
        approvals.append((name, arguments))
        return True

    agent, session = _connected_external_agent(
        tmp_path,
        policy=McpPolicy(),
        approval_callback=approve,
    )

    assert asyncio.run(agent.ask("search")) == "done"
    assert approvals == [("continuous__search", {"query": "x"})]
    assert session.calls == [("search", {"query": "x"})]


def test_admin_auto_approved_external_tool_skips_callback(tmp_path: Path) -> None:
    async def must_not_be_called(_name, _arguments):
        raise AssertionError("approval callback must not run for auto-approved tool")

    agent, session = _connected_external_agent(
        tmp_path,
        policy=McpPolicy(auto_approve_tools=("continuous__search",)),
        approval_callback=must_not_be_called,
    )

    assert asyncio.run(agent.ask("search")) == "done"
    assert session.calls == [("search", {"query": "x"})]
