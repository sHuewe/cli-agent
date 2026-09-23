from __future__ import annotations

import cli_agent.mcp_limits as limits
import cli_agent.mcp_schema_guard as guard


def setup_function() -> None:
    guard._WORKER_VALIDATOR_CACHE.clear()


def teardown_function() -> None:
    guard._WORKER_VALIDATOR_CACHE.clear()


def test_schema_validation_timeouts_have_safety_margin() -> None:
    assert guard.MCP_SCHEMA_ARGUMENT_TIMEOUT_SECONDS == 3.0
    assert guard.MCP_SCHEMA_METADATA_TIMEOUT_SECONDS == 5.0


def test_worker_validator_cache_reuses_equivalent_schema(
    monkeypatch,
) -> None:
    original = limits._schema_validator
    calls = 0

    def counting_validator(schema, *, tool_name):
        nonlocal calls
        calls += 1
        return original(schema, tool_name=tool_name)

    monkeypatch.setattr(limits, "_schema_validator", counting_validator)

    first = {
        "type": "object",
        "properties": {
            "query": {"type": "string"},
            "limit": {"type": "integer"},
        },
    }
    # Same JSON Schema with deliberately different dict insertion order.
    second = {
        "properties": {
            "limit": {"type": "integer"},
            "query": {"type": "string"},
        },
        "type": "object",
    }

    validator_one = guard._worker_schema_validator(
        first,
        tool_name="server__search",
    )
    validator_two = guard._worker_schema_validator(
        second,
        tool_name="server__search",
    )

    assert validator_two is validator_one
    assert calls == 1
    assert len(guard._WORKER_VALIDATOR_CACHE) == 1


def test_worker_validator_cache_distinguishes_schemas(monkeypatch) -> None:
    original = limits._schema_validator
    calls = 0

    def counting_validator(schema, *, tool_name):
        nonlocal calls
        calls += 1
        return original(schema, tool_name=tool_name)

    monkeypatch.setattr(limits, "_schema_validator", counting_validator)

    first = guard._worker_schema_validator(
        {"type": "object", "properties": {"value": {"type": "string"}}},
        tool_name="server__first",
    )
    second = guard._worker_schema_validator(
        {"type": "object", "properties": {"value": {"type": "integer"}}},
        tool_name="server__second",
    )

    assert second is not first
    assert calls == 2
    assert len(guard._WORKER_VALIDATOR_CACHE) == 2


def test_worker_validator_cache_is_lru_bounded(monkeypatch) -> None:
    monkeypatch.setattr(guard, "MAX_MCP_SCHEMA_VALIDATOR_CACHE_ENTRIES", 2)

    schema_a = {"type": "object", "properties": {"a": {"type": "string"}}}
    schema_b = {"type": "object", "properties": {"b": {"type": "string"}}}
    schema_c = {"type": "object", "properties": {"c": {"type": "string"}}}

    validator_a = guard._worker_schema_validator(schema_a, tool_name="server__a")
    guard._worker_schema_validator(schema_b, tool_name="server__b")

    # Refresh A so B becomes the least recently used entry.
    assert (
        guard._worker_schema_validator(schema_a, tool_name="server__a")
        is validator_a
    )
    guard._worker_schema_validator(schema_c, tool_name="server__c")

    assert len(guard._WORKER_VALIDATOR_CACHE) == 2

    key_a = guard.hashlib.sha256(
        guard._canonical_schema_text(schema_a).encode("utf-8")
    ).hexdigest()
    key_b = guard.hashlib.sha256(
        guard._canonical_schema_text(schema_b).encode("utf-8")
    ).hexdigest()
    key_c = guard.hashlib.sha256(
        guard._canonical_schema_text(schema_c).encode("utf-8")
    ).hexdigest()

    assert key_a in guard._WORKER_VALIDATOR_CACHE
    assert key_b not in guard._WORKER_VALIDATOR_CACHE
    assert key_c in guard._WORKER_VALIDATOR_CACHE


def test_cached_validator_is_used_for_argument_validation(monkeypatch) -> None:
    schema = {
        "type": "object",
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
        "additionalProperties": False,
    }

    validator = guard._worker_schema_validator(
        schema,
        tool_name="server__search",
    )

    def fail_if_rebuilt(*args, **kwargs):
        raise AssertionError("cached validator should prevent schema rebuild")

    monkeypatch.setattr(limits, "_schema_validator", fail_if_rebuilt)

    response = guard._handle_request(
        {
            "operation": "validate_arguments",
            "tool_name": "server__search",
            "schema": schema,
            "arguments": {"query": "ok"},
        }
    )

    assert response == {"ok": True, "validation_error": None}
    assert next(iter(guard._WORKER_VALIDATOR_CACHE.values()))[1] is validator
