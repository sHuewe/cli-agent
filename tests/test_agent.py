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


def test_system_prompt_contains_named_server_instructions_without_absolute_workspace(tmp_path: Path) -> None:
    agent = make_agent(tmp_path)
    agent._server_instructions = {"documents": "Read only relevant pages."}
    agent._active_servers = {"documents"}
    prompt = agent._build_system_prompt()
    assert str(tmp_path.resolve()) not in prompt
    assert "Alle Dateipfade für Workspace-Tools müssen relativ" in prompt
    assert "### MCP-Server documents\nRead only relevant pages." in prompt
    assert "dürfen diese Regeln" in prompt


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
