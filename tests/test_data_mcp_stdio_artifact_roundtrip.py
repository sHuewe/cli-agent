"""End-to-end read-only Data MCP artifact handoff over a real stdio process.

This test guards the integration that isolated unit tests do not cover:
built-in Data MCP -> protocol _meta -> agent-owned .cli-agent artifact ->
read-only Data MCP reads the newly generated artifact. Run on Linux and
Windows, including the Python 3.13 CI compatibility matrix.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from cli_agent.context.agent_knowledge import tool_result_text
from cli_agent.models.ollama import OllamaClient
from cli_agent.platform.execution import data_mcp_server_config
from cli_agent.runtime.agent import CliAgent
from cli_agent.security.mcp_result_artifacts import (
    METADATA_FOR_LLM_KEY,
    ONLY_METADATA_TO_LLM_KEY,
)


@pytest.mark.asyncio
async def test_read_only_stdio_data_mcp_can_chain_agent_written_artifacts(
    tmp_path: Path,
) -> None:
    config_file = tmp_path / "config.toml"
    config_file.write_text(
        '[model]\nprovider = "ollama"\nmodel = "test"\n'
        'base_url = "http://localhost:11434"\n',
        encoding="utf-8",
    )

    agent = CliAgent(
        tmp_path,
        OllamaClient(base_url="http://localhost:11434", model="test"),
        (data_mcp_server_config("read"),),
        config_file=config_file,
        workspace_access="read",
    )
    artifact: Path | None = None
    async with agent:
        session = agent._sessions["data"]
        produced = await session.call_tool(
            "wrap_data", {"content": "column,value\na,1\nb,2\n"}
        )
        assert produced.isError is not True
        assert produced.meta[ONLY_METADATA_TO_LLM_KEY] is True
        assert isinstance(produced.meta[METADATA_FOR_LLM_KEY], dict)

        model_metadata = json.loads(
            agent._result_artifacts.metadata_result(
                produced, tool_result_text(produced)
            )
        )
        assert model_metadata["row_count"] == 2
        assert "rows" not in model_metadata
        artifact = tmp_path / model_metadata["workspace_path"]
        assert artifact.is_file()
        assert artifact.parent.name.startswith("mcp-data-")
        assert artifact.parent.parent.name == ".cli-agent"

        # Only the agent writes artifacts. The Data MCP sees the granted
        # directory as readable even when its general access is read-only.
        readback = await session.call_tool(
            "preview_data",
            {
                "path": model_metadata["workspace_path"],
                "records_path": "/rows",
                "sample_rows": 2,
            },
        )
        assert readback.isError is not True
        assert 'column' in tool_result_text(readback)
        assert 'a' in tool_result_text(readback)
    assert artifact is not None
    assert not artifact.exists()
