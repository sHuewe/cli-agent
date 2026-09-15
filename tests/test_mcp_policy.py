from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

from cli_agent.admin_config import McpPolicy, TrustedMcpServer, TrustedMcpToolApproval
from cli_agent.agent import CliAgent
from cli_agent.config import McpServerConfig
from cli_agent.mcp_contracts import tool_contract_fingerprint

SEARCH_SCHEMA = {
    "type": "object",
    "properties": {"query": {"type": "string"}},
    "required": ["query"],
    "additionalProperties": False,
}
SEARCH_DESCRIPTION = "Search"
SEARCH_CONTRACT = tool_contract_fingerprint("search", SEARCH_SCHEMA, SEARCH_DESCRIPTION)


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

    async def chat(self, messages, tools, *, think=None):
        return self.responses.pop(0)


class FakeSession:
    def __init__(self) -> None:
        self.calls = []

    async def call_tool(self, name, arguments):
        self.calls.append((name, arguments))
        return SimpleNamespace(content=[], isError=False)


def _approval(contract: str = SEARCH_CONTRACT) -> TrustedMcpToolApproval:
    return TrustedMcpToolApproval(name="search", contract_sha256=contract)


def _connected_external_agent(
    tmp_path: Path,
    *,
    policy: McpPolicy,
    approval_callback,
    server: McpServerConfig | None = None,
    schema=SEARCH_SCHEMA,
    description: str = SEARCH_DESCRIPTION,
) -> tuple[CliAgent, FakeSession]:
    server = server or McpServerConfig(name="continuous", command="unused")
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
        "continuous": [
            {
                "function": {
                    "name": "continuous__search",
                    "description": description,
                    "parameters": schema,
                }
            }
        ]
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


def test_matching_server_tool_and_contract_skip_callback(tmp_path: Path) -> None:
    async def must_not_be_called(_name, _arguments):
        raise AssertionError("approval callback must not run for auto-approved tool")

    policy = McpPolicy(
        trusted_servers=(
            TrustedMcpServer(
                name="continuous",
                transport="stdio",
                command="unused",
                auto_approve_tools=(_approval(),),
            ),
        )
    )
    agent, session = _connected_external_agent(
        tmp_path,
        policy=policy,
        approval_callback=must_not_be_called,
    )

    assert asyncio.run(agent.ask("search")) == "done"
    assert session.calls == [("search", {"query": "x"})]


def test_changed_tool_schema_falls_back_to_interactive_approval(tmp_path: Path) -> None:
    approvals = []

    async def approve(name, arguments):
        approvals.append((name, arguments))
        return True

    changed_schema = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "workspace_data": {"type": "string"},
        },
        "required": ["query"],
        "additionalProperties": False,
    }
    policy = McpPolicy(
        trusted_servers=(
            TrustedMcpServer(
                name="continuous",
                transport="stdio",
                command="unused",
                auto_approve_tools=(_approval(),),
            ),
        )
    )
    agent, session = _connected_external_agent(
        tmp_path,
        policy=policy,
        approval_callback=approve,
        schema=changed_schema,
    )

    assert asyncio.run(agent.ask("search")) == "done"
    assert approvals == [("continuous__search", {"query": "x"})]
    assert session.calls == [("search", {"query": "x"})]


def test_changed_tool_description_falls_back_to_interactive_approval(tmp_path: Path) -> None:
    approvals = []

    async def approve(name, arguments):
        approvals.append((name, arguments))
        return True

    policy = McpPolicy(
        trusted_servers=(
            TrustedMcpServer(
                name="continuous",
                transport="stdio",
                command="unused",
                auto_approve_tools=(_approval(),),
            ),
        )
    )
    agent, session = _connected_external_agent(
        tmp_path,
        policy=policy,
        approval_callback=approve,
        description="Search and include local project data",
    )

    assert asyncio.run(agent.ask("search")) == "done"
    assert approvals == [("continuous__search", {"query": "x"})]
    assert session.calls == [("search", {"query": "x"})]


def test_same_server_and_tool_name_with_different_stdio_command_is_not_auto_approved(tmp_path: Path) -> None:
    approvals = []

    async def approve(name, arguments):
        approvals.append((name, arguments))
        return True

    policy = McpPolicy(
        trusted_servers=(
            TrustedMcpServer(
                name="continuous",
                transport="stdio",
                command="trusted-server",
                auto_approve_tools=(_approval(),),
            ),
        )
    )
    server = McpServerConfig(name="continuous", command="malicious-server")
    agent, session = _connected_external_agent(
        tmp_path,
        policy=policy,
        approval_callback=approve,
        server=server,
    )

    assert asyncio.run(agent.ask("search")) == "done"
    assert approvals == [("continuous__search", {"query": "x"})]
    assert session.calls == [("search", {"query": "x"})]


def test_same_server_and_tool_name_with_different_http_url_is_not_auto_approved(tmp_path: Path) -> None:
    approvals = []

    async def approve(name, arguments):
        approvals.append((name, arguments))
        return True

    policy = McpPolicy(
        trusted_servers=(
            TrustedMcpServer(
                name="continuous",
                transport="streamable_http",
                url="https://trusted.example/mcp",
                auto_approve_tools=(_approval(),),
            ),
        )
    )
    server = McpServerConfig(
        name="continuous",
        transport="streamable_http",
        url="http://localhost:9999/mcp",
    )
    agent, session = _connected_external_agent(
        tmp_path,
        policy=policy,
        approval_callback=approve,
        server=server,
    )

    assert asyncio.run(agent.ask("search")) == "done"
    assert approvals == [("continuous__search", {"query": "x"})]
    assert session.calls == [("search", {"query": "x"})]
