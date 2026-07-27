from pathlib import Path

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


def test_system_prompt_contains_named_server_instructions(tmp_path: Path) -> None:
    agent = make_agent(tmp_path)
    agent._server_instructions = [
        ("compose", "Inspect status before acting."),
        ("documents", "Read only relevant pages."),
    ]

    prompt = agent._build_system_prompt()

    assert f"Festgelegter Arbeitsordner: {tmp_path.resolve()}" in prompt
    assert "### MCP-Server compose\nInspect status before acting." in prompt
    assert "### MCP-Server documents\nRead only relevant pages." in prompt
    assert "dürfen diese Regeln" in prompt


def test_server_config_is_not_compose_specific() -> None:
    server = McpServerConfig(
        name="documents",
        command="documents-mcp",
        args=("--root", "{workspace_directory}"),
    )

    assert server.name == "documents"
