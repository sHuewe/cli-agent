"""Process-isolated JSON Schema validation for untrusted MCP data.

JSON Schema validation can become computationally expensive even when all regexes
are handled by RE2. Runtime MCP schema checks therefore execute ``jsonschema`` in
a disposable Python process with a hard wall-clock timeout.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from typing import Any

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


def _run_worker(
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

    try:
        result = subprocess.run(
            [sys.executable, "-m", "cli_agent.mcp_schema_guard"],
            input=payload,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            timeout=timeout_seconds,
            check=False,
            env=_worker_environment(),
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"{operation} überschreitet das Zeitlimit von "
            f"{timeout_seconds:g} Sekunden."
        ) from exc
    except OSError as exc:
        raise RuntimeError(f"{operation} konnte nicht gestartet werden.") from exc

    if result.returncode != 0:
        raise RuntimeError(f"{operation} wurde unerwartet beendet.")
    if len(result.stdout) > MAX_MCP_SCHEMA_WORKER_RESPONSE_BYTES:
        raise RuntimeError(f"{operation} lieferte eine unerwartet große Antwort.")

    try:
        response = json.loads(result.stdout.decode("utf-8"))
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


def validate_mcp_server_metadata(
    *,
    server_name: str,
    instructions: str | None,
    tools: list[Any],
) -> None:
    """Apply cheap parent-side limits, then validate schemas in one worker."""

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


def _main() -> int:
    logging.disable(logging.CRITICAL)
    try:
        raw = sys.stdin.buffer.read(MAX_MCP_SCHEMA_WORKER_REQUEST_BYTES + 1)
        if len(raw) > MAX_MCP_SCHEMA_WORKER_REQUEST_BYTES:
            raise RuntimeError("JSON-Schema-Worker-Eingabe überschreitet das Limit.")
        request = json.loads(raw.decode("utf-8"))
        response = _handle_request(request)
    except RuntimeError as exc:
        response = {"ok": False, "error": str(exc)}
    except (UnicodeError, json.JSONDecodeError, TypeError, ValueError, RecursionError):
        response = {"ok": False, "error": "Ungültige JSON-Schema-Worker-Anfrage."}
    except Exception:
        # Do not expose parser/validator internals or argument content.
        response = {"ok": False, "error": "JSON-Schema-Worker ist fehlgeschlagen."}

    encoded = json.dumps(response, ensure_ascii=False).encode("utf-8")
    if len(encoded) > MAX_MCP_SCHEMA_WORKER_RESPONSE_BYTES:
        encoded = json.dumps(
            {"ok": False, "error": "JSON-Schema-Worker-Antwort überschreitet das Limit."}
        ).encode("utf-8")
    sys.stdout.buffer.write(encoded)
    return 0


if __name__ == "__main__":
    raise SystemExit(_main())
