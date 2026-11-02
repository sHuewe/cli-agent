"""Python tool bridge contract and SDK isolation."""
import asyncio
from types import SimpleNamespace

import pytest
from cli_agent.sdk import AgentBuilder
from cli_agent.sdk.python_tools import make_python_tool, PythonToolSession, register_python_tools


def average(values: list[float]) -> float:
    """Average the values."""
    return sum(values) / len(values)


async def echo(value: str) -> dict[str, str]:
    return {"echo": value}


def test_python_tool_registration_and_schema():
    builder = AgentBuilder().register_tool(average).register_tool(echo)
    agent = builder.build()
    assert len(agent._python_tools) == 2
    assert agent._python_tools[0].name == "average"
    assert agent._python_tools[0].schema["required"] == ["values"]
    with pytest.raises(ValueError):
        builder.register_tool(average)
    assert not AgentBuilder().build()._python_tools


def test_python_tool_invalid_definitions():
    with pytest.raises(TypeError):
        AgentBuilder().register_tool(lambda value: value)
    with pytest.raises(ValueError):
        AgentBuilder().register_tool(average, name="agent__bad-tool")
    def unsupported(*args: int):
        return args
    with pytest.raises(TypeError):
        AgentBuilder().register_tool(unsupported)


def test_python_tool_session_sync_async_and_error():
    async def scenario():
        session = PythonToolSession((make_python_tool(average), make_python_tool(echo)))
        first = await session.call_tool("average", {"values": [2.0, 4.0]})
        assert not first.isError
        assert first.content[0].text == "3.0"
        second = await session.call_tool("echo", {"value": "hello"})
        assert not second.isError
        assert '"echo": "hello"' in second.content[0].text
        bad = await session.call_tool("average", {"values": "incorrect"})
        assert bad.isError
    asyncio.run(scenario())


def test_python_tools_route_through_regular_approval_and_listing():
    def fake_runtime():
        from cli_agent.runtime.agent import CliAgent
        runtime = CliAgent.__new__(CliAgent)
        runtime.mcp_servers = ()
        runtime._server_configs = {}
        runtime._active_servers = set()
        runtime._server_tools = {}
        runtime._tool_routes = {}
        runtime._tool_identities = {}
        runtime._session_approved_tools = set()
        runtime._disabled_tools = set()
        runtime.mcp_policy = SimpleNamespace(trusted_servers=())
        runtime._sub_run_enabled = False
        runtime._agui_frontend_tools = {}
        runtime._evidence_state = SimpleNamespace(tool_definitions=lambda: [])
        return runtime

    runtime = fake_runtime()
    register_python_tools(runtime, (make_python_tool(average),))
    assert "python__average" in runtime._tool_routes
    session, name, config = runtime._tool_routes["python__average"]
    assert name == "average"
    assert runtime._requires_approval(config, name, exposed_name="python__average")
    from cli_agent.sdk.tools import available_tools
    assert any(x.name == "python__average" and x.approval_required for x in available_tools(runtime))
    with pytest.raises(ValueError):
        register_python_tools(runtime, (make_python_tool(echo),))
