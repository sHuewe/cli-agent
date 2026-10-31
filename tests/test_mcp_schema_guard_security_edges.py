"""Behavioral regression tests for MCP schema and worker security boundaries.

These cases exercise fail-closed validation before untrusted data enters the
out-of-process schema validator, and validate the worker's protocol contract.
"""
from __future__ import annotations

import json
import os
from types import SimpleNamespace

import pytest

from cli_agent.security import mcp_limits
from cli_agent.security import mcp_schema_guard as guard


def tool(name="read", *, description="", schema=None):
    return SimpleNamespace(
        name=name, description=description,
        inputSchema={"type": "object"} if schema is None else schema,
    )


@pytest.mark.parametrize(
    ("limit_name", "limit", "instructions", "tools", "message"),
    [
        ("MAX_MCP_INSTRUCTIONS_CHARS", 2, "abc", [], "Instructions"),
        ("MAX_MCP_TOOLS_PER_SERVER", 1, None, [tool("one"), tool("two")], "zu viele Tools"),
        ("MAX_MCP_TOOL_NAME_CHARS", 2, None, [tool("long")], "Toolnamen"),
        ("MAX_MCP_TOOL_SCHEMA_CHARS", 2, None, [tool()], "zu großes Schema"),
        ("MAX_MCP_TOTAL_TOOL_METADATA_CHARS", 2, None, [tool()], "Tool-Metadaten"),
    ],
)
def test_metadata_limits_fail_before_subprocess(
    monkeypatch, limit_name, limit, instructions, tools, message,
):
    monkeypatch.setattr(mcp_limits, limit_name, limit)
    monkeypatch.setattr(
        guard, "_run_worker",
        lambda *args, **kwargs: pytest.fail("invalid metadata reached worker"),
    )
    with pytest.raises(RuntimeError, match=message):
        guard.validate_mcp_server_metadata(
            server_name="untrusted", instructions=instructions, tools=tools,
        )


@pytest.mark.parametrize(
    ("tools", "message"),
    [
        ([tool("duplicate"), tool("duplicate")], "mehrfach"),
        ([tool("\ud800")], "Surrogate"),
        ([tool(schema=[])], "JSON-Objekt"),
        ([tool(schema={"type": object()})], "serialisierbares Schema"),
    ],
)
def test_invalid_mcp_tool_metadata_is_rejected_before_worker(
    monkeypatch, tools, message,
):
    monkeypatch.setattr(
        guard, "_run_worker",
        lambda *args, **kwargs: pytest.fail("invalid metadata reached worker"),
    )
    with pytest.raises(RuntimeError, match=message):
        guard.validate_mcp_server_metadata(
            server_name="untrusted", instructions=None, tools=tools,
        )


def test_valid_metadata_forwards_all_tool_schemas_without_mutation(monkeypatch):
    calls = []
    monkeypatch.setattr(
        guard, "_run_worker",
        lambda request, **kwargs: calls.append((request, kwargs)) or {"ok": True},
    )
    left = tool("read", schema={"type": "object", "properties": {"path": {"type": "string"}}})
    right = tool("search", schema={"type": "object"})
    guard.validate_mcp_server_metadata(
        server_name="sandbox", instructions="reference data", tools=[left, right],
    )
    assert len(calls) == 1
    request, kwargs = calls[0]
    assert request == {
        "operation": "validate_schemas",
        "schemas": [
            {"tool_name": "sandbox__read", "schema": left.inputSchema},
            {"tool_name": "sandbox__search", "schema": right.inputSchema},
        ],
    }
    assert kwargs["timeout_seconds"] == guard.MCP_SCHEMA_METADATA_TIMEOUT_SECONDS


def test_empty_tool_metadata_never_starts_worker(monkeypatch):
    monkeypatch.setattr(
        guard, "_run_worker",
        lambda *args, **kwargs: pytest.fail("empty metadata started worker"),
    )
    guard.validate_mcp_server_metadata(
        server_name="empty", instructions=None, tools=[],
    )


@pytest.mark.parametrize("invalid", [3, [], {}, False])
def test_invalid_validation_error_response_fails_closed(monkeypatch, invalid):
    monkeypatch.setattr(guard, "_run_worker", lambda *a, **k: {"validation_error": invalid})
    with pytest.raises(RuntimeError, match="ungültige Antwort"):
        guard.validate_mcp_tool_arguments(
            tool_name="server__read", schema={"type": "object"}, arguments={},
        )


def test_arguments_are_bounded_before_schema_worker(monkeypatch):
    monkeypatch.setattr(guard, "MAX_MCP_SCHEMA_PARENT_DEPTH", 2)
    monkeypatch.setattr(
        guard, "_run_worker",
        lambda *args, **kwargs: pytest.fail("deep arguments reached worker"),
    )
    with pytest.raises(RuntimeError, match="Tiefenlimit"):
        guard.validate_mcp_tool_arguments(
            tool_name="server__read", schema={"type": "object"},
            arguments={"deep": {"deeper": {"too_deep": True}}},
        )


@pytest.mark.parametrize(
    ("worker_request", "error"),
    [
        (None, "Ungültige"),
        ({"operation": "validate_schemas", "schemas": {}}, "Ungültige"),
        ({"operation": "validate_schemas", "schemas": [None]}, "Ungültige"),
        ({"operation": "validate_schemas", "schemas": [{}]}, "Ungültige"),
        ({"operation": "validate_schemas", "schemas": [{"tool_name": 42, "schema": {}}]}, "Ungültige"),
        ({"operation": "validate_arguments", "tool_name": 42, "schema": {}, "arguments": {}}, "Ungültige"),
        ({"operation": "validate_arguments", "tool_name": "safe", "schema": [], "arguments": {}}, "Ungültige"),
        ({"operation": "validate_arguments", "tool_name": "safe", "schema": {}, "arguments": []}, "Ungültige"),
        ({"operation": "unknown"}, "Unbekannte"),
    ],
)
def test_schema_worker_rejects_malformed_protocol_requests(worker_request, error):
    with pytest.raises(RuntimeError, match=error):
        guard._handle_request(worker_request)


def test_schema_worker_accepts_schema_batch():
    assert guard._handle_request({
        "operation": "validate_schemas",
        "schemas": [{"tool_name": "server__read", "schema": {"type": "object"}}],
    }) == {"ok": True}


def test_oversized_worker_response_is_replaced_with_bounded_error(monkeypatch):
    monkeypatch.setattr(guard, "MAX_MCP_SCHEMA_WORKER_RESPONSE_BYTES", 100)
    payload = guard._encode_worker_response({"ok": True, "result": "secret" * 100})
    response = json.loads(payload)
    assert response["ok"] is False
    assert "Limit" in response["error"]
    assert "secret" not in payload.decode()


def test_untrusted_worker_request_rejects_unserializable_and_large_payload(monkeypatch):
    worker = guard._PersistentSchemaWorker()
    with pytest.raises(RuntimeError, match="serialisierbar"):
        worker.request({"x": object()}, timeout_seconds=0.1, operation="safe")
    monkeypatch.setattr(guard, "MAX_MCP_SCHEMA_WORKER_REQUEST_BYTES", 8)
    with pytest.raises(RuntimeError, match="Prozesslimit"):
        worker.request({"payload": "too-big"}, timeout_seconds=0.1, operation="safe")
    assert worker._process is None


def test_worker_environment_does_not_inherit_secrets(monkeypatch):
    monkeypatch.setenv("MCP_PRIVATE_KEY", "secret-not-for-child")
    monkeypatch.setenv("PYTHONPATH", "unsafe-path")
    environment = guard._worker_environment()
    assert "MCP_PRIVATE_KEY" not in environment
    assert "PYTHONPATH" not in environment
    assert environment["PYTHONSAFEPATH"] == "1"
