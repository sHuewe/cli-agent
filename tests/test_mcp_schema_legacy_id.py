from __future__ import annotations

from types import SimpleNamespace

import pytest

import cli_agent.mcp_limits as limits


def _tool(schema: dict):
    return SimpleNamespace(
        name="search",
        description="",
        inputSchema=schema,
    )


@pytest.mark.parametrize(
    "draft_uri",
    [
        "http://json-schema.org/draft-03/schema#",
        "http://json-schema.org/draft-04/schema#",
    ],
)
def test_legacy_nested_id_resources_are_rejected(draft_uri: str) -> None:
    schema = {
        "$schema": draft_uri,
        "type": "object",
        "properties": {
            "value": {
                "id": "nested-resource",
                "type": "string",
            }
        },
    }

    with pytest.raises(RuntimeError, match="verschachteltes id"):
        limits.validate_mcp_server_metadata(
            server_name="hostile",
            instructions=None,
            tools=[_tool(schema)],
        )


@pytest.mark.parametrize(
    "draft_uri",
    [
        "http://json-schema.org/draft-03/schema#",
        "http://json-schema.org/draft-04/schema#",
    ],
)
def test_legacy_nested_dollar_id_is_allowed_as_extension(
    draft_uri: str,
) -> None:
    schema = {
        "$schema": draft_uri,
        "type": "object",
        "properties": {
            "value": {
                "$id": "ordinary-extension",
                "type": "string",
            }
        },
    }

    limits.validate_mcp_server_metadata(
        server_name="valid",
        instructions=None,
        tools=[_tool(schema)],
    )


def test_modern_id_annotation_is_not_treated_as_resource() -> None:
    schema = {
        "$schema": "http://json-schema.org/draft-07/schema#",
        "type": "object",
        "properties": {
            "value": {
                "id": "ordinary-annotation",
                "type": "string",
            }
        },
    }

    limits.validate_mcp_server_metadata(
        server_name="valid",
        instructions=None,
        tools=[_tool(schema)],
    )


@pytest.mark.parametrize(
    "draft_uri",
    [
        "http://json-schema.org/draft-06/schema#",
        "http://json-schema.org/draft-07/schema#",
        "https://json-schema.org/draft/2019-09/schema",
        "https://json-schema.org/draft/2020-12/schema",
    ],
)
def test_modern_nested_dollar_id_resources_are_rejected(
    draft_uri: str,
) -> None:
    schema = {
        "$schema": draft_uri,
        "type": "object",
        "properties": {
            "value": {
                "$id": "nested-resource",
                "type": "string",
            }
        },
    }

    with pytest.raises(RuntimeError, match=r"verschachteltes \$id"):
        limits.validate_mcp_server_metadata(
            server_name="hostile",
            instructions=None,
            tools=[_tool(schema)],
        )


def test_embedded_dialect_switch_uses_local_resource_keyword() -> None:
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$defs": {
            "legacy": {
                "$schema": "http://json-schema.org/draft-04/schema#",
                "$id": "ordinary-extension",
                "type": "string",
            }
        },
        "type": "object",
    }

    limits.validate_mcp_server_metadata(
        server_name="valid",
        instructions=None,
        tools=[_tool(schema)],
    )
