from pathlib import Path

from cli_agent.admin_config import McpPolicy
from cli_agent.agent import CliAgent
from cli_agent.config import McpServerConfig


class DummyModel:
    model = "test"
    base_url = "test://model"


async def _session(_name, _arguments):
    return "session"


def _agent(tmp_path: Path) -> CliAgent:
    return CliAgent(
        tmp_path,
        DummyModel(),
        (),
        mcp_policy=McpPolicy(),
        approval_callback=_session,
    )


def test_session_approval_is_exact_tool_only(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    external = McpServerConfig(name="external", command="unused")

    assert agent._requires_approval(external, "search", "external__search") is True
    assert agent._requires_approval(external, "read", "external__read") is True

    assert __import__("asyncio").run(
        agent._approve_tool_call("external__search", {"query": "first"})
    ) is True

    assert agent._requires_approval(external, "search", "external__search") is False
    assert agent._requires_approval(external, "read", "external__read") is True


def test_session_approval_applies_to_built_in_write_tool(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    writable = McpServerConfig(
        name="os",
        built_in=True,
        config={"allow_write_files": True},
    )

    assert agent._requires_approval(writable, "write_file", "os__write_file") is True
    assert __import__("asyncio").run(
        agent._approve_tool_call("os__write_file", {"path": "a.txt"})
    ) is True
    assert agent._requires_approval(writable, "write_file", "os__write_file") is False
    assert agent._requires_approval(writable, "delete_file", "os__delete_file") is True


def test_admin_auto_approval_stays_external_only(tmp_path: Path) -> None:
    agent = CliAgent(
        tmp_path,
        DummyModel(),
        (),
        mcp_policy=McpPolicy(auto_approve_tools=("os__write_file", "external__search")),
    )
    writable = McpServerConfig(
        name="os",
        built_in=True,
        config={"allow_write_files": True},
    )
    external = McpServerConfig(name="external", command="unused")

    assert agent._requires_approval(writable, "write_file", "os__write_file") is True
    assert agent._requires_approval(external, "search", "external__search") is False
