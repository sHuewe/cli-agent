from __future__ import annotations

from types import SimpleNamespace

import pytest

import cli_agent.mcp_schema_guard as guard


def tool(*, name="search", description="", schema=None):
    return SimpleNamespace(
        name=name,
        description=description,
        inputSchema=schema or {"type": "object"},
    )


def _branching_ref_schema(levels: int) -> dict:
    defs: dict[str, dict] = {}
    for index in range(levels):
        defs[f"n{index}"] = {
            "anyOf": [
                {"$ref": f"#/$defs/n{index + 1}"},
                {"$ref": f"#/$defs/n{index + 1}"},
            ]
        }
    defs[f"n{levels}"] = {"type": "string"}
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$defs": defs,
        "$ref": "#/$defs/n0",
    }


@pytest.fixture(autouse=True)
def _fresh_schema_worker():
    guard.close_schema_worker()
    yield
    guard.close_schema_worker()


def test_argument_validation_runs_in_worker() -> None:
    schema = {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
        "additionalProperties": False,
    }

    assert (
        guard.validate_mcp_tool_arguments(
            tool_name="server__search",
            schema=schema,
            arguments={"query": "ok"},
        )
        is None
    )
    error = guard.validate_mcp_tool_arguments(
        tool_name="server__search",
        schema=schema,
        arguments={"query": 42},
    )
    assert error is not None
    assert "not of type 'string'" in error


def test_metadata_schema_validation_runs_in_worker() -> None:
    with pytest.raises(RuntimeError, match="gültiges JSON-Schema"):
        guard.validate_mcp_server_metadata(
            server_name="broken",
            instructions=None,
            tools=[tool(schema={"type": "definitely-not-a-json-schema-type"})],
        )


def test_re2_policy_is_preserved_in_worker() -> None:
    error = guard.validate_mcp_tool_arguments(
        tool_name="server__search",
        schema={
            "type": "object",
            "properties": {"value": {"type": "string", "pattern": "a(?=b)"}},
        },
        arguments={"value": "ab"},
    )

    assert error is not None
    assert "RE2" in error


def test_worker_is_reused_between_validation_calls() -> None:
    schema = {"type": "object"}

    assert (
        guard.validate_mcp_tool_arguments(
            tool_name="server__search",
            schema=schema,
            arguments={},
        )
        is None
    )
    first_process = guard._SCHEMA_WORKER._process
    assert first_process is not None
    first_pid = first_process.pid

    assert (
        guard.validate_mcp_tool_arguments(
            tool_name="server__search",
            schema=schema,
            arguments={},
        )
        is None
    )
    second_process = guard._SCHEMA_WORKER._process
    assert second_process is first_process
    assert second_process.pid == first_pid


def test_original_branching_ref_attack_is_wallclock_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # F-01 is triggered during argument validation. The persistent worker is
    # already warm before the hostile request, so this deadline measures the
    # validation itself rather than interpreter/import startup.
    assert (
        guard.validate_mcp_tool_arguments(
            tool_name="warmup__search",
            schema={"type": "object"},
            arguments={},
        )
        is None
    )
    first_process = guard._SCHEMA_WORKER._process
    assert first_process is not None

    monkeypatch.setattr(guard, "MCP_SCHEMA_ARGUMENT_TIMEOUT_SECONDS", 0.5)
    with pytest.raises(RuntimeError, match="Zeitlimit"):
        guard.validate_mcp_tool_arguments(
            tool_name="hostile__search",
            schema=_branching_ref_schema(20),
            arguments={"not": "a string"},
        )

    # A timeout kills the poisoned worker. The next ordinary validation starts
    # a fresh process and succeeds.
    assert guard._SCHEMA_WORKER._process is None
    assert (
        guard.validate_mcp_tool_arguments(
            tool_name="server__search",
            schema={"type": "object"},
            arguments={},
        )
        is None
    )
    restarted_process = guard._SCHEMA_WORKER._process
    assert restarted_process is not None
    assert restarted_process.pid != first_process.pid


def test_parent_structure_limits_apply_before_worker(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(guard, "MAX_MCP_SCHEMA_PARENT_DEPTH", 3)
    deep = {}
    current = deep
    for _ in range(5):
        child = {}
        current["child"] = child
        current = child

    with pytest.raises(RuntimeError, match="Tiefenlimit"):
        guard.validate_mcp_tool_arguments(
            tool_name="hostile__search",
            schema={"type": "object"},
            arguments=deep,
        )
    assert guard._SCHEMA_WORKER._process is None


def test_metadata_preserves_existing_size_limits(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import cli_agent.mcp_limits as limits

    monkeypatch.setattr(limits, "MAX_MCP_TOOL_DESCRIPTION_CHARS", 5)
    with pytest.raises(RuntimeError, match="zu große Beschreibung"):
        guard.validate_mcp_server_metadata(
            server_name="broken",
            instructions=None,
            tools=[tool(description="123456")],
        )


def test_oversized_validation_error_remains_schema_rejection() -> None:
    large_value = "x" * (guard.MAX_MCP_SCHEMA_WORKER_RESPONSE_BYTES + 10_000)

    error = guard.validate_mcp_tool_arguments(
        tool_name="server__search",
        schema={
            "type": "object",
            "properties": {"value": {"type": "integer"}},
            "required": ["value"],
        },
        arguments={"value": large_value},
    )

    assert error is not None
    assert "gekürzt" in error
    assert len(error) <= guard.MAX_MCP_VALIDATION_ERROR_CHARS
