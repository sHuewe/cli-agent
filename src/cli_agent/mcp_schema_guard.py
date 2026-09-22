"""Process-isolated JSON Schema validation for untrusted MCP metadata.

JSON Schema validation can become computationally expensive even when all regexes
are handled by RE2. The public MCP validation helpers therefore execute
``jsonschema`` in a disposable Python process with a hard wall-clock timeout.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
from typing import Any

MCP_SCHEMA_METADATA_TIMEOUT_SECONDS = 5.0
MCP_SCHEMA_ARGUMENT_TIMEOUT_SECONDS = 2.0
MAX_MCP_SCHEMA_WORKER_REQUEST_BYTES = 64_000_000
MAX_MCP_SCHEMA_WORKER_RESPONSE_BYTES = 64_000

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


def _run_worker(
    request: dict[str, Any],
    *,
    timeout_seconds: float,
    operation: str,
) -> dict[str, Any]:
    try:
        payload = json.dumps(
            request,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as exc:
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


def validate_schemas_in_worker(schemas: list[dict[str, Any]]) -> None:
    if not schemas:
        return
    _run_worker(
        {"operation": "validate_schemas", "schemas": schemas},
        timeout_seconds=MCP_SCHEMA_METADATA_TIMEOUT_SECONDS,
        operation="MCP-JSON-Schema-Prüfung",
    )


def validate_arguments_in_worker(
    *,
    tool_name: str,
    schema: dict[str, Any],
    arguments: dict[str, Any],
) -> str | None:
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
    # Import only in the worker. mcp_limits imports this module lazily from its
    # public functions, so there is no import cycle in the normal process.
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
        return {
            "ok": True,
            "validation_error": mcp_limits._validate_mcp_tool_arguments_direct(
                tool_name=tool_name,
                schema=schema,
                arguments=arguments,
            ),
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
    except (UnicodeError, json.JSONDecodeError, TypeError, ValueError):
        response = {"ok": False, "error": "Ungültige JSON-Schema-Worker-Anfrage."}
    except Exception:
        # Do not expose parser/validator internals or document content.
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
