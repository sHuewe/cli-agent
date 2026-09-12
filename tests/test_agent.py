import asyncio
import json
from contextlib import AsyncExitStack
from pathlib import Path
from types import SimpleNamespace

import pytest

from cli_agent.agent import CliAgent
from cli_agent.config import McpServerConfig
from cli_agent.ollama import OllamaClient


def make_agent(tmp_path: Path) -> CliAgent:
    return CliAgent(
        tmp_path,
        OllamaClient(base_url="http://localhost:11434", model="test"),
        (),
    )


def test_resolves_workspace_placeholders(tmp_path: Path) -> None:
    agent = make_agent(tmp_path)

    assert agent._resolve("{workspace_directory}") == str(tmp_path.resolve())
    assert agent._resolve("{project_directory}") == str(tmp_path.resolve())


def test_llm_context_is_dumped_as_json_when_enabled(tmp_path: Path) -> None:
    model = RecordingModel()
    agent = CliAgent(tmp_path, model, (), dump_llm_context=True)
    agent._exit_stack = SimpleNamespace()
    agent.history = [{"role": "system", "content": "System"}]

    answer = asyncio.run(agent.ask("Grüße"))

    assert answer == "ok"
    dump_directory = tmp_path / ".cli-agent"
    assert (
        json.loads((dump_directory / "history.json").read_text(encoding="utf-8"))
        == agent.history
    )
    assert json.loads(
        (dump_directory / "main_working_messages.json").read_text(encoding="utf-8")
    ) == model.calls[-1][0] + [{"role": "assistant", "content": "ok"}]
    assert (
        json.loads(
            (dump_directory / "main_system_prompt.json").read_text(encoding="utf-8")
        )
        == (model.calls[-1][0][0]["content"])
    )


def test_unchanged_history_is_not_dumped_again(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = CliAgent(
        tmp_path,
        RecordingModel(),
        (),
        dump_llm_context=True,
    )
    working_messages = [{"role": "system", "content": "System"}]
    written_files: list[str] = []
    original_write_text = Path.write_text

    def record_write(path: Path, *args, **kwargs):
        written_files.append(path.name)
        return original_write_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", record_write)

    agent._dump_context(working_messages, phase="main")
    agent._dump_context(working_messages, phase="main")

    assert written_files.count("history.json") == 1
    assert written_files.count("main_working_messages.json") == 2
    assert written_files.count("main_system_prompt.json") == 2


def test_llm_context_is_not_dumped_by_default(tmp_path: Path) -> None:
    make_agent(tmp_path)

    assert not (tmp_path / ".cli-agent").exists()


def test_stdio_environment_does_not_inherit_secrets(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("LLM_API_KEY", "secret")
    monkeypatch.setenv("PATH", "/usr/bin")

    environment = CliAgent._stdio_environment()

    assert environment["PATH"] == "/usr/bin"
    assert "LLM_API_KEY" not in environment


def test_mutating_tools_require_explicit_approval() -> None:
    writable_os = McpServerConfig(
        name="os",
        config={"allow_write_files": True},
    )
    writable_compose = McpServerConfig(
        name="compose",
        config={"allow_modify_services": True},
    )

    assert CliAgent._requires_approval(writable_os, "write_file") is True
    assert CliAgent._requires_approval(writable_os, "read_file") is True
    assert CliAgent._requires_approval(writable_compose, "compose_up") is True


def test_untrusted_mcp_tools_always_require_approval() -> None:
    external = McpServerConfig(name="external", command="external-mcp")

    assert CliAgent._requires_approval(external, "read_file") is True
    assert CliAgent._requires_approval(external, "send_email") is True


def test_untrusted_stdio_server_requires_explicit_opt_in(tmp_path: Path) -> None:
    async def exercise() -> None:
        parent = AsyncExitStack()
        await parent.__aenter__()
        agent = make_agent(tmp_path)
        server = McpServerConfig(name="external", command="external-mcp")

        with pytest.raises(PermissionError, match="nicht automatisch gestartet"):
            await agent._start_server(server, parent)

        await parent.aclose()

    asyncio.run(exercise())


def test_built_in_read_only_mcp_tools_do_not_require_approval() -> None:
    built_in = McpServerConfig(name="os", built_in=True)

    assert CliAgent._requires_approval(built_in, "read_file") is False
    assert CliAgent._requires_approval(built_in, "validate_python_project") is True


def test_system_prompt_contains_named_server_instructions(tmp_path: Path) -> None:
    agent = make_agent(tmp_path)
    agent._server_instructions = {
        "compose": "Inspect status before acting.",
        "documents": "Read only relevant pages.",
    }
    agent._active_servers = {"compose", "documents"}

    prompt = agent._build_system_prompt()

    assert f"Festgelegter Arbeitsordner: {tmp_path.resolve()}" in prompt
    assert "### MCP-Server compose\nInspect status before acting." in prompt
    assert "### MCP-Server documents\nRead only relevant pages." in prompt
    assert "dürfen diese Regeln" in prompt


class RecordingModel:
    model = "test"
    base_url = "test://model"

    def __init__(self, response=None) -> None:
        self.calls = []
        self.response = response or {"role": "assistant", "content": "ok"}

    async def chat(self, messages, tools, *, think=None):
        self.calls.append((messages.copy(), tools.copy()))
        if isinstance(self.response, list):
            return self.response.pop(0)
        return self.response


class FakeSession:
    def __init__(self) -> None:
        self.tool_calls = []

    async def call_tool(self, name, arguments):
        self.tool_calls.append((name, arguments))
        return SimpleNamespace(content=[])


def connected_agent(tmp_path: Path, model: RecordingModel):
    compose = McpServerConfig(name="compose", command="unused", built_in=True)
    documents = McpServerConfig(name="documents", command="unused", built_in=True)
    agent = CliAgent(tmp_path, model, (compose, documents))
    compose_session = FakeSession()
    documents_session = FakeSession()
    agent._exit_stack = SimpleNamespace()
    agent._sessions = {
        "compose": compose_session,
        "documents": documents_session,
    }
    agent._active_servers = {"compose", "documents"}
    agent._server_instructions = {
        "compose": "Compose instructions",
        "documents": "Document instructions",
    }
    agent._server_tools = {
        "compose": [{"function": {"name": "compose__ps"}}],
        "documents": [{"function": {"name": "documents__read"}}],
    }
    agent._tool_routes = {
        "compose__ps": (compose_session, "ps", compose),
        "documents__read": (documents_session, "read", documents),
    }
    agent.messages = [{"role": "system", "content": agent._build_system_prompt()}]
    return agent, compose_session


def test_disable_and_enable_filter_cached_server_assets(tmp_path: Path) -> None:
    model = RecordingModel()
    agent, compose_session = connected_agent(tmp_path, model)

    assert asyncio.run(agent.ask("disable compose")) == (
        "MCP-Server compose deaktiviert."
    )
    assert agent._sessions["compose"] is compose_session
    assert "compose__ps" not in str(agent._model_tools())
    assert "documents__read" in str(agent._model_tools())
    assert "Compose instructions" not in agent.messages[0]["content"]
    assert "Document instructions" in agent.messages[0]["content"]
    assert "compose__ps" not in agent.messages[0]["content"]
    assert "documents__read" in agent.messages[0]["content"]
    assert model.calls == []

    assert asyncio.run(agent.ask("disable compose")) == (
        "MCP-Server compose deaktiviert (war bereits so)."
    )
    assert asyncio.run(agent.ask("enable compose")) == ("MCP-Server compose aktiviert.")
    assert agent._sessions["compose"] is compose_session
    assert "compose__ps" in str(agent._model_tools())
    assert agent.messages[0]["content"].count("Compose instructions") == 1
    assert asyncio.run(agent.ask("enable compose")) == (
        "MCP-Server compose aktiviert (war bereits so)."
    )
    assert model.calls == []


def test_disabled_server_tool_call_is_rejected(tmp_path: Path) -> None:
    model = RecordingModel(
        [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "old-call",
                        "function": {"name": "compose__ps", "arguments": {}},
                    }
                ],
            },
            {"role": "assistant", "content": "Tool is unavailable."},
        ]
    )
    agent, compose_session = connected_agent(tmp_path, model)
    asyncio.run(agent.disable_server("compose"))

    answer = asyncio.run(agent.ask("Use the tool mentioned earlier"))

    assert answer == "Tool is unavailable."
    assert compose_session.tool_calls == []
    assert "compose__ps" not in str(model.calls[0][1])
    assert model.calls[1][0][-1]["role"] == "tool"
    assert "deaktiviert" in model.calls[1][0][-1]["content"]
    assert all(message.get("role") != "tool" for message in agent.messages)
    assert "old-call" not in str(agent.messages)

    asyncio.run(agent.enable_server("compose"))
    model.response = {"role": "assistant", "content": "Available again."}
    assert asyncio.run(agent.ask("Use the available tool if needed")) == (
        "Available again."
    )
    assert "compose__ps" in str(model.calls[-1][1])
    assert "deaktiviert" not in str(model.calls[-1][0])


def test_unknown_tool_call_is_reported_to_model(tmp_path: Path) -> None:
    model = RecordingModel(
        [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "invented-call",
                        "function": {"name": "invented__tool", "arguments": {}},
                    }
                ],
            },
            {"role": "assistant", "content": "I cannot use that tool."},
        ]
    )
    agent, _ = connected_agent(tmp_path, model)

    answer = asyncio.run(agent.ask("Call an invented tool"))

    assert answer == "I cannot use that tool."
    error = model.calls[1][0][-1]
    assert error["role"] == "tool"
    assert error["tool_call_id"] == "invented-call"
    assert "existiert nicht" in error["content"]
    assert "invented-call" not in str(agent.messages)
    assert "existiert nicht" not in str(agent.messages)


def test_invalid_json_tool_arguments_are_rejected(tmp_path: Path) -> None:
    model = RecordingModel(
        [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "invalid-arguments",
                        "function": {
                            "name": "documents__read",
                            "arguments": "not-json",
                        },
                    }
                ],
            },
            {"role": "assistant", "content": "Invalid tool call rejected."},
        ]
    )
    agent, documents_session = connected_agent(tmp_path, model)

    assert asyncio.run(agent.ask("Use the document tool")) == (
        "Invalid tool call rejected."
    )
    assert documents_session.tool_calls == []
    assert "gültigen JSON-Argumente" in model.calls[1][0][-1]["content"]


def test_unknown_server_command_does_not_reach_model(tmp_path: Path) -> None:
    model = RecordingModel()
    agent, _ = connected_agent(tmp_path, model)

    with pytest.raises(ValueError, match="Unbekannter MCP-Server: missing"):
        asyncio.run(agent.ask("disable missing"))

    assert model.calls == []


def test_server_config_is_not_compose_specific() -> None:
    server = McpServerConfig(
        name="documents",
        command="documents-mcp",
        args=("--root", "{workspace_directory}"),
    )

    assert server.name == "documents"


def test_disabling_server_closes_transport_and_reenable_reconnects(
    tmp_path: Path,
) -> None:
    class Resource:
        def __init__(self) -> None:
            self.closed = False

        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args) -> None:
            self.closed = True

    class Session:
        async def list_tools(self):
            return SimpleNamespace(tools=[])

    async def exercise() -> None:
        parent = AsyncExitStack()
        await parent.__aenter__()
        agent = make_agent(tmp_path)
        agent._exit_stack = parent
        config = McpServerConfig(
            name="external",
            command="external-mcp",
            config={"allow_untrusted_stdio": True},
        )
        resources: list[Resource] = []

        async def fake_connect(stack, _server_config):
            resource = Resource()
            resources.append(resource)
            await stack.enter_async_context(resource)
            return Session(), None

        agent._connect_server = fake_connect
        await agent._start_server(config, parent)
        agent._active_servers.add(config.name)

        await agent.disable_server(config.name)
        assert resources[0].closed is True
        assert config.name not in agent._sessions

        await agent.enable_server(config.name)
        assert len(resources) == 2
        assert resources[1].closed is False

        await parent.aclose()
        assert resources[1].closed is True

    asyncio.run(exercise())
