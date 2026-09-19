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


@pytest.mark.parametrize(
    "schema, keyword",
    [
        (
            {
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "pattern": "^(a+)+$",
                    }
                },
            },
            "pattern",
        ),
        (
            {
                "type": "object",
                "patternProperties": {
                    "^x-": {"type": "string"},
                },
            },
            "patternProperties",
        ),
        (
            {
                "type": "object",
                "properties": {
                    "payload": {
                        "anyOf": [
                            {"type": "string"},
                            {"type": "string", "pattern": "^(a+)+$"},
                        ]
                    }
                },
            },
            "pattern",
        ),
    ],
)
def test_regex_constraints_in_mcp_schemas_are_rejected(
    schema: dict,
    keyword: str,
) -> None:
    with pytest.raises(RuntimeError, match=keyword):
        limits.validate_mcp_server_metadata(
            server_name="external",
            instructions=None,
            tools=[tool(schema=schema)],
        )


def test_property_named_pattern_is_not_mistaken_for_schema_keyword() -> None:
    schema = {
        "type": "object",
        "properties": {
            "pattern": {
                "type": "string",
                "minLength": 1,
            }
        },
        "required": ["pattern"],
        "additionalProperties": False,
    }

    limits.validate_mcp_server_metadata(
        server_name="external",
        instructions=None,
        tools=[tool(schema=schema)],
    )


def test_non_regex_schema_constraints_remain_supported() -> None:
    schema = {
        "type": "object",
        "properties": {
            "mode": {"type": "string", "enum": ["read", "write"]},
            "name": {"type": "string", "minLength": 1, "maxLength": 100},
            "count": {"type": "integer", "minimum": 0, "maximum": 100},
        },
        "required": ["mode"],
        "additionalProperties": False,
    }

    limits.validate_mcp_server_metadata(
        server_name="external",
        instructions=None,
        tools=[tool(schema=schema)],
    )
    assert (
        limits.validate_mcp_tool_arguments(
            tool_name="external__search",
            schema=schema,
            arguments={"mode": "read", "name": "docs", "count": 1},
        )
        is None
    )


def test_regex_schema_is_rejected_before_argument_validation() -> None:
    schema = {
        "type": "object",
        "properties": {
            "value": {
                "type": "string",
                "pattern": "^(a+)+$",
            }
        },
    }

    with pytest.raises(RuntimeError, match="pattern"):
        limits.validate_mcp_tool_arguments(
            tool_name="external__search",
            schema=schema,
            arguments={"value": "a" * 24 + "!"},
        )


def test_draft7_tuple_items_with_regex_are_rejected() -> None:
    schema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "properties": {
            "values": {
                "type": "array",
                "items": [
                    {
                        "type": "string",
                        "pattern": "^(a+)+$",
                    }
                ],
            }
        },
    }

    with pytest.raises(RuntimeError, match="pattern"):
        limits.validate_mcp_server_metadata(
            server_name="external",
            instructions=None,
            tools=[tool(schema=schema)],
        )


def test_legacy_additional_items_with_regex_are_rejected() -> None:
    schema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "array",
        "items": [{"type": "string"}],
        "additionalItems": {
            "type": "string",
            "pattern": "^(a+)+$",
        },
    }

    with pytest.raises(RuntimeError, match="pattern"):
        limits.validate_mcp_server_metadata(
            server_name="external",
            instructions=None,
            tools=[tool(schema=schema)],
        )


def test_harmless_draft7_tuple_schema_remains_supported() -> None:
    schema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "array",
        "items": [
            {"type": "string", "minLength": 1},
            {"type": "integer", "minimum": 0},
        ],
        "additionalItems": False,
    }

    limits.validate_mcp_server_metadata(
        server_name="external",
        instructions=None,
        tools=[tool(schema=schema)],
    )


def test_local_ref_target_with_regex_is_rejected() -> None:
    schema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "$ref": "#/hidden",
        "hidden": {
            "type": "string",
            "pattern": "^(a+)+$",
        },
    }

    with pytest.raises(RuntimeError, match="pattern"):
        limits.validate_mcp_server_metadata(
            server_name="external",
            instructions=None,
            tools=[tool(schema=schema)],
        )


def test_local_ref_target_without_regex_remains_supported() -> None:
    schema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "$ref": "#/definitions/value",
        "definitions": {
            "value": {
                "type": "string",
                "minLength": 1,
            }
        },
    }

    limits.validate_mcp_server_metadata(
        server_name="external",
        instructions=None,
        tools=[tool(schema=schema)],
    )


def test_non_pointer_local_ref_is_rejected() -> None:
    schema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "$ref": "#named-anchor",
        "definitions": {
            "value": {"type": "string"},
        },
    }

    with pytest.raises(RuntimeError, match="kein JSON-Pointer-Fragment"):
        limits.validate_mcp_server_metadata(
            server_name="external",
            instructions=None,
            tools=[tool(schema=schema)],
        )


@pytest.mark.parametrize("keyword", ["$dynamicRef", "$recursiveRef"])
def test_dynamic_or_recursive_refs_are_rejected(keyword: str) -> None:
    schema = {
        keyword: "#",
    }

    with pytest.raises(RuntimeError, match="nicht zulässig"):
        limits.validate_mcp_server_metadata(
            server_name="external",
            instructions=None,
            tools=[tool(schema=schema)],
        )


def test_draft3_schema_is_rejected() -> None:
    schema = {
        "$schema": "http://json-schema.org/draft-03/schema#",
        "extends": {
            "type": "string",
            "pattern": "^(a+)+$",
        },
    }

    with pytest.raises(RuntimeError, match="Draft 3"):
        limits.validate_mcp_server_metadata(
            server_name="external",
            instructions=None,
            tools=[tool(schema=schema)],
        )


def test_harmless_draft3_schema_is_still_rejected() -> None:
    schema = {
        "$schema": "http://json-schema.org/draft-03/schema#",
        "type": "string",
    }

    with pytest.raises(RuntimeError, match="Draft 3"):
        limits.validate_mcp_server_metadata(
            server_name="external",
            instructions=None,
            tools=[tool(schema=schema)],
        )
