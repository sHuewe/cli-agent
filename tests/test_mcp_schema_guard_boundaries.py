"""Boundary and error-message tests for the MCP schema worker protocol."""

import json

import pytest

from cli_agent.security import mcp_schema_guard as guard


@pytest.mark.parametrize(
    ("raw", "message"),
    [
        (b"", "keine gültige Antwort"),
        (b"not-json", "keine gültige Antwort"),
        (b"[]", "keine gültige Antwort"),
        (b'{"ok":"yes"}', "keine gültige Antwort"),
        (b'{"ok":false}', "ist fehlgeschlagen"),
        (b'{"ok":false,"error":42}', "ist fehlgeschlagen"),
        (b'{"ok":false,"error":"rejected"}', "rejected"),
    ],
)
def test_worker_protocol_rejects_malformed_responses(raw, message):
    with pytest.raises(RuntimeError, match=message):
        guard._decode_worker_response(raw, operation="schema-check")


def test_worker_protocol_rejects_oversized_response(monkeypatch):
    monkeypatch.setattr(guard, "MAX_MCP_SCHEMA_WORKER_RESPONSE_BYTES", 10)
    with pytest.raises(RuntimeError, match="große Antwort"):
        guard._decode_worker_response(b"x" * 11, operation="schema-check")


def test_worker_protocol_accepts_valid_response():
    response = {"ok": True, "validation_error": None}
    assert guard._decode_worker_response(
        json.dumps(response).encode("utf-8"), operation="schema-check"
    ) == response


def test_error_truncation_preserves_both_ends_without_exceeding_budget():
    bound = guard.MAX_MCP_VALIDATION_ERROR_CHARS
    message = "prefix-" + "x" * (bound * 2) + "-suffix"
    truncated = guard._truncate_validation_error(message)
    assert truncated is not None
    assert len(truncated) == bound
    assert truncated.startswith("prefix-")
    assert truncated.endswith("-suffix")
    assert "gekürzt" in truncated


def test_error_truncation_keeps_short_and_missing_messages():
    assert guard._truncate_validation_error(None) is None
    assert guard._truncate_validation_error("short") == "short"


@pytest.mark.parametrize("value", [{"a": [1, 2]}, [{"x": True}], 5, None])
def test_parent_structure_accepts_small_json_data(value):
    guard._validate_parent_structure(value, label="schema-check")


def test_parent_structure_enforces_node_limit(monkeypatch):
    monkeypatch.setattr(guard, "MAX_MCP_SCHEMA_PARENT_NODES", 3)
    with pytest.raises(RuntimeError, match="Knotenlimit"):
        guard._validate_parent_structure([1, 2, 3], label="schema-check")


def test_parent_structure_enforces_depth_limit(monkeypatch):
    monkeypatch.setattr(guard, "MAX_MCP_SCHEMA_PARENT_DEPTH", 2)
    with pytest.raises(RuntimeError, match="Tiefenlimit"):
        guard._validate_parent_structure([[[None]]], label="schema-check")
