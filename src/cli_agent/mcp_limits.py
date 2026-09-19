from __future__ import annotations

import asyncio
import json
from typing import Any, Awaitable, TypeVar
from urllib.parse import unquote

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


def _resolve_local_json_pointer(
    root_schema: Any,
    reference: str,
    *,
    tool_name: str,
) -> Any:
    fragment = unquote(reference[1:])
    if fragment == "":
        return root_schema
    if not fragment.startswith("/"):
        raise RuntimeError(
            f"MCP-Tool {tool_name} verwendet eine lokale JSON-Schema-Referenz "
            f"{reference!r}, die kein JSON-Pointer-Fragment ist. "
            "Nur lokale '#/…'-Referenzen sind zulässig."
        )

    current = root_schema
    for raw_part in fragment[1:].split("/"):
        part = raw_part.replace("~1", "/").replace("~0", "~")
        if isinstance(current, dict):
            if part not in current:
                raise RuntimeError(
                    f"MCP-Tool {tool_name} verwendet eine nicht auflösbare "
                    f"lokale JSON-Schema-Referenz {reference!r}."
                )
            current = current[part]
            continue
        if isinstance(current, list):
            try:
                index = int(part)
            except ValueError as exc:
                raise RuntimeError(
                    f"MCP-Tool {tool_name} verwendet eine nicht auflösbare "
                    f"lokale JSON-Schema-Referenz {reference!r}."
                ) from exc
            if index < 0 or index >= len(current):
                raise RuntimeError(
                    f"MCP-Tool {tool_name} verwendet eine nicht auflösbare "
                    f"lokale JSON-Schema-Referenz {reference!r}."
                )
            current = current[index]
            continue
        raise RuntimeError(
            f"MCP-Tool {tool_name} verwendet eine nicht auflösbare "
            f"lokale JSON-Schema-Referenz {reference!r}."
        )
    return current


def _local_schema_reference_targets(
    schema: Any,
    *,
    tool_name: str,
) -> list[Any]:
    """Return statically resolvable local $ref targets and reject unsafe refs."""

    stack = [schema]
    targets: list[Any] = []
    while stack:
        current = stack.pop()
        if isinstance(current, dict):
            for key, value in current.items():
                if key in {"$dynamicRef", "$recursiveRef"}:
                    raise RuntimeError(
                        f"MCP-Tool {tool_name} verwendet {key}. "
                        "Dynamische bzw. rekursive JSON-Schema-Referenzen sind "
                        "für MCP-Tool-Schemas nicht zulässig."
                    )
                if key == "$ref":
                    if not isinstance(value, str) or not value.startswith("#"):
                        raise RuntimeError(
                            f"MCP-Tool {tool_name} verwendet eine externe "
                            "JSON-Schema-Referenz. Nur lokale '#/…'-Referenzen "
                            "sind zulässig."
                        )
                    targets.append(
                        _resolve_local_json_pointer(
                            schema,
                            value,
                            tool_name=tool_name,
                        )
                    )
                stack.append(value)
        elif isinstance(current, list):
            stack.extend(current)
    return targets


def _reject_regex_schema_constraints(
    schema: Any,
    *,
    tool_name: str,
    additional_roots: list[Any] | None = None,
) -> None:
    """Reject JSON-Schema regex keywords from MCP-controlled schemas.

    Python's regular-expression engine can exhibit catastrophic backtracking.
    MCP servers control their tool schemas, so evaluating server-supplied
    pattern or patternProperties constraints would let an untrusted server
    consume CPU during local argument validation.

    The traversal follows standard schema-bearing keywords rather than blindly
    inspecting every dictionary. This avoids treating instance property names
    such as properties.pattern as JSON-Schema keywords.
    """

    single_schema_keywords = {
        "additionalItems",
        "additionalProperties",
        "contains",
        "contentSchema",
        "else",
        "if",
        "not",
        "propertyNames",
        "then",
        "unevaluatedItems",
        "unevaluatedProperties",
    }
    schema_array_keywords = {"allOf", "anyOf", "oneOf", "prefixItems"}
    schema_map_keywords = {
        "$defs",
        "definitions",
        "dependentSchemas",
        "properties",
    }

    stack: list[Any] = [schema, *(additional_roots or [])]
    seen_schema_nodes: set[int] = set()
    while stack:
        current = stack.pop()
        if not isinstance(current, dict):
            continue
        node_id = id(current)
        if node_id in seen_schema_nodes:
            continue
        seen_schema_nodes.add(node_id)

        if "pattern" in current:
            raise RuntimeError(
                f"MCP-Tool {tool_name} verwendet den JSON-Schema-Constraint "
                "'pattern'. Regex-basierte Constraints aus MCP-Schemas sind "
                "aus Ressourcenschutzgründen nicht zulässig."
            )
        if "patternProperties" in current:
            raise RuntimeError(
                f"MCP-Tool {tool_name} verwendet den JSON-Schema-Constraint "
                "'patternProperties'. Regex-basierte Constraints aus MCP-Schemas "
                "sind aus Ressourcenschutzgründen nicht zulässig."
            )

        for key, value in current.items():
            # Draft-04/06/07 allow tuple validation via an array-valued
            # "items". Newer drafts use a single schema here and
            # "prefixItems" for tuples. Support both forms because
            # validator_for() honors the schema's declared draft.
            if key == "items":
                if isinstance(value, (dict, bool)):
                    stack.append(value)
                elif isinstance(value, list):
                    stack.extend(
                        item for item in value if isinstance(item, (dict, bool))
                    )
                continue

            if key in single_schema_keywords:
                if isinstance(value, (dict, bool)):
                    stack.append(value)
                continue

            if key in schema_array_keywords:
                if isinstance(value, list):
                    stack.extend(
                        item for item in value if isinstance(item, (dict, bool))
                    )
                continue

            if key in schema_map_keywords:
                if isinstance(value, dict):
                    stack.extend(
                        item
                        for item in value.values()
                        if isinstance(item, (dict, bool))
                    )
                continue

            # In older drafts, dependencies may contain either property-name
            # arrays or schemas.
            if key == "dependencies" and isinstance(value, dict):
                stack.extend(
                    item
                    for item in value.values()
                    if isinstance(item, (dict, bool))
                )


def _schema_validator(schema: dict[str, Any], *, tool_name: str):
    reference_targets = _local_schema_reference_targets(
        schema,
        tool_name=tool_name,
    )
    _reject_regex_schema_constraints(
        schema,
        tool_name=tool_name,
        additional_roots=reference_targets,
    )
    try:
        validator_class = validator_for(schema)
        if validator_class.__name__ == "Draft3Validator":
            raise RuntimeError(
                f"MCP-Tool {tool_name} verwendet JSON Schema Draft 3. "
                "Dieser Legacy-Draft ist für MCP-Tool-Schemas nicht zulässig."
            )
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
