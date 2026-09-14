from __future__ import annotations

from types import SimpleNamespace

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
    )

    assert first == second
    assert first.startswith("sha256:")
    assert len(first) == len("sha256:") + 64


def test_tool_contract_changes_when_schema_changes() -> None:
    original = tool_contract_fingerprint(
        "search",
        {"type": "object", "properties": {"query": {"type": "string"}}},
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
    )

    assert original != changed


def test_tool_contract_changes_when_native_tool_name_changes() -> None:
    schema = {"type": "object", "properties": {"query": {"type": "string"}}}

    assert tool_contract_fingerprint("search", schema) != tool_contract_fingerprint("export", schema)


def test_tool_description_is_intentionally_not_part_of_contract() -> None:
    schema = {"type": "object", "properties": {"query": {"type": "string"}}}
    first = SimpleNamespace(name="search", description="old", inputSchema=schema)
    second = SimpleNamespace(name="search", description="new prompt text", inputSchema=schema)

    assert tool_contract_fingerprint(first.name, first.inputSchema) == tool_contract_fingerprint(second.name, second.inputSchema)


def test_contract_fingerprint_validation_is_fail_closed() -> None:
    with pytest.raises(ValueError, match="contract_sha256"):
        validate_tool_contract_fingerprint("sha256:not-a-hash", section="test")
