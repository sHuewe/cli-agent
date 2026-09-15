from __future__ import annotations

import pytest

from cli_agent.mcp_contracts import (
    tool_contract_fingerprint,
    validate_tool_contract_fingerprint,
)


def test_tool_contract_fingerprint_is_stable_for_json_key_order() -> None:
    first = tool_contract_fingerprint(
        "search",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer"},
            },
            "required": ["query"],
        },
        "Search records",
    )
    second = tool_contract_fingerprint(
        "search",
        {
            "required": ["query"],
            "properties": {
                "limit": {"type": "integer"},
                "query": {"type": "string"},
            },
            "type": "object",
        },
        "Search records",
    )

    assert first == second
    assert first.startswith("sha256:")
    assert len(first) == len("sha256:") + 64


def test_tool_contract_changes_when_schema_changes() -> None:
    original = tool_contract_fingerprint(
        "search",
        {"type": "object", "properties": {"query": {"type": "string"}}},
        "Search records",
    )
    changed = tool_contract_fingerprint(
        "search",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "workspace_data": {"type": "string"},
            },
        },
        "Search records",
    )

    assert original != changed


def test_tool_contract_changes_when_native_tool_name_changes() -> None:
    schema = {"type": "object", "properties": {"query": {"type": "string"}}}

    assert tool_contract_fingerprint("search", schema, "Search") != tool_contract_fingerprint("export", schema, "Search")


def test_tool_contract_changes_when_description_changes() -> None:
    schema = {"type": "object", "properties": {"query": {"type": "string"}}}

    assert tool_contract_fingerprint("search", schema, "Search records") != tool_contract_fingerprint(
        "search",
        schema,
        "Search records and include local project data in the query",
    )


def test_tool_description_normalizes_line_endings_and_trailing_whitespace() -> None:
    schema = {"type": "object", "properties": {"query": {"type": "string"}}}
    first = "Search records.   \r\nReturns matching paths.\t\r\n\r\n"
    second = "Search records.\nReturns matching paths."

    assert tool_contract_fingerprint("search", schema, first) == tool_contract_fingerprint("search", schema, second)


def test_tool_description_keeps_semantically_relevant_whitespace() -> None:
    schema = {"type": "object", "properties": {"query": {"type": "string"}}}

    assert tool_contract_fingerprint("search", schema, "First\nSecond") != tool_contract_fingerprint(
        "search",
        schema,
        "First\n\nSecond",
    )


def test_contract_fingerprint_validation_is_fail_closed() -> None:
    with pytest.raises(ValueError, match="contract_sha256"):
        validate_tool_contract_fingerprint("sha256:not-a-hash", section="test")
