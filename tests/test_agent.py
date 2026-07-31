import asyncio
import json
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
    assert json.loads((dump_directory / "history.json").read_text()) == (
        agent.history
    )
    assert json.loads(
        (dump_directory / "working_messages.json").read_text()
    ) == model.calls[-1][0] + [{"role": "assistant", "content": "ok"}]
    assert json.loads((dump_directory / "system_prompt.json").read_text()) == (
        model.calls[-1][0][0]["content"]
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

    agent._dump_context(working_messages)
    agent._dump_context(working_messages)

    assert written_files.count("history.json") == 1
    assert written_files.count("working_messages.json") == 2
    assert written_files.count("system_prompt.json") == 2


def test_llm_context_is_not_dumped_by_default(tmp_path: Path) -> None:
    agent = make_agent(tmp_path)

    assert not (tmp_path / ".cli-agent").exists()


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
    compose = McpServerConfig(name="compose", command="unused")
    documents = McpServerConfig(name="documents", command="unused")
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
    assert asyncio.run(agent.ask("enable compose")) == (
        "MCP-Server compose aktiviert."
    )
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
    agent.disable_server("compose")

    answer = asyncio.run(agent.ask("Use the tool mentioned earlier"))

    assert answer == "Tool is unavailable."
    assert compose_session.tool_calls == []
    assert "compose__ps" not in str(model.calls[0][1])
    assert model.calls[1][0][-1]["role"] == "tool"
    assert "deaktiviert" in model.calls[1][0][-1]["content"]
    assert all(message.get("role") != "tool" for message in agent.messages)
    assert "old-call" not in str(agent.messages)

    agent.enable_server("compose")
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
