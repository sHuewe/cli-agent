from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any, Awaitable, TypeVar

import re2
from jsonschema import (
    Draft3Validator,
    Draft4Validator,
    Draft6Validator,
    Draft7Validator,
    Draft201909Validator,
    Draft202012Validator,
    ValidationError,
    validators,
)
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


def _safe_pattern(validator, pattern, instance, schema):
    if not validator.is_type(instance, "string"):
        return
    compiled = _compile_safe_regex(pattern)
    if not compiled.search(instance):
        yield ValidationError(f"{instance!r} does not match {pattern!r}")


def _safe_pattern_properties(validator, pattern_properties, instance, schema):
    if not validator.is_type(instance, "object"):
        return

    for pattern, subschema in pattern_properties.items():
        compiled = _compile_safe_regex(pattern)
        for key, value in instance.items():
            if compiled.search(key):
                yield from validator.descend(
                    value,
                    subschema,
                    path=key,
                    schema_path=pattern,
                )


class _UnsupportedSafeRegex(ValueError):
    pass


def _compile_safe_regex(pattern: str):
    try:
        return re2.compile(pattern)
    except (re2.error, TypeError) as exc:
        raise _UnsupportedSafeRegex(str(exc)) from exc


def _extras_msg(extras):
    verb = "was" if len(extras) == 1 else "were"
    return ", ".join(repr(extra) for extra in extras), verb


def _is_valid(errors) -> bool:
    return next(errors, None) is None


def _safe_find_additional_properties(instance, schema):
    properties = schema.get("properties", {})
    patterns = [
        _compile_safe_regex(pattern)
        for pattern in schema.get("patternProperties", {})
    ]

    for property_name in instance:
        if property_name in properties:
            continue
        if any(pattern.search(property_name) for pattern in patterns):
            continue
        yield property_name


def _safe_additional_properties(validator, additional, instance, schema):
    if not validator.is_type(instance, "object"):
        return

    extras = set(_safe_find_additional_properties(instance, schema))

    if validator.is_type(additional, "object"):
        for extra in extras:
            yield from validator.descend(instance[extra], additional, path=extra)
    elif not additional and extras:
        if "patternProperties" in schema:
            verb = "does" if len(extras) == 1 else "do"
            joined = ", ".join(repr(each) for each in sorted(extras))
            patterns = ", ".join(
                repr(each) for each in sorted(schema["patternProperties"])
            )
            yield ValidationError(
                f"{joined} {verb} not match any of the regexes: {patterns}"
            )
        else:
            error = "Additional properties are not allowed (%s %s unexpected)"
            yield ValidationError(
                error % _extras_msg(sorted(extras, key=str))
            )


def _safe_find_evaluated_property_keys(validator, instance, schema):
    if validator.is_type(schema, "boolean"):
        return []

    evaluated_keys = []

    ref = schema.get("$ref")
    if ref is not None:
        resolved = validator._resolver.lookup(ref)
        evaluated_keys.extend(
            _safe_find_evaluated_property_keys(
                validator.evolve(
                    schema=resolved.contents,
                    _resolver=resolved.resolver,
                ),
                instance,
                resolved.contents,
            )
        )

    dynamic_ref = schema.get("$dynamicRef")
    if dynamic_ref is not None:
        resolved = validator._resolver.lookup(dynamic_ref)
        evaluated_keys.extend(
            _safe_find_evaluated_property_keys(
                validator.evolve(
                    schema=resolved.contents,
                    _resolver=resolved.resolver,
                ),
                instance,
                resolved.contents,
            )
        )

    properties = schema.get("properties")
    if validator.is_type(properties, "object"):
        evaluated_keys += properties.keys() & instance.keys()

    for keyword in ["additionalProperties", "unevaluatedProperties"]:
        subschema = schema.get(keyword)
        if subschema is None:
            continue
        evaluated_keys += (
            key
            for key, value in instance.items()
            if _is_valid(validator.descend(value, subschema))
        )

    if "patternProperties" in schema:
        compiled = [
            _compile_safe_regex(pattern)
            for pattern in schema["patternProperties"]
        ]
        for property_name in instance:
            for pattern in compiled:
                if pattern.search(property_name):
                    evaluated_keys.append(property_name)

    if "dependentSchemas" in schema:
        for property_name, subschema in schema["dependentSchemas"].items():
            if property_name not in instance:
                continue
            evaluated_keys += _safe_find_evaluated_property_keys(
                validator, instance, subschema
            )

    for keyword in ["allOf", "oneOf", "anyOf"]:
        for subschema in schema.get(keyword, []):
            if not _is_valid(validator.descend(instance, subschema)):
                continue
            evaluated_keys += _safe_find_evaluated_property_keys(
                validator, instance, subschema
            )

    if "if" in schema:
        if validator.evolve(schema=schema["if"]).is_valid(instance):
            evaluated_keys += _safe_find_evaluated_property_keys(
                validator, instance, schema["if"]
            )
            if "then" in schema:
                evaluated_keys += _safe_find_evaluated_property_keys(
                    validator, instance, schema["then"]
                )
        elif "else" in schema:
            evaluated_keys += _safe_find_evaluated_property_keys(
                validator, instance, schema["else"]
            )

    return evaluated_keys


def _safe_unevaluated_properties(validator, unevaluated, instance, schema):
    if not validator.is_type(instance, "object"):
        return

    evaluated_keys = _safe_find_evaluated_property_keys(
        validator, instance, schema
    )

    unevaluated_keys = []
    for property_name in instance:
        if property_name in evaluated_keys:
            continue
        for _ in validator.descend(
            instance[property_name],
            unevaluated,
            path=property_name,
            schema_path=property_name,
        ):
            unevaluated_keys.append(property_name)

    if unevaluated_keys:
        if unevaluated is False:
            error = "Unevaluated properties are not allowed (%s %s unexpected)"
            yield ValidationError(
                error % _extras_msg(sorted(unevaluated_keys, key=str))
            )
        else:
            error = (
                "Unevaluated properties are not valid under "
                "the given schema (%s %s unevaluated and invalid)"
            )
            yield ValidationError(error % _extras_msg(unevaluated_keys))


def _lookup_recursive_ref(resolver):
    resolved = resolver.lookup("#")
    if isinstance(resolved.contents, Mapping) and resolved.contents.get(
        "$recursiveAnchor"
    ):
        for uri, _ in resolver.dynamic_scope():
            next_resolved = resolver.lookup(uri)
            if not isinstance(
                next_resolved.contents, Mapping
            ) or not next_resolved.contents.get("$recursiveAnchor"):
                break
            resolved = next_resolved
    return resolved


def _safe_find_evaluated_property_keys_draft2019(
    validator, instance, schema
):
    if validator.is_type(schema, "boolean"):
        return []

    evaluated_keys = []

    ref = schema.get("$ref")
    if ref is not None:
        resolved = validator._resolver.lookup(ref)
        evaluated_keys.extend(
            _safe_find_evaluated_property_keys_draft2019(
                validator.evolve(
                    schema=resolved.contents,
                    _resolver=resolved.resolver,
                ),
                instance,
                resolved.contents,
            )
        )

    if "$recursiveRef" in schema:
        resolved = _lookup_recursive_ref(validator._resolver)
        evaluated_keys.extend(
            _safe_find_evaluated_property_keys_draft2019(
                validator.evolve(
                    schema=resolved.contents,
                    _resolver=resolved.resolver,
                ),
                instance,
                resolved.contents,
            )
        )

    for keyword in [
        "properties",
        "additionalProperties",
        "unevaluatedProperties",
    ]:
        if keyword not in schema:
            continue
        schema_value = schema[keyword]
        if validator.is_type(schema_value, "boolean") and schema_value:
            evaluated_keys += instance.keys()
        elif validator.is_type(schema_value, "object"):
            for property_name in schema_value:
                if property_name in instance:
                    evaluated_keys.append(property_name)

    if "patternProperties" in schema:
        compiled = [
            _compile_safe_regex(pattern)
            for pattern in schema["patternProperties"]
        ]
        for property_name in instance:
            for pattern in compiled:
                if pattern.search(property_name):
                    evaluated_keys.append(property_name)

    if "dependentSchemas" in schema:
        for property_name, subschema in schema["dependentSchemas"].items():
            if property_name not in instance:
                continue
            evaluated_keys += _safe_find_evaluated_property_keys_draft2019(
                validator, instance, subschema
            )

    for keyword in ["allOf", "oneOf", "anyOf"]:
        for subschema in schema.get(keyword, []):
            if next(validator.descend(instance, subschema), None) is not None:
                continue
            evaluated_keys += _safe_find_evaluated_property_keys_draft2019(
                validator, instance, subschema
            )

    if "if" in schema:
        if validator.evolve(schema=schema["if"]).is_valid(instance):
            evaluated_keys += _safe_find_evaluated_property_keys_draft2019(
                validator, instance, schema["if"]
            )
            if "then" in schema:
                evaluated_keys += (
                    _safe_find_evaluated_property_keys_draft2019(
                        validator, instance, schema["then"]
                    )
                )
        elif "else" in schema:
            evaluated_keys += _safe_find_evaluated_property_keys_draft2019(
                validator, instance, schema["else"]
            )

    return evaluated_keys


def _safe_unevaluated_properties_draft2019(
    validator, unevaluated, instance, schema
):
    if not validator.is_type(instance, "object"):
        return

    evaluated_keys = _safe_find_evaluated_property_keys_draft2019(
        validator, instance, schema
    )

    unevaluated_keys = []
    for property_name in instance:
        if property_name in evaluated_keys:
            continue
        for _ in validator.descend(
            instance[property_name],
            unevaluated,
            path=property_name,
            schema_path=property_name,
        ):
            unevaluated_keys.append(property_name)

    if unevaluated_keys:
        if unevaluated is False:
            error = "Unevaluated properties are not allowed (%s %s unexpected)"
            yield ValidationError(
                error % _extras_msg(sorted(unevaluated_keys, key=str))
            )
        else:
            error = (
                "Unevaluated properties are not valid under "
                "the given schema (%s %s unevaluated and invalid)"
            )
            yield ValidationError(error % _extras_msg(unevaluated_keys))


def _safe_validator_class(base_validator: type) -> type:
    safe = _SAFE_VALIDATOR_CLASSES.get(base_validator)
    if safe is not None:
        return safe

    overrides = {
        "pattern": _safe_pattern,
        "patternProperties": _safe_pattern_properties,
        "additionalProperties": _safe_additional_properties,
    }
    if base_validator is Draft201909Validator:
        overrides["unevaluatedProperties"] = (
            _safe_unevaluated_properties_draft2019
        )
    elif base_validator is Draft202012Validator:
        overrides["unevaluatedProperties"] = _safe_unevaluated_properties

    safe = validators.extend(
        base_validator,
        validators=overrides,
    )
    _SAFE_VALIDATOR_CLASSES[base_validator] = safe
    _SAFE_VALIDATOR_CLASSES[safe] = safe
    return safe


def _install_safe_jsonschema_validators() -> None:
    # jsonschema may switch dialect while descending into an embedded resource
    # that declares its own $schema. Register the RE2-backed variants under the
    # standard metaschema identifiers so those transitions remain safe too.
    supported = (
        Draft3Validator,
        Draft4Validator,
        Draft6Validator,
        Draft7Validator,
        Draft201909Validator,
        Draft202012Validator,
    )
    for base_validator in supported:
        safe = _safe_validator_class(base_validator)
        validators.validates(f"{base_validator.__name__}-re2")(safe)


_install_safe_jsonschema_validators()


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
    try:
        errors = sorted(
            validator.iter_errors(arguments),
            key=lambda error: (
                tuple(str(part) for part in error.absolute_path),
                str(error.validator),
                error.message,
            ),
        )
    except _UnsupportedSafeRegex as exc:
        return (
            "$: JSON-Schema-Regex wird von der sicheren RE2-Engine "
            f"nicht unterstützt: {exc}"
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
        if any(0xD800 <= ord(character) <= 0xDFFF for character in tool_name):
            raise RuntimeError(
                f"MCP-Server {server_name!r} liefert einen Toolnamen mit "
                "einem ungültigen Unicode-Surrogate."
            )
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
