from __future__ import annotations

import asyncio

from types import SimpleNamespace

import pytest

import cli_agent.mcp_limits as limits


def tool(*, name="search", description="", schema=None):
    return SimpleNamespace(
        name=name,
        description=description,
        inputSchema=schema or {"type": "object"},
    )


def test_mcp_operation_timeout_is_reported() -> None:
    async def slow():
        await asyncio.sleep(1)
        return "done"

    with pytest.raises(RuntimeError, match="initialize.*Timeout"):
        asyncio.run(
            limits.await_mcp_operation(
                slow(),
                timeout_seconds=0.01,
                operation="initialize",
            )
        )


def test_inner_timeout_error_is_not_relabelled_as_application_timeout() -> None:
    async def fail():
        raise TimeoutError("inner timeout")

    with pytest.raises(TimeoutError, match="inner timeout"):
        asyncio.run(
            limits.await_mcp_operation(
                fail(),
                timeout_seconds=1,
                operation="call_tool",
            )
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


def test_tool_name_length_limit_is_enforced() -> None:
    limits.validate_mcp_server_metadata(
        server_name="valid",
        instructions=None,
        tools=[tool(name="t" * limits.MAX_MCP_TOOL_NAME_CHARS)],
    )

    with pytest.raises(RuntimeError, match="zu langen Toolnamen"):
        limits.validate_mcp_server_metadata(
            server_name="broken",
            instructions=None,
            tools=[tool(name="t" * (limits.MAX_MCP_TOOL_NAME_CHARS + 1))],
        )


def test_duplicate_tool_names_are_rejected() -> None:
    with pytest.raises(RuntimeError, match="mehrfach"):
        limits.validate_mcp_server_metadata(
            server_name="broken",
            instructions=None,
            tools=[tool(name="search"), tool(name="search")],
        )


def test_invalid_and_external_ref_schemas_are_rejected() -> None:
    with pytest.raises(RuntimeError, match="gültiges JSON-Schema"):
        limits.validate_mcp_server_metadata(
            server_name="broken",
            instructions=None,
            tools=[tool(schema={"type": "definitely-not-a-json-schema-type"})],
        )

    with pytest.raises(RuntimeError, match="externe JSON-Schema-Referenz"):
        limits.validate_mcp_server_metadata(
            server_name="broken",
            instructions=None,
            tools=[tool(schema={"$ref": "https://example.invalid/schema.json"})],
        )


def test_tool_arguments_are_validated_against_schema() -> None:
    schema = {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
        "additionalProperties": False,
    }

    assert (
        limits.validate_mcp_tool_arguments(
            tool_name="server__search",
            schema=schema,
            arguments={"query": "test"},
        )
        is None
    )
    assert "Additional properties" in str(
        limits.validate_mcp_tool_arguments(
            tool_name="server__search",
            schema=schema,
            arguments={"query": "test", "extra": "unexpected"},
        )
    )
    assert "not of type 'string'" in str(
        limits.validate_mcp_tool_arguments(
            tool_name="server__search",
            schema=schema,
            arguments={"query": 123},
        )
    )
    assert "required property" in str(
        limits.validate_mcp_tool_arguments(
            tool_name="server__search",
            schema=schema,
            arguments={},
        )
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


def test_catastrophic_backtracking_pattern_is_handled_safely() -> None:
    schema = {
        "type": "object",
        "properties": {
            "value": {
                "type": "string",
                "pattern": "^(a+)+$",
            }
        },
        "required": ["value"],
        "additionalProperties": False,
    }

    error = limits.validate_mcp_tool_arguments(
        tool_name="external__search",
        schema=schema,
        arguments={"value": "a" * 100_000 + "!"},
    )

    assert error is not None
    assert "does not match" in error


def test_re2_incompatible_pattern_fails_closed() -> None:
    schema = {
        "type": "object",
        "properties": {
            "value": {
                "type": "string",
                "pattern": "a(?=b)",
            }
        },
    }

    error = limits.validate_mcp_tool_arguments(
        tool_name="external__search",
        schema=schema,
        arguments={"value": "ab"},
    )

    assert error is not None
    assert "RE2" in error


def test_pattern_properties_are_validated_with_re2() -> None:
    schema = {
        "type": "object",
        "patternProperties": {
            "^x-[a-z]+$": {"type": "integer"},
        },
        "additionalProperties": False,
    }

    assert (
        limits.validate_mcp_tool_arguments(
            tool_name="external__search",
            schema=schema,
            arguments={"x-count": 3},
        )
        is None
    )
    assert "not of type 'integer'" in str(
        limits.validate_mcp_tool_arguments(
            tool_name="external__search",
            schema=schema,
            arguments={"x-count": "three"},
        )
    )


def test_embedded_schema_dialect_keeps_re2_validation() -> None:
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "$defs": {
            "legacy": {
                "$schema": "http://json-schema.org/draft-07/schema#",
                "type": "string",
                "pattern": "a(?=b)",
            }
        },
        "properties": {
            "value": {"$ref": "#/$defs/legacy"},
        },
        "required": ["value"],
    }

    error = limits.validate_mcp_tool_arguments(
        tool_name="external__search",
        schema=schema,
        arguments={"value": "ab"},
    )

    assert error is not None
    assert "RE2" in error


def test_pattern_properties_with_additional_properties_uses_re2() -> None:
    schema = {
        "type": "object",
        "patternProperties": {
            "^(a+)+$": {},
        },
        "additionalProperties": False,
    }

    error = limits.validate_mcp_tool_arguments(
        tool_name="external__search",
        schema=schema,
        arguments={"a" * 100_000 + "!": 1},
    )

    assert error is not None
    assert "does not match any of the regexes" in error


def test_unevaluated_properties_draft202012_uses_re2() -> None:
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "patternProperties": {
            "^(a+)+$": {},
        },
        "unevaluatedProperties": False,
    }

    error = limits.validate_mcp_tool_arguments(
        tool_name="external__search",
        schema=schema,
        arguments={"a" * 100_000 + "!": 1},
    )

    assert error is not None
    assert "Unevaluated properties are not allowed" in error


def test_unevaluated_properties_draft201909_uses_re2() -> None:
    schema = {
        "$schema": "https://json-schema.org/draft/2019-09/schema",
        "type": "object",
        "patternProperties": {
            "^(a+)+$": {},
        },
        "unevaluatedProperties": False,
    }

    error = limits.validate_mcp_tool_arguments(
        tool_name="external__search",
        schema=schema,
        arguments={"a" * 100_000 + "!": 1},
    )

    assert error is not None
    assert "Unevaluated properties are not allowed" in error


def test_re2_incompatible_pattern_properties_fail_closed_everywhere() -> None:
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "patternProperties": {
            "a(?=b)": {},
        },
        "additionalProperties": False,
    }

    error = limits.validate_mcp_tool_arguments(
        tool_name="external__search",
        schema=schema,
        arguments={"ab": 1},
    )

    assert error is not None
    assert "RE2" in error


@pytest.mark.parametrize(
    "schema",
    [
        {
            "type": "object",
            "properties": {
                "value": {
                    "not": {
                        "pattern": "a(?=b)",
                    }
                }
            },
        },
        {
            "type": "object",
            "properties": {
                "value": {
                    "anyOf": [
                        {"type": "integer"},
                        {"pattern": "a(?=b)"},
                    ]
                }
            },
        },
        {
            "type": "object",
            "properties": {
                "value": {
                    "if": {"pattern": "a(?=b)"},
                    "then": {"type": "string"},
                }
            },
        },
    ],
)
def test_re2_incompatible_regex_is_not_masked_by_combinators(
    schema: dict,
) -> None:
    error = limits.validate_mcp_tool_arguments(
        tool_name="external__search",
        schema=schema,
        arguments={"value": "ab"},
    )

    assert error is not None
    assert "RE2" in error


def test_not_with_supported_pattern_keeps_json_schema_semantics() -> None:
    schema = {
        "type": "object",
        "properties": {
            "value": {
                "not": {
                    "pattern": "^ab$",
                }
            }
        },
    }

    error = limits.validate_mcp_tool_arguments(
        tool_name="external__search",
        schema=schema,
        arguments={"value": "ab"},
    )

    assert error is not None
    assert "should not be valid" in error
