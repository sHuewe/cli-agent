from __future__ import annotations

import asyncio
import json
from typing import Any, Awaitable, TypeVar

import re2
from jsonschema import ValidationError, validators
from jsonschema.exceptions import SchemaError
from jsonschema.validators import validator_for

# These limits are intentionally generous. They are last-resort safety bounds
# against broken or compromised MCP servers, not normal application quotas.
MAX_MCP_TOOLS_PER_SERVER = 1_024
MAX_MCP_TOOL_NAME_CHARS = 512
MAX_MCP_INSTRUCTIONS_CHARS = 2_000_000
MAX_MCP_TOOL_DESCRIPTION_CHARS = 250_000
MAX_MCP_TOOL_SCHEMA_CHARS = 2_000_000
MAX_MCP_TOTAL_TOOL_METADATA_CHARS = 20_000_000
MAX_MCP_TOOL_RESULT_CHARS = 10_000_000

# Lifecycle calls should fail reasonably quickly if a server is wedged, while
# normal tool calls get a deliberately much larger execution window.
MCP_INITIALIZE_TIMEOUT_SECONDS = 120.0
MCP_LIST_TOOLS_TIMEOUT_SECONDS = 120.0
MCP_TOOL_CALL_TIMEOUT_SECONDS = 600.0

_T = TypeVar("_T")


_SAFE_VALIDATOR_CLASSES: dict[type, type] = {}


def _safe_regex_search(pattern: str, text: str, *, tool_name: str):
    try:
        return re2.search(pattern, text)
    except re2.error as exc:
        raise RuntimeError(
            f"MCP-Tool {tool_name} verwendet einen regulären Ausdruck, "
            "der von der sicheren RE2-Engine nicht unterstützt wird."
        ) from exc


def _safe_pattern(validator, pattern, instance, schema):
    if not validator.is_type(instance, "string"):
        return
    try:
        matched = re2.search(pattern, instance)
    except re2.error as exc:
        yield ValidationError(
            "Der JSON-Schema-Regex wird von der sicheren RE2-Engine "
            f"nicht unterstützt: {exc}"
        )
        return
    if not matched:
        yield ValidationError(f"{instance!r} does not match {pattern!r}")


def _safe_pattern_properties(validator, pattern_properties, instance, schema):
    if not validator.is_type(instance, "object"):
        return

    for pattern, subschema in pattern_properties.items():
        try:
            compiled = re2.compile(pattern)
        except re2.error as exc:
            yield ValidationError(
                "Der JSON-Schema-Regex wird von der sicheren RE2-Engine "
                f"nicht unterstützt: {exc}"
            )
            continue
        for key, value in instance.items():
            if compiled.search(key):
                yield from validator.descend(
                    value,
                    subschema,
                    path=key,
                    schema_path=pattern,
                )


def _safe_validator_class(base_validator: type) -> type:
    safe = _SAFE_VALIDATOR_CLASSES.get(base_validator)
    if safe is not None:
        return safe

    safe = validators.extend(
        base_validator,
        validators={
            "pattern": _safe_pattern,
            "patternProperties": _safe_pattern_properties,
        },
    )
    _SAFE_VALIDATOR_CLASSES[base_validator] = safe
    return safe


async def await_mcp_operation(
    awaitable: Awaitable[_T],
    *,
    timeout_seconds: float,
    operation: str,
) -> _T:
    """Await one MCP operation with an application-level deadline."""

    timeout = asyncio.timeout(timeout_seconds)
    try:
        async with timeout:
            return await awaitable
    except TimeoutError as exc:
        if not timeout.expired():
            raise
        raise RuntimeError(
            f"{operation} hat das Timeout von {timeout_seconds:g} Sekunden überschritten."
        ) from exc


def _reject_external_schema_references(schema: Any, *, tool_name: str) -> None:
    stack = [schema]
    reference_keys = {"$ref", "$dynamicRef", "$recursiveRef"}
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            for key, value in current.items():
                if key in reference_keys:
                    if not isinstance(value, str) or not value.startswith("#"):
                        raise RuntimeError(
                            f"MCP-Tool {tool_name} verwendet eine externe JSON-Schema-Referenz. "
                            "Nur lokale Schema-Referenzen sind zulässig."
                        )
                stack.append(value)
        elif isinstance(current, list):
            stack.extend(current)


def _schema_validator(schema: dict[str, Any], *, tool_name: str):
    _reject_external_schema_references(schema, tool_name=tool_name)
    try:
        validator_class = _safe_validator_class(validator_for(schema))
        validator_class.check_schema(schema)
    except SchemaError as exc:
        raise RuntimeError(
            f"MCP-Tool {tool_name} liefert kein gültiges JSON-Schema."
        ) from exc
    return validator_class(schema)


def validate_mcp_tool_arguments(
    *,
    tool_name: str,
    schema: dict[str, Any],
    arguments: dict[str, Any],
) -> str | None:
    """Return a concise validation error, or ``None`` for valid arguments."""

    validator = _schema_validator(schema, tool_name=tool_name)
    errors = sorted(
        validator.iter_errors(arguments),
        key=lambda error: (
            tuple(str(part) for part in error.absolute_path),
            str(error.validator),
            error.message,
        ),
    )
    if not errors:
        return None
    error = errors[0]
    location = "$"
    for part in error.absolute_path:
        if isinstance(part, int):
            location += f"[{part}]"
        else:
            location += f".{part}"
    return f"{location}: {error.message}"


def validate_mcp_server_metadata(
    *,
    server_name: str,
    instructions: str | None,
    tools: list[Any],
) -> None:
    if instructions is not None and len(instructions) > MAX_MCP_INSTRUCTIONS_CHARS:
        raise RuntimeError(
            f"MCP-Server {server_name!r} liefert zu große Instructions "
            f"({len(instructions)} > {MAX_MCP_INSTRUCTIONS_CHARS} Zeichen)."
        )
    if len(tools) > MAX_MCP_TOOLS_PER_SERVER:
        raise RuntimeError(
            f"MCP-Server {server_name!r} bietet zu viele Tools an "
            f"({len(tools)} > {MAX_MCP_TOOLS_PER_SERVER})."
        )

    total_metadata_chars = 0
    seen_tool_names: set[str] = set()
    for tool in tools:
        tool_name = str(getattr(tool, "name", "<unbekannt>"))
        if len(tool_name) > MAX_MCP_TOOL_NAME_CHARS:
            raise RuntimeError(
                f"MCP-Server {server_name!r} liefert einen zu langen Toolnamen "
                f"({len(tool_name)} > {MAX_MCP_TOOL_NAME_CHARS} Zeichen)."
            )
        if tool_name in seen_tool_names:
            raise RuntimeError(
                f"MCP-Server {server_name!r} bietet den Toolnamen {tool_name!r} mehrfach an."
            )
        seen_tool_names.add(tool_name)

        description = str(getattr(tool, "description", None) or "")
        if len(description) > MAX_MCP_TOOL_DESCRIPTION_CHARS:
            raise RuntimeError(
                f"MCP-Tool {server_name}__{tool_name} hat eine zu große Beschreibung "
                f"({len(description)} > {MAX_MCP_TOOL_DESCRIPTION_CHARS} Zeichen)."
            )
        schema = getattr(tool, "inputSchema", {})
        if not isinstance(schema, dict):
            raise RuntimeError(
                f"MCP-Tool {server_name}__{tool_name} liefert kein JSON-Objekt als Input-Schema."
            )
        try:
            schema_text = json.dumps(
                schema,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        except (TypeError, ValueError) as exc:
            raise RuntimeError(
                f"MCP-Tool {server_name}__{tool_name} liefert kein gültig serialisierbares Schema."
            ) from exc
        if len(schema_text) > MAX_MCP_TOOL_SCHEMA_CHARS:
            raise RuntimeError(
                f"MCP-Tool {server_name}__{tool_name} hat ein zu großes Schema "
                f"({len(schema_text)} > {MAX_MCP_TOOL_SCHEMA_CHARS} Zeichen)."
            )
        _schema_validator(schema, tool_name=f"{server_name}__{tool_name}")
        total_metadata_chars += len(description) + len(schema_text)
        if total_metadata_chars > MAX_MCP_TOTAL_TOOL_METADATA_CHARS:
            raise RuntimeError(
                f"MCP-Server {server_name!r} liefert insgesamt zu viele Tool-Metadaten "
                f"({total_metadata_chars} > {MAX_MCP_TOTAL_TOOL_METADATA_CHARS} Zeichen)."
            )


def enforce_mcp_tool_result_limit(text: str) -> str:
    if len(text) > MAX_MCP_TOOL_RESULT_CHARS:
        raise RuntimeError(
            "MCP-Tool-Ergebnis überschreitet das Sicherheitslimit "
            f"({len(text)} > {MAX_MCP_TOOL_RESULT_CHARS} Zeichen)."
        )
    return text
