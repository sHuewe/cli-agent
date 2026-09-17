from __future__ import annotations

from pathlib import Path

from cli_agent.agent import CliAgent
from cli_agent.config import McpServerConfig
from cli_agent.ollama import OllamaClient


def _agent(tmp_path: Path) -> CliAgent:
    return CliAgent(
        tmp_path,
        OllamaClient(base_url="http://localhost:11434", model="test"),
        (),
    )


def test_built_in_move_file_requires_approval_when_os_write_is_enabled(tmp_path: Path) -> None:
    agent = _agent(tmp_path)
    writable = McpServerConfig(
        name="os",
        built_in=True,
        config={"allow_write_files": True},
    )
    read_only = McpServerConfig(name="os", built_in=True)

    assert agent._requires_approval(writable, "move_file", "os__move_file") is True
    assert agent._requires_approval(read_only, "move_file", "os__move_file") is False
