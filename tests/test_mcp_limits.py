from __future__ import annotations

from types import SimpleNamespace

import pytest

import cli_agent.mcp_limits as limits


def tool(*, name="search", description="", schema=None):
    return SimpleNamespace(
        name=name,
        description=description,
        inputSchema=schema or {"type": "object"},
    )


def test_metadata_within_generous_defaults_is_accepted() -> None:
    limits.validate_mcp_server_metadata(
        server_name="large-but-valid",
        instructions="i" * 100_000,
        tools=[
            tool(
                name=f"tool_{index}",
                description="d" * 10_000,
                schema={"type": "object", "description": "s" * 10_000},
            )
            for index in range(100)
        ],
    )


def test_tool_count_limit_is_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(limits, "MAX_MCP_TOOLS_PER_SERVER", 2)

    with pytest.raises(RuntimeError, match="zu viele Tools"):
        limits.validate_mcp_server_metadata(
            server_name="broken",
            instructions=None,
            tools=[tool(name="a"), tool(name="b"), tool(name="c")],
        )


def test_instruction_limit_is_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(limits, "MAX_MCP_INSTRUCTIONS_CHARS", 10)

    with pytest.raises(RuntimeError, match="zu große Instructions"):
        limits.validate_mcp_server_metadata(
            server_name="broken",
            instructions="x" * 11,
            tools=[],
        )


def test_description_and_schema_limits_are_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(limits, "MAX_MCP_TOOL_DESCRIPTION_CHARS", 10)
    with pytest.raises(RuntimeError, match="zu große Beschreibung"):
        limits.validate_mcp_server_metadata(
            server_name="broken",
            instructions=None,
            tools=[tool(description="x" * 11)],
        )

    monkeypatch.setattr(limits, "MAX_MCP_TOOL_DESCRIPTION_CHARS", 100)
    monkeypatch.setattr(limits, "MAX_MCP_TOOL_SCHEMA_CHARS", 20)
    with pytest.raises(RuntimeError, match="zu großes Schema"):
        limits.validate_mcp_server_metadata(
            server_name="broken",
            instructions=None,
            tools=[tool(schema={"description": "x" * 30})],
        )


def test_total_metadata_limit_is_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(limits, "MAX_MCP_TOTAL_TOOL_METADATA_CHARS", 50)

    with pytest.raises(RuntimeError, match="insgesamt zu viele Tool-Metadaten"):
        limits.validate_mcp_server_metadata(
            server_name="broken",
            instructions=None,
            tools=[tool(description="x" * 40), tool(description="y" * 40)],
        )


def test_tool_result_limit_is_enforced(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(limits, "MAX_MCP_TOOL_RESULT_CHARS", 10)

    assert limits.enforce_mcp_tool_result_limit("1234567890") == "1234567890"
    with pytest.raises(RuntimeError, match="Sicherheitslimit"):
        limits.enforce_mcp_tool_result_limit("12345678901")
