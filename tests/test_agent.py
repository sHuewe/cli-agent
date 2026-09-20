import asyncio
import json
from contextlib import AsyncExitStack
from pathlib import Path
from types import SimpleNamespace

import pytest

from cli_agent.admin_config import McpPolicy, TrustedMcpServer, TrustedMcpToolApproval
from cli_agent.agent import CliAgent
from cli_agent.config import McpServerConfig
from cli_agent.mcp_contracts import tool_contract_fingerprint
from cli_agent.ollama import OllamaClient


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


def make_agent(tmp_path: Path, *, mcp_policy: McpPolicy | None = None) -> CliAgent:
    return CliAgent(tmp_path, OllamaClient(base_url="http://localhost:11434", model="test"), (), mcp_policy=mcp_policy)


def test_stdio_resolves_all_runtime_placeholders(tmp_path: Path) -> None:
    config_file = tmp_path / "config.toml"
    agent = CliAgent(
        tmp_path,
        OllamaClient(base_url="http://localhost:11434", model="test"),
        (),
        config_file=config_file,
    )

    assert agent._resolve_stdio_value("{workspace_directory}") == str(tmp_path.resolve())
    assert agent._resolve_stdio_value("{project_directory}") == str(tmp_path.resolve())
    assert agent._resolve_stdio_value("{config_file}") == str(config_file)
    assert agent._resolve_stdio_value("{python}")


def test_http_resolves_only_workspace_placeholders(tmp_path: Path) -> None:
    agent = make_agent(tmp_path)

    assert agent._resolve_http_value("{workspace_directory}") == str(tmp_path.resolve())
    assert agent._resolve_http_value("{project_directory}") == str(tmp_path.resolve())


@pytest.mark.parametrize("placeholder", ["{python}", "{config_file}"])
def test_http_rejects_local_runtime_placeholders(
    tmp_path: Path,
    placeholder: str,
) -> None:
    agent = make_agent(tmp_path)

    with pytest.raises(ValueError, match="HTTP-MCP-Werte.*nicht zulässig"):
        agent._resolve_http_value(placeholder)


def test_llm_context_is_dumped_as_json_when_enabled(tmp_path: Path) -> None:
    model = RecordingModel()
    agent = CliAgent(tmp_path, model, (), dump_llm_context=True)
    agent._exit_stack = SimpleNamespace()
    agent.history = [{"role": "system", "content": "System"}]
    assert asyncio.run(agent.ask("Grüße")) == "ok"
    dump_directory = tmp_path / ".cli-agent"
    assert json.loads((dump_directory / "history.json").read_text(encoding="utf-8")) == agent.history
    assert json.loads((dump_directory / "main_working_messages.json").read_text(encoding="utf-8")) == model.calls[-1][0] + [{"role": "assistant", "content": "ok"}]


def test_unchanged_history_is_not_dumped_again(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    agent = CliAgent(tmp_path, RecordingModel(), (), dump_llm_context=True)
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


def test_llm_context_is_not_dumped_by_default(tmp_path: Path) -> None:
    make_agent(tmp_path)
    assert not (tmp_path / ".cli-agent").exists()


def test_stdio_environment_does_not_inherit_secrets(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LLM_API_KEY", "secret")
    monkeypatch.setenv("PATH", "/usr/bin")
    environment = CliAgent._stdio_environment()
    assert environment["PATH"] == "/usr/bin"
    assert "LLM_API_KEY" not in environment



def test_http_mcp_static_authorization_is_rejected_at_runtime(tmp_path: Path) -> None:
    server = McpServerConfig(
        name="docs",
        transport="streamable_http",
        url="https://mcp.internal/mcp",
        headers={"Authorization": "Bearer static-secret"},
    )
    agent = make_agent(tmp_path)

    with pytest.raises(ValueError, match="Authorization"):
        agent._http_headers(server)


def test_http_mcp_bearer_auth_is_optional(tmp_path: Path) -> None:
    server = McpServerConfig(
        name="docs",
        transport="streamable_http",
        url="https://mcp.internal/mcp",
        headers={"X-Client": "cli-agent"},
    )
    agent = make_agent(tmp_path)

    assert agent._http_bearer_token_env(server) is None
    assert agent._http_headers(server) == {"X-Client": "cli-agent"}


def test_http_mcp_bearer_token_is_injected_from_admin_bound_env(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = McpServerConfig(
        name="docs",
        transport="streamable_http",
        url="https://mcp.internal/mcp",
        headers={"X-Workspace": "{workspace_directory}"},
    )
    policy = McpPolicy(
        trusted_servers=(
            TrustedMcpServer(
                name="docs",
                transport="streamable_http",
                url="https://mcp.internal/mcp",
                headers=(("X-Workspace", "{workspace_directory}"),),
                bearer_token_env="CLI_AGENT_DOCS_TOKEN",
            ),
        )
    )
    agent = make_agent(tmp_path, mcp_policy=policy)
    monkeypatch.setenv("CLI_AGENT_DOCS_TOKEN", "secret-token")

    headers = agent._http_headers(server)

    assert headers["Authorization"] == "Bearer secret-token"
    assert headers["X-Workspace"] == str(tmp_path.resolve())


def test_http_mcp_header_identity_is_case_insensitive(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = McpServerConfig(
        name="docs",
        transport="streamable_http",
        url="https://mcp.internal/mcp",
        headers={"X-Client-Id": "cli-agent"},
    )
    policy = McpPolicy(
        trusted_servers=(
            TrustedMcpServer(
                name="docs",
                transport="streamable_http",
                url="https://mcp.internal/mcp",
                headers=(("x-client-id", "cli-agent"),),
                bearer_token_env="CLI_AGENT_DOCS_TOKEN",
            ),
        )
    )
    agent = make_agent(tmp_path, mcp_policy=policy)
    monkeypatch.setenv("CLI_AGENT_DOCS_TOKEN", "secret-token")

    headers = agent._http_headers(server)

    assert headers["Authorization"] == "Bearer secret-token"
    assert headers["X-Client-Id"] == "cli-agent"


def test_http_mcp_bearer_token_missing_fails_closed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = McpServerConfig(
        name="docs",
        transport="streamable_http",
        url="https://mcp.internal/mcp",
    )
    policy = McpPolicy(
        trusted_servers=(
            TrustedMcpServer(
                name="docs",
                transport="streamable_http",
                url="https://mcp.internal/mcp",
                bearer_token_env="CLI_AGENT_DOCS_TOKEN",
            ),
        )
    )
    agent = make_agent(tmp_path, mcp_policy=policy)
    monkeypatch.delenv("CLI_AGENT_DOCS_TOKEN", raising=False)

    with pytest.raises(ValueError, match="CLI_AGENT_DOCS_TOKEN"):
        agent._http_headers(server)


def test_http_mcp_bearer_source_only_applies_to_matching_server_identity(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    server = McpServerConfig(
        name="docs",
        transport="streamable_http",
        url="https://other.internal/mcp",
    )
    policy = McpPolicy(
        trusted_servers=(
            TrustedMcpServer(
                name="docs",
                transport="streamable_http",
                url="https://mcp.internal/mcp",
                bearer_token_env="CLI_AGENT_DOCS_TOKEN",
            ),
        )
    )
    agent = make_agent(tmp_path, mcp_policy=policy)
    monkeypatch.setenv("CLI_AGENT_DOCS_TOKEN", "secret-token")

    assert agent._http_bearer_token_env(server) is None
    assert "Authorization" not in agent._http_headers(server)

def test_external_mcp_tools_require_approval_by_default(tmp_path: Path) -> None:
    agent = make_agent(tmp_path)
    external = McpServerConfig(name="external", command="external-mcp")
    assert agent._requires_approval(external, "read", "external__read") is True
    assert agent._requires_approval(external, "delete", "external__delete") is True


def test_auto_approval_requires_matching_server_identity_tool_and_contract(tmp_path: Path) -> None:
    schema = {"type": "object", "properties": {"query": {"type": "string"}}}
    contract = tool_contract_fingerprint("search", schema)
    policy = McpPolicy(
        trusted_servers=(
            TrustedMcpServer(
                name="continuous",
                transport="stdio",
                command="external-mcp",
                auto_approve_tools=(TrustedMcpToolApproval("search", contract),),
            ),
        )
    )
    agent = make_agent(tmp_path, mcp_policy=policy)
    agent._server_tools = {
        "continuous": [
            {
                "function": {
                    "name": "continuous__search",
                    "parameters": schema,
                }
            }
        ]
    }
    continuous = McpServerConfig(name="continuous", command="external-mcp")
    same_name_wrong_command = McpServerConfig(name="continuous", command="other-mcp")
    other = McpServerConfig(name="other", command="external-mcp")

    assert agent._requires_approval(continuous, "search", "continuous__search") is False
    assert agent._requires_approval(continuous, "read", "continuous__read") is True
    assert agent._requires_approval(same_name_wrong_command, "search", "continuous__search") is True
    assert agent._requires_approval(other, "search", "other__search") is True

    agent._server_tools["continuous"][0]["function"]["parameters"] = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "extra": {"type": "string"},
        },
    }
    assert agent._requires_approval(continuous, "search", "continuous__search") is True


def test_built_in_os_approval_depends_on_write_capability(tmp_path: Path) -> None:
    agent = make_agent(tmp_path)
    read_only = McpServerConfig(name="os", built_in=True)
    writable = McpServerConfig(name="os", built_in=True, config={"allow_write_files": True})
    assert agent._requires_approval(read_only, "read_file", "os__read_file") is False
    assert agent._requires_approval(read_only, "write_file", "os__write_file") is False
    assert agent._requires_approval(writable, "write_file", "os__write_file") is True


def test_admin_auto_approval_cannot_bypass_built_in_write_approval(tmp_path: Path) -> None:
    contract = tool_contract_fingerprint("write_file", {})
    policy = McpPolicy(
        trusted_servers=(
            TrustedMcpServer(
                name="os",
                transport="stdio",
                command="unused",
                auto_approve_tools=(TrustedMcpToolApproval("write_file", contract),),
            ),
        )
    )
    agent = make_agent(tmp_path, mcp_policy=policy)
    writable = McpServerConfig(name="os", command="unused", built_in=True, config={"allow_write_files": True})
    assert agent._requires_approval(writable, "write_file", "os__write_file") is True


def test_external_stdio_server_is_blocked_without_admin_profile(tmp_path: Path) -> None:
    async def exercise() -> None:
        parent = AsyncExitStack(); await parent.__aenter__()
        agent = make_agent(tmp_path)
        server = McpServerConfig(name="external")
        with pytest.raises(PermissionError, match="trusted_servers"):
            await agent._start_server(server, parent)
        await parent.aclose()
    asyncio.run(exercise())


def test_admin_profile_allows_exact_stdio_server(tmp_path: Path) -> None:
    class Session:
        async def list_tools(self): return SimpleNamespace(tools=[])
    async def exercise() -> None:
        parent = AsyncExitStack(); await parent.__aenter__()
        policy = McpPolicy(
            trusted_servers=(
                TrustedMcpServer(
                    name="external",
                    transport="stdio",
                    command="/trusted/external-mcp",
                    args=("--project", "{workspace_directory}"),
                ),
            )
        )
        agent = make_agent(tmp_path, mcp_policy=policy)
        server = McpServerConfig(name="external")
        seen = []
        async def fake_connect(_stack, server_config):
            seen.append(server_config)
            return Session(), None
        agent._connect_server = fake_connect
        await agent._start_server(server, parent)
        assert "external" in agent._sessions
        assert seen[0].command == "/trusted/external-mcp"
        assert seen[0].args == ("--project", "{workspace_directory}")
        await parent.aclose()
    asyncio.run(exercise())


def test_system_prompt_contains_named_server_instructions_without_workspace_rule(
    tmp_path: Path,
) -> None:
    agent = make_agent(tmp_path)
    agent._server_instructions = {"documents": "Read only relevant pages."}
    agent._active_servers = {"documents"}
    prompt = agent._build_system_prompt()
    assert str(tmp_path.resolve()) not in prompt
    assert "Workspace-Tools" not in prompt
    assert "Projekt-Workspace" not in prompt
    assert "### MCP-Server documents\nRead only relevant pages." in prompt


def connected_agent(tmp_path: Path, model: RecordingModel):
    documents = McpServerConfig(name="documents", command="unused", built_in=True)
    agent = CliAgent(tmp_path, model, (documents,))
    session = FakeSession()
    agent._exit_stack = SimpleNamespace()
    agent._sessions = {"documents": session}
    agent._server_configs = {"documents": documents}
    agent._active_servers = {"documents"}
    agent._server_instructions = {"documents": "Document instructions"}
    agent._server_tools = {"documents": [{"function": {"name": "documents__read"}}]}
    agent._tool_routes = {"documents__read": (session, "read", documents)}
    agent.messages = [{"role": "system", "content": agent._build_system_prompt()}]
    return agent, session


def test_unknown_tool_call_is_reported_to_model(tmp_path: Path) -> None:
    model = RecordingModel([{"role": "assistant", "content": "", "tool_calls": [{"id": "invented-call", "function": {"name": "invented__tool", "arguments": {}}}]}, {"role": "assistant", "content": "I cannot use that tool."}])
    agent, _ = connected_agent(tmp_path, model)
    assert asyncio.run(agent.ask("Call an invented tool")) == "I cannot use that tool."
    error = model.calls[1][0][-1]
    assert error["role"] == "tool"
    assert error["tool_call_id"] == "invented-call"
    assert "existiert nicht" in error["content"]


def test_invalid_json_tool_arguments_are_rejected(tmp_path: Path) -> None:
    model = RecordingModel([{"role": "assistant", "content": "", "tool_calls": [{"id": "invalid", "function": {"name": "documents__read", "arguments": "not-json"}}]}, {"role": "assistant", "content": "Invalid tool call rejected."}])
    agent, session = connected_agent(tmp_path, model)
    assert asyncio.run(agent.ask("Use the document tool")) == "Invalid tool call rejected."
    assert session.tool_calls == []
    assert "gültigen JSON-Argumente" in model.calls[1][0][-1]["content"]


def test_unknown_server_command_does_not_reach_model(tmp_path: Path) -> None:
    model = RecordingModel(); agent, _ = connected_agent(tmp_path, model)
    with pytest.raises(ValueError, match="Unbekannter MCP-Server: missing"):
        asyncio.run(agent.ask("disable missing"))
    assert model.calls == []


def test_disabling_server_closes_transport_and_reenable_reconnects(tmp_path: Path) -> None:
    class Resource:
        def __init__(self) -> None: self.closed = False
        async def __aenter__(self): return self
        async def __aexit__(self, *_args) -> None: self.closed = True
    class Session:
        async def list_tools(self): return SimpleNamespace(tools=[])
    async def exercise() -> None:
        parent = AsyncExitStack(); await parent.__aenter__()
        policy = McpPolicy(
            trusted_servers=(
                TrustedMcpServer(
                    name="external",
                    transport="stdio",
                    command="/trusted/external-mcp",
                ),
            )
        )
        agent = make_agent(tmp_path, mcp_policy=policy); agent._exit_stack = parent
        config = McpServerConfig(name="external"); resources: list[Resource] = []
        async def fake_connect(stack, _server_config):
            resource = Resource(); resources.append(resource); await stack.enter_async_context(resource); return Session(), None
        agent._connect_server = fake_connect
        await agent._start_server(config, parent); agent._active_servers.add(config.name)
        await agent.disable_server(config.name)
        assert resources[0].closed is True; assert config.name not in agent._sessions
        await agent.enable_server(config.name)
        assert len(resources) == 2; assert resources[1].closed is False
        await parent.aclose(); assert resources[1].closed is True
    asyncio.run(exercise())


def test_approval_callback_missing_or_failing_is_fail_closed(
    tmp_path: Path,
) -> None:
    agent = make_agent(tmp_path)

    assert asyncio.run(agent._approve_tool_call("external__write", {})) is False

    async def failing_callback(_tool_name, _arguments):
        raise RuntimeError("approval backend failed")

    agent.approval_callback = failing_callback
    assert asyncio.run(agent._approve_tool_call("external__write", {})) is False


def test_session_approval_is_remembered_for_exact_exposed_tool(
    tmp_path: Path,
) -> None:
    async def approve_for_session(_tool_name, _arguments):
        return "session"

    agent = make_agent(tmp_path)
    agent.approval_callback = approve_for_session
    external = McpServerConfig(name="external", command="external-mcp")

    assert (
        asyncio.run(
            agent._approve_tool_call(
                "external__write",
                {"path": "a.txt"},
            )
        )
        is True
    )
    assert (
        agent._requires_approval(
            external,
            "write",
            "external__write",
        )
        is False
    )
    assert (
        agent._requires_approval(
            external,
            "delete",
            "external__delete",
        )
        is True
    )


def test_trusted_instructions_require_exact_server_identity(
    tmp_path: Path,
) -> None:
    policy = McpPolicy(
        trusted_servers=(
            TrustedMcpServer(
                name="docs",
                transport="stdio",
                command="/trusted/docs-mcp",
                trust_instructions=True,
            ),
        )
    )
    agent = make_agent(tmp_path, mcp_policy=policy)

    matching = McpServerConfig(
        name="docs",
        command="/trusted/docs-mcp",
    )
    wrong_command = McpServerConfig(
        name="docs",
        command="/other/docs-mcp",
    )
    built_in = McpServerConfig(
        name="os",
        built_in=True,
    )

    assert agent._instructions_are_trusted(matching) is True
    assert agent._instructions_are_trusted(wrong_command) is False
    assert agent._instructions_are_trusted(built_in) is True


def test_http_trusted_identity_fails_closed_on_invalid_runtime_placeholder(
    tmp_path: Path,
) -> None:
    trusted = TrustedMcpServer(
        name="docs",
        transport="streamable_http",
        url="https://mcp.internal/{config_file}",
    )
    agent = make_agent(tmp_path)
    server = McpServerConfig(
        name="docs",
        transport="streamable_http",
        url="https://mcp.internal/{config_file}",
    )

    assert agent._trusted_server_matches(server, trusted) is False


def test_auto_approval_fails_closed_when_current_contract_is_missing(
    tmp_path: Path,
) -> None:
    policy = McpPolicy(
        trusted_servers=(
            TrustedMcpServer(
                name="continuous",
                transport="stdio",
                command="external-mcp",
                auto_approve_tools=(
                    TrustedMcpToolApproval("search", "sha256:missing"),
                ),
            ),
        )
    )
    agent = make_agent(tmp_path, mcp_policy=policy)
    server = McpServerConfig(
        name="continuous",
        command="external-mcp",
    )

    assert agent._requires_approval(
        server,
        "search",
        "continuous__search",
    ) is True


def test_stdio_literal_args_bypass_runtime_placeholder_expansion(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    protected = str(tmp_path / "{config_file}" / "{workspace_directory}.md")
    server = McpServerConfig(
        name="os",
        command="{python}",
        args=("--config", "{config_file}"),
        literal_args=("--mutation-protected-path", protected),
        built_in=True,
    )
    agent = CliAgent(
        tmp_path,
        OllamaClient(base_url="http://localhost:11434", model="test"),
        (),
        config_file=tmp_path / "config.toml",
    )
    captured = {}

    class DummyContext:
        async def __aenter__(self):
            return object(), object()

        async def __aexit__(self, *_args):
            return None

    class DummySession:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            return None

        async def initialize(self):
            return SimpleNamespace(instructions=None)

    def fake_stdio_client(parameters):
        captured["parameters"] = parameters
        return DummyContext()

    monkeypatch.setattr("cli_agent.agent_mcp.stdio_client", fake_stdio_client)
    monkeypatch.setattr("cli_agent.agent_mcp.ClientSession", lambda *_args: DummySession())

    async def exercise() -> None:
        stack = AsyncExitStack()
        await stack.__aenter__()
        try:
            await agent._connect_server(stack, server)
        finally:
            await stack.aclose()

    asyncio.run(exercise())

    args = captured["parameters"].args
    assert args[0] == "--config"
    assert args[1] == str(tmp_path / "config.toml")
    assert args[-2:] == ["--mutation-protected-path", protected]


def test_json_response_format_adds_german_system_instruction(tmp_path: Path) -> None:
    agent = CliAgent(
        tmp_path,
        OllamaClient(base_url="http://localhost:11434", model="test"),
        (),
        response_format="json",
    )

    prompt = agent._build_system_prompt()

    assert "JSON als finales Antwortformat vorgeschrieben" in prompt
    assert "ausschließlich syntaktisch gültiges JSON" in prompt
    assert "Markdown-Codeblöcke" in prompt


def test_text_response_format_does_not_add_json_instruction(tmp_path: Path) -> None:
    agent = make_agent(tmp_path)

    assert "JSON als finales Antwortformat vorgeschrieben" not in agent._build_system_prompt()


def test_agent_rejects_unknown_response_format(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="response_format"):
        CliAgent(
            tmp_path,
            OllamaClient(base_url="http://localhost:11434", model="test"),
            (),
            response_format="yaml",
        )


def test_json_response_repair_keeps_tools_and_tool_history(tmp_path: Path) -> None:
    model = RecordingModel(
        [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "read-1",
                        "function": {
                            "name": "documents__read",
                            "arguments": {},
                        },
                    }
                ],
            },
            {"role": "assistant", "content": "not json"},
            {"role": "assistant", "content": '{"ok":true}'},
        ]
    )
    agent, session = connected_agent(tmp_path, model)
    agent.response_format = "json"
    agent._server_tools["documents"][0]["function"]["parameters"] = {
        "type": "object",
        "properties": {},
    }

    answer = asyncio.run(agent.ask("Lies das Dokument und antworte als JSON."))

    assert answer == '{"ok":true}'
    assert session.tool_calls == [("read", {})]
    assert len(model.calls) == 3
    repair_messages, repair_tools = model.calls[2]
    assert repair_tools
    assert any(message.get("role") == "tool" for message in repair_messages)
    assert "Korrigiere die Antwort jetzt" in repair_messages[-1]["content"]
    assert "verfügbaren Tools verwenden" in repair_messages[-1]["content"]


def test_json_response_format_fails_after_two_repairs(tmp_path: Path) -> None:
    model = RecordingModel(
        [
            {"role": "assistant", "content": "not json 1"},
            {"role": "assistant", "content": "not json 2"},
            {"role": "assistant", "content": "not json 3"},
        ]
    )
    agent = CliAgent(
        tmp_path,
        model,
        (),
        response_format="json",
    )
    agent._exit_stack = SimpleNamespace()

    with pytest.raises(ValueError, match="nach 2 Korrekturversuchen"):
        asyncio.run(agent.ask("Antworte als JSON."))

    assert len(model.calls) == 3


@pytest.mark.parametrize("invalid_json", ["NaN", "Infinity", "-Infinity"])
def test_json_response_format_repairs_non_standard_constants(
    tmp_path: Path,
    invalid_json: str,
) -> None:
    model = RecordingModel(
        [
            {"role": "assistant", "content": invalid_json},
            {"role": "assistant", "content": '{"ok":true}'},
        ]
    )
    agent = CliAgent(
        tmp_path,
        model,
        (),
        response_format="json",
    )
    agent._exit_stack = SimpleNamespace()

    assert asyncio.run(agent.ask("Antworte als JSON.")) == '{"ok":true}'
    assert len(model.calls) == 2


def test_json_repairs_share_main_tool_call_budget(tmp_path: Path) -> None:
    model = RecordingModel(
        [
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "read-1",
                        "function": {
                            "name": "documents__read",
                            "arguments": {},
                        },
                    }
                ],
            },
            {"role": "assistant", "content": "not json"},
            {
                "role": "assistant",
                "content": "",
                "tool_calls": [
                    {
                        "id": "read-2",
                        "function": {
                            "name": "documents__read",
                            "arguments": {},
                        },
                    }
                ],
            },
        ]
    )
    agent, session = connected_agent(tmp_path, model)
    agent.response_format = "json"
    agent.max_tool_calls = 1
    agent._server_tools["documents"][0]["function"]["parameters"] = {
        "type": "object",
        "properties": {},
    }

    with pytest.raises(RuntimeError, match="nach 1 Tool-Aufrufen"):
        asyncio.run(agent.ask("Lies das Dokument und antworte als JSON."))

    assert session.tool_calls == [("read", {})]
    assert len(model.calls) == 3


def test_system_prompt_without_tools_omits_tool_and_workspace_sections(
    tmp_path: Path,
) -> None:
    agent = make_agent(tmp_path)

    prompt = agent._build_system_prompt()

    assert "Nutze die bereitgestellten MCP-Tools" not in prompt
    assert "Aktuell verfügbare MCP-Tools" not in prompt
    assert "Aktuell sind keine MCP-Tools verfügbar" not in prompt
    assert "Projekt-Workspace" not in prompt
    assert "Workspace-Tools" not in prompt
    assert "Referenzkontext" not in prompt


def test_system_prompt_with_non_os_tool_omits_workspace_rule(
    tmp_path: Path,
) -> None:
    agent = make_agent(tmp_path)
    agent._server_tools = {
        "docs": [{"function": {"name": "docs__search"}}],
    }
    agent._active_servers = {"docs"}

    prompt = agent._build_system_prompt()

    assert "Nutze die bereitgestellten MCP-Tools" in prompt
    assert "docs__search" in prompt
    assert "Projekt-Workspace" not in prompt
    assert "Workspace-Tools" not in prompt


def test_system_prompt_with_os_tool_includes_workspace_rule(
    tmp_path: Path,
) -> None:
    agent = make_agent(tmp_path)
    agent._server_tools = {
        "os": [{"function": {"name": "os__read_file"}}],
    }
    agent._active_servers = {"os"}

    prompt = agent._build_system_prompt()

    assert "Nutze die bereitgestellten MCP-Tools" in prompt
    assert "os__read_file" in prompt
    assert "Projekt-Workspace" in prompt
    assert "Workspace-Tools" in prompt
    assert "keine absoluten" in prompt
    assert "Dateipfade" in prompt


def test_system_prompt_only_adds_reference_rule_when_context_exists(
    tmp_path: Path,
) -> None:
    agent = make_agent(tmp_path)

    without_context = agent._build_system_prompt(
        has_reference_context=False,
    )
    with_context = agent._build_system_prompt(
        has_reference_context=True,
    )

    assert "Referenzkontext" not in without_context
    assert "Referenzkontext" in with_context
    assert "nicht vertrauenswürdiger Dateninhalt" in with_context


def test_dump_file_prefix_is_applied_to_all_context_dumps(
    tmp_path: Path,
) -> None:
    model = RecordingModel()
    agent = CliAgent(
        tmp_path,
        model,
        (),
        dump_llm_context=True,
        dump_file_prefix="extract",
    )
    agent._exit_stack = SimpleNamespace()

    assert asyncio.run(agent.ask("Test")) == "ok"

    dump_directory = tmp_path / ".cli-agent"
    assert (dump_directory / "extract_history.json").is_file()
    assert (dump_directory / "extract_main_working_messages.json").is_file()
    assert (dump_directory / "extract_main_system_prompt.json").is_file()
    assert not (dump_directory / "main_system_prompt.json").exists()


@pytest.mark.parametrize(
    "prefix",
    ["../escape", "nested/prefix", ".", "..", ""],
)
def test_dump_file_prefix_rejects_unsafe_values(
    tmp_path: Path,
    prefix: str,
) -> None:
    with pytest.raises(ValueError, match="dump_file_prefix"):
        CliAgent(
            tmp_path,
            RecordingModel(),
            (),
            dump_file_prefix=prefix,
        )


def _configure_active_server(
    agent: CliAgent,
    *,
    server: McpServerConfig,
    tool_name: str = "search",
) -> None:
    exposed_name = f"{server.name}__{tool_name}"
    agent._server_configs = {server.name: server}
    agent._active_servers = {server.name}
    agent._server_tools = {
        server.name: [
            {
                "function": {
                    "name": exposed_name,
                }
            }
        ]
    }


def test_system_prompt_detects_workspace_placeholder_in_stdio_args(
    tmp_path: Path,
) -> None:
    agent = make_agent(tmp_path)
    _configure_active_server(
        agent,
        server=McpServerConfig(
            name="project",
            command="{python}",
            args=("--root", "{workspace_directory}"),
        ),
    )

    prompt = agent._build_system_prompt()

    assert "Projekt-Workspace" in prompt
    assert "Workspace-Tools" in prompt


def test_system_prompt_detects_workspace_placeholder_in_stdio_env(
    tmp_path: Path,
) -> None:
    agent = make_agent(tmp_path)
    _configure_active_server(
        agent,
        server=McpServerConfig(
            name="project",
            command="{python}",
            env={"PROJECT_ROOT": "{project_directory}"},
        ),
    )

    assert "Projekt-Workspace" in agent._build_system_prompt()


@pytest.mark.parametrize(
    ("url", "headers"),
    [
        ("https://mcp.example/{workspace_directory}", {}),
        (
            "https://mcp.example/api",
            {"X-Workspace": "{project_directory}"},
        ),
    ],
)
def test_system_prompt_detects_workspace_placeholder_in_http_config(
    tmp_path: Path,
    url: str,
    headers: dict[str, str],
) -> None:
    agent = make_agent(tmp_path)
    _configure_active_server(
        agent,
        server=McpServerConfig(
            name="remote",
            transport="streamable_http",
            url=url,
            headers=headers,
        ),
    )

    assert "Projekt-Workspace" in agent._build_system_prompt()


def test_system_prompt_normal_mcp_without_workspace_placeholder_omits_workspace_rule(
    tmp_path: Path,
) -> None:
    agent = make_agent(tmp_path)
    _configure_active_server(
        agent,
        server=McpServerConfig(
            name="remote",
            transport="streamable_http",
            url="https://mcp.example/api",
            headers={"X-Client": "cli-agent"},
        ),
    )

    prompt = agent._build_system_prompt()

    assert "Projekt-Workspace" not in prompt
    assert "Workspace-Tools" not in prompt


def test_system_prompt_ignores_workspace_placeholder_from_inactive_server(
    tmp_path: Path,
) -> None:
    agent = make_agent(tmp_path)
    workspace_server = McpServerConfig(
        name="project",
        command="{python}",
        args=("--root", "{workspace_directory}"),
    )
    active_server = McpServerConfig(
        name="docs",
        command="{python}",
    )
    agent._server_configs = {
        "project": workspace_server,
        "docs": active_server,
    }
    agent._active_servers = {"docs"}
    agent._server_tools = {
        "project": [{"function": {"name": "project__search"}}],
        "docs": [{"function": {"name": "docs__search"}}],
    }

    prompt = agent._build_system_prompt()

    assert "docs__search" in prompt
    assert "project__search" not in prompt
    assert "Projekt-Workspace" not in prompt


def test_system_prompt_does_not_infer_workspace_from_literal_args(
    tmp_path: Path,
) -> None:
    agent = make_agent(tmp_path)
    _configure_active_server(
        agent,
        server=McpServerConfig(
            name="project",
            command="{python}",
            literal_args=(
                "--mutation-protected-path",
                str(tmp_path / "prompt.md"),
            ),
        ),
    )

    assert "Projekt-Workspace" not in agent._build_system_prompt()
