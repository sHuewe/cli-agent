from __future__ import annotations

import threading
import time
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
    assert "$.query" in error
    assert "validator=type" in error
    assert "42" not in error


def test_argument_validation_error_does_not_expose_rejected_value() -> None:
    secret = "SECRET-SENTINEL-DO-NOT-LOG"
    error = guard.validate_mcp_tool_arguments(
        tool_name="server__search",
        schema={
            "type": "object",
            "properties": {"query": {"type": "integer"}},
            "required": ["query"],
        },
        arguments={"query": secret},
    )

    assert error is not None
    assert "$.query" in error
    assert "validator=type" in error
    assert secret not in error


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
    assert "validator=type" in error
    assert large_value not in error
    assert len(error) <= guard.MAX_MCP_VALIDATION_ERROR_CHARS


def test_after_fork_child_replaces_inherited_locked_mutex() -> None:
    worker = guard._PersistentSchemaWorker()
    inherited_lock = worker._lock
    assert inherited_lock.acquire(blocking=False)
    try:
        worker._after_fork_child()
        assert worker._lock is not inherited_lock
        assert worker._lock.acquire(blocking=False)
        worker._lock.release()
    finally:
        inherited_lock.release()


def test_lock_wait_consumes_request_deadline() -> None:
    worker = guard._PersistentSchemaWorker()
    acquired = threading.Event()
    release = threading.Event()

    def hold_lock() -> None:
        with worker._lock:
            acquired.set()
            release.wait(timeout=1.0)

    holder = threading.Thread(target=hold_lock)
    holder.start()
    assert acquired.wait(timeout=1.0)
    started = time.monotonic()
    try:
        with pytest.raises(RuntimeError, match="Zeitlimit"):
            worker.request(
                {"operation": "validate_arguments"},
                timeout_seconds=0.05,
                operation="test-validation",
            )
    finally:
        release.set()
        holder.join(timeout=1.0)
    assert time.monotonic() - started < 0.5


def test_request_transmission_consumes_request_deadline() -> None:
    class SlowStdin:
        def __init__(self) -> None:
            self.closed = False

        def write(self, _data: bytes) -> int:
            time.sleep(0.2)
            return len(_data)

        def flush(self) -> None:
            return None

        def close(self) -> None:
            self.closed = True

    class FakeProcess:
        def __init__(self) -> None:
            self.stdin = SlowStdin()
            self.stdout = None
            self.killed = False

        def poll(self):
            return None if not self.killed else -9

        def kill(self) -> None:
            self.killed = True

        def wait(self, timeout=None):
            return -9

    worker = guard._PersistentSchemaWorker()
    process = FakeProcess()
    worker._process = process
    worker._responses = guard.queue.Queue()

    started = time.monotonic()
    with pytest.raises(RuntimeError, match="Zeitlimit"):
        worker.request(
            {"operation": "validate_arguments"},
            timeout_seconds=0.05,
            operation="test-validation",
        )
    assert time.monotonic() - started < 0.5
    assert process.killed is True
    assert worker._process is None


def test_partial_worker_pipe_writes_are_completed() -> None:
    class PartialStdin:
        def __init__(self) -> None:
            self.data = bytearray()

        def write(self, data: bytes) -> int:
            count = min(3, len(data))
            self.data.extend(data[:count])
            return count

        def flush(self) -> None:
            return None

    process = SimpleNamespace(stdin=PartialStdin())
    worker = guard._PersistentSchemaWorker()

    assert worker._write_payload_locked(
        process,
        b'abc123',
        deadline=time.monotonic() + 1.0,
        operation="test-validation",
        timeout_seconds=1.0,
    )
    assert bytes(process.stdin.data) == b'abc123\n'
