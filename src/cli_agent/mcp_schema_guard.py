"""Process-isolated JSON Schema validation for untrusted MCP data.

JSON Schema validation can become computationally expensive even when all regexes
are handled by RE2. Runtime MCP schema checks therefore execute ``jsonschema`` in
a persistent disposable Python worker. The worker is started once, but every
validation request still has a hard wall-clock timeout; a timed-out or broken
worker is killed and replaced on the next request.
"""

from __future__ import annotations

import atexit
import json
import logging
import os
import queue
import subprocess
import sys
import threading
from typing import Any

MCP_SCHEMA_WORKER_START_TIMEOUT_SECONDS = 10.0
MCP_SCHEMA_METADATA_TIMEOUT_SECONDS = 2.0
MCP_SCHEMA_ARGUMENT_TIMEOUT_SECONDS = 1.0
MAX_MCP_SCHEMA_WORKER_REQUEST_BYTES = 64_000_000
MAX_MCP_SCHEMA_WORKER_RESPONSE_BYTES = 64_000
MAX_MCP_VALIDATION_ERROR_CHARS = 8_000
MAX_MCP_SCHEMA_PARENT_NODES = 25_000
MAX_MCP_SCHEMA_PARENT_DEPTH = 128

_WORKER_ENV_NAMES = (
    "PATH",
    "HOME",
    "USERPROFILE",
    "SYSTEMROOT",
    "TEMP",
    "TMP",
    "LANG",
    "LC_ALL",
    "PYTHONIOENCODING",
)


def _worker_environment() -> dict[str, str]:
    environment = {
        name: os.environ[name]
        for name in _WORKER_ENV_NAMES
        if os.environ.get(name) is not None
    }
    environment["PYTHONSAFEPATH"] = "1"
    return environment


def _validate_parent_structure(value: Any, *, label: str) -> None:
    """Bound work done before the untrusted value reaches the worker."""

    nodes = 0
    stack: list[tuple[Any, int]] = [(value, 0)]
    while stack:
        current, depth = stack.pop()
        nodes += 1
        if nodes > MAX_MCP_SCHEMA_PARENT_NODES:
            raise RuntimeError(
                f"{label} überschreitet das Knotenlimit "
                f"({nodes} > {MAX_MCP_SCHEMA_PARENT_NODES})."
            )
        if depth > MAX_MCP_SCHEMA_PARENT_DEPTH:
            raise RuntimeError(
                f"{label} überschreitet das Tiefenlimit "
                f"({depth} > {MAX_MCP_SCHEMA_PARENT_DEPTH})."
            )
        if isinstance(current, dict):
            stack.extend((child, depth + 1) for child in current.values())
        elif isinstance(current, list):
            stack.extend((child, depth + 1) for child in current)


def _truncate_validation_error(message: str | None) -> str | None:
    """Keep schema rejections useful without overflowing the worker response."""

    if message is None or len(message) <= MAX_MCP_VALIDATION_ERROR_CHARS:
        return message
    marker = " … [Validierungsfehler gekürzt] … "
    remaining = MAX_MCP_VALIDATION_ERROR_CHARS - len(marker)
    head = remaining // 2
    tail = remaining - head
    return f"{message[:head]}{marker}{message[-tail:]}"


def _decode_worker_response(raw: bytes, *, operation: str) -> dict[str, Any]:
    if len(raw) > MAX_MCP_SCHEMA_WORKER_RESPONSE_BYTES:
        raise RuntimeError(f"{operation} lieferte eine unerwartet große Antwort.")
    try:
        response = json.loads(raw.decode("utf-8"))
    except (UnicodeError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"{operation} lieferte keine gültige Antwort.") from exc
    if not isinstance(response, dict) or not isinstance(response.get("ok"), bool):
        raise RuntimeError(f"{operation} lieferte keine gültige Antwort.")
    if not response["ok"]:
        message = response.get("error")
        if not isinstance(message, str) or not message:
            message = f"{operation} ist fehlgeschlagen."
        raise RuntimeError(message)
    return response


class _PersistentSchemaWorker:
    """Serialize requests through one reusable process with per-call deadlines."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._process: subprocess.Popen[bytes] | None = None
        self._responses: queue.Queue[bytes | None] | None = None
        self._reader: threading.Thread | None = None
        self._owner_pid = os.getpid()

    @staticmethod
    def _reader_loop(
        process: subprocess.Popen[bytes],
        responses: queue.Queue[bytes | None],
    ) -> None:
        stdout = process.stdout
        if stdout is None:
            responses.put(None)
            return
        try:
            while True:
                line = stdout.readline(MAX_MCP_SCHEMA_WORKER_RESPONSE_BYTES + 2)
                if not line:
                    break
                responses.put(line)
        except (OSError, ValueError):
            pass
        finally:
            responses.put(None)

    def _discard_inherited_state_locked(self) -> None:
        """Do not let a forked child reuse or kill its parent's worker."""

        if self._owner_pid == os.getpid():
            return
        process = self._process
        if process is not None:
            for stream in (process.stdin, process.stdout):
                if stream is not None:
                    try:
                        stream.close()
                    except OSError:
                        pass
        self._process = None
        self._responses = None
        self._reader = None
        self._owner_pid = os.getpid()

    def _stop_locked(self) -> None:
        process = self._process
        self._process = None
        self._responses = None
        self._reader = None
        if process is None:
            return
        if process.poll() is None:
            try:
                process.kill()
            except OSError:
                pass
        try:
            process.wait(timeout=1.0)
        except (OSError, subprocess.TimeoutExpired):
            pass
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                try:
                    stream.close()
                except OSError:
                    pass

    def _start_locked(self) -> None:
        self._discard_inherited_state_locked()
        process = self._process
        if process is not None and process.poll() is None:
            return
        self._stop_locked()

        try:
            process = subprocess.Popen(
                [sys.executable, "-m", "cli_agent.mcp_schema_guard"],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                bufsize=0,
                env=_worker_environment(),
            )
        except OSError as exc:
            raise RuntimeError(
                "MCP-JSON-Schema-Worker konnte nicht gestartet werden."
            ) from exc

        responses: queue.Queue[bytes | None] = queue.Queue()
        reader = threading.Thread(
            target=self._reader_loop,
            args=(process, responses),
            name="cli-agent-mcp-schema-worker-reader",
            daemon=True,
        )
        self._process = process
        self._responses = responses
        self._reader = reader
        reader.start()

        try:
            ready = responses.get(timeout=MCP_SCHEMA_WORKER_START_TIMEOUT_SECONDS)
        except queue.Empty as exc:
            self._stop_locked()
            raise RuntimeError(
                "MCP-JSON-Schema-Worker überschreitet das Start-Zeitlimit von "
                f"{MCP_SCHEMA_WORKER_START_TIMEOUT_SECONDS:g} Sekunden."
            ) from exc

        if ready is None:
            self._stop_locked()
            raise RuntimeError("MCP-JSON-Schema-Worker wurde unerwartet beendet.")
        try:
            handshake = json.loads(ready.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            self._stop_locked()
            raise RuntimeError(
                "MCP-JSON-Schema-Worker lieferte keinen gültigen Start-Handshake."
            ) from exc
        if handshake != {"ready": True}:
            self._stop_locked()
            raise RuntimeError(
                "MCP-JSON-Schema-Worker lieferte keinen gültigen Start-Handshake."
            )

    def request(
        self,
        request: dict[str, Any],
        *,
        timeout_seconds: float,
        operation: str,
    ) -> dict[str, Any]:
        _validate_parent_structure(request, label=operation)
        try:
            payload = json.dumps(
                request,
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
        except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
            raise RuntimeError(
                f"{operation}: Validierungsdaten sind nicht sicher serialisierbar."
            ) from exc

        if len(payload) > MAX_MCP_SCHEMA_WORKER_REQUEST_BYTES:
            raise RuntimeError(
                f"{operation}: Validierungsdaten überschreiten das Prozesslimit "
                f"({len(payload)} > {MAX_MCP_SCHEMA_WORKER_REQUEST_BYTES} Bytes)."
            )

        with self._lock:
            for attempt in range(2):
                self._start_locked()
                process = self._process
                responses = self._responses
                if process is None or process.stdin is None or responses is None:
                    self._stop_locked()
                    raise RuntimeError(
                        f"{operation}: JSON-Schema-Worker ist nicht verfügbar."
                    )
                try:
                    process.stdin.write(payload + b"\n")
                    process.stdin.flush()
                    break
                except (BrokenPipeError, OSError):
                    self._stop_locked()
                    if attempt:
                        raise RuntimeError(
                            f"{operation}: JSON-Schema-Worker wurde unerwartet beendet."
                        )
            else:  # pragma: no cover - defensive; the loop either breaks or raises.
                raise RuntimeError(f"{operation}: JSON-Schema-Worker ist nicht verfügbar.")

            try:
                raw = responses.get(timeout=timeout_seconds)
            except queue.Empty as exc:
                self._stop_locked()
                raise RuntimeError(
                    f"{operation} überschreitet das Zeitlimit von "
                    f"{timeout_seconds:g} Sekunden."
                ) from exc

            if raw is None:
                self._stop_locked()
                raise RuntimeError(f"{operation} wurde unerwartet beendet.")
            try:
                return _decode_worker_response(raw, operation=operation)
            except RuntimeError:
                # A malformed/oversized protocol response makes the stream
                # untrustworthy; discard the process before propagating.
                self._stop_locked()
                raise

    def close(self) -> None:
        with self._lock:
            self._discard_inherited_state_locked()
            self._stop_locked()


_SCHEMA_WORKER = _PersistentSchemaWorker()


def close_schema_worker() -> None:
    """Stop the reusable validator worker, primarily for shutdown/tests."""

    _SCHEMA_WORKER.close()


atexit.register(close_schema_worker)


def _run_worker(
    request: dict[str, Any],
    *,
    timeout_seconds: float,
    operation: str,
) -> dict[str, Any]:
    return _SCHEMA_WORKER.request(
        request,
        timeout_seconds=timeout_seconds,
        operation=operation,
    )


def validate_mcp_server_metadata(
    *,
    server_name: str,
    instructions: str | None,
    tools: list[Any],
) -> None:
    """Apply cheap parent-side limits, then validate schemas in the worker."""

    from . import mcp_limits

    if instructions is not None and len(instructions) > mcp_limits.MAX_MCP_INSTRUCTIONS_CHARS:
        raise RuntimeError(
            f"MCP-Server {server_name!r} liefert zu große Instructions "
            f"({len(instructions)} > {mcp_limits.MAX_MCP_INSTRUCTIONS_CHARS} Zeichen)."
        )
    if len(tools) > mcp_limits.MAX_MCP_TOOLS_PER_SERVER:
        raise RuntimeError(
            f"MCP-Server {server_name!r} bietet zu viele Tools an "
            f"({len(tools)} > {mcp_limits.MAX_MCP_TOOLS_PER_SERVER})."
        )

    total_metadata_chars = 0
    seen_tool_names: set[str] = set()
    schemas: list[dict[str, Any]] = []
    for tool in tools:
        tool_name = str(getattr(tool, "name", "<unbekannt>"))
        if any(0xD800 <= ord(character) <= 0xDFFF for character in tool_name):
            raise RuntimeError(
                f"MCP-Server {server_name!r} liefert einen Toolnamen mit "
                "einem ungültigen Unicode-Surrogate."
            )
        if len(tool_name) > mcp_limits.MAX_MCP_TOOL_NAME_CHARS:
            raise RuntimeError(
                f"MCP-Server {server_name!r} liefert einen zu langen Toolnamen "
                f"({len(tool_name)} > {mcp_limits.MAX_MCP_TOOL_NAME_CHARS} Zeichen)."
            )
        if tool_name in seen_tool_names:
            raise RuntimeError(
                f"MCP-Server {server_name!r} bietet den Toolnamen {tool_name!r} mehrfach an."
            )
        seen_tool_names.add(tool_name)

        description = str(getattr(tool, "description", None) or "")
        if len(description) > mcp_limits.MAX_MCP_TOOL_DESCRIPTION_CHARS:
            raise RuntimeError(
                f"MCP-Tool {server_name}__{tool_name} hat eine zu große Beschreibung "
                f"({len(description)} > {mcp_limits.MAX_MCP_TOOL_DESCRIPTION_CHARS} Zeichen)."
            )
        schema = getattr(tool, "inputSchema", {})
        if not isinstance(schema, dict):
            raise RuntimeError(
                f"MCP-Tool {server_name}__{tool_name} liefert kein JSON-Objekt als Input-Schema."
            )
        _validate_parent_structure(
            schema,
            label=f"MCP-Tool {server_name}__{tool_name}: Input-Schema",
        )
        try:
            schema_text = json.dumps(
                schema,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
            raise RuntimeError(
                f"MCP-Tool {server_name}__{tool_name} liefert kein gültig serialisierbares Schema."
            ) from exc
        if len(schema_text) > mcp_limits.MAX_MCP_TOOL_SCHEMA_CHARS:
            raise RuntimeError(
                f"MCP-Tool {server_name}__{tool_name} hat ein zu großes Schema "
                f"({len(schema_text)} > {mcp_limits.MAX_MCP_TOOL_SCHEMA_CHARS} Zeichen)."
            )
        total_metadata_chars += len(description) + len(schema_text)
        if total_metadata_chars > mcp_limits.MAX_MCP_TOTAL_TOOL_METADATA_CHARS:
            raise RuntimeError(
                f"MCP-Server {server_name!r} liefert insgesamt zu viele Tool-Metadaten "
                f"({total_metadata_chars} > {mcp_limits.MAX_MCP_TOTAL_TOOL_METADATA_CHARS} Zeichen)."
            )
        schemas.append(
            {
                "tool_name": f"{server_name}__{tool_name}",
                "schema": schema,
            }
        )

    if schemas:
        _run_worker(
            {"operation": "validate_schemas", "schemas": schemas},
            timeout_seconds=MCP_SCHEMA_METADATA_TIMEOUT_SECONDS,
            operation=f"MCP-Server {server_name!r}: JSON-Schema-Prüfung",
        )


def validate_mcp_tool_arguments(
    *,
    tool_name: str,
    schema: dict[str, Any],
    arguments: dict[str, Any],
) -> str | None:
    _validate_parent_structure(
        schema,
        label=f"MCP-Tool {tool_name}: Input-Schema",
    )
    _validate_parent_structure(
        arguments,
        label=f"MCP-Tool {tool_name}: Argumente",
    )
    response = _run_worker(
        {
            "operation": "validate_arguments",
            "tool_name": tool_name,
            "schema": schema,
            "arguments": arguments,
        },
        timeout_seconds=MCP_SCHEMA_ARGUMENT_TIMEOUT_SECONDS,
        operation=f"MCP-Tool {tool_name}: JSON-Schema-Validierung",
    )
    validation_error = response.get("validation_error")
    if validation_error is not None and not isinstance(validation_error, str):
        raise RuntimeError(
            f"MCP-Tool {tool_name}: JSON-Schema-Validierung lieferte "
            "eine ungültige Antwort."
        )
    return validation_error


def _handle_request(request: Any) -> dict[str, Any]:
    from . import mcp_limits

    if not isinstance(request, dict):
        raise RuntimeError("Ungültige JSON-Schema-Worker-Anfrage.")

    operation = request.get("operation")
    if operation == "validate_schemas":
        schemas = request.get("schemas")
        if not isinstance(schemas, list):
            raise RuntimeError("Ungültige JSON-Schema-Worker-Anfrage.")
        for item in schemas:
            if not isinstance(item, dict):
                raise RuntimeError("Ungültige JSON-Schema-Worker-Anfrage.")
            tool_name = item.get("tool_name")
            schema = item.get("schema")
            if not isinstance(tool_name, str) or not isinstance(schema, dict):
                raise RuntimeError("Ungültige JSON-Schema-Worker-Anfrage.")
            mcp_limits._schema_validator(schema, tool_name=tool_name)
        return {"ok": True}

    if operation == "validate_arguments":
        tool_name = request.get("tool_name")
        schema = request.get("schema")
        arguments = request.get("arguments")
        if (
            not isinstance(tool_name, str)
            or not isinstance(schema, dict)
            or not isinstance(arguments, dict)
        ):
            raise RuntimeError("Ungültige JSON-Schema-Worker-Anfrage.")
        validation_error = mcp_limits.validate_mcp_tool_arguments(
            tool_name=tool_name,
            schema=schema,
            arguments=arguments,
        )
        return {
            "ok": True,
            "validation_error": _truncate_validation_error(validation_error),
        }

    raise RuntimeError("Unbekannte JSON-Schema-Worker-Operation.")


def _encode_worker_response(response: dict[str, Any]) -> bytes:
    encoded = json.dumps(response, ensure_ascii=False).encode("utf-8")
    if len(encoded) > MAX_MCP_SCHEMA_WORKER_RESPONSE_BYTES:
        encoded = json.dumps(
            {
                "ok": False,
                "error": "JSON-Schema-Worker-Antwort überschreitet das Limit.",
            }
        ).encode("utf-8")
    return encoded


def _worker_main() -> int:
    logging.disable(logging.CRITICAL)

    # Import expensive validator dependencies before the readiness handshake.
    # Startup time is deliberately separate from per-validation deadlines.
    try:
        from . import mcp_limits as _mcp_limits  # noqa: F401
    except Exception:
        return 1

    sys.stdout.buffer.write(b'{"ready":true}\n')
    sys.stdout.buffer.flush()

    while True:
        raw = sys.stdin.buffer.readline(MAX_MCP_SCHEMA_WORKER_REQUEST_BYTES + 2)
        if not raw:
            return 0
        if len(raw) > MAX_MCP_SCHEMA_WORKER_REQUEST_BYTES + 1:
            return 1
        if not raw.endswith(b"\n"):
            return 1

        try:
            request = json.loads(raw[:-1].decode("utf-8"))
            response = _handle_request(request)
        except RuntimeError as exc:
            response = {"ok": False, "error": str(exc)}
        except (
            UnicodeError,
            json.JSONDecodeError,
            TypeError,
            ValueError,
            RecursionError,
        ):
            response = {
                "ok": False,
                "error": "Ungültige JSON-Schema-Worker-Anfrage.",
            }
        except Exception:
            # Do not expose parser/validator internals or argument content.
            response = {
                "ok": False,
                "error": "JSON-Schema-Worker ist fehlgeschlagen.",
            }

        sys.stdout.buffer.write(_encode_worker_response(response) + b"\n")
        sys.stdout.buffer.flush()


if __name__ == "__main__":
    raise SystemExit(_worker_main())
