"""Bounded JSON Schema contracts for model-generated data.

Only local JSON references are permitted. A user-supplied schema must never
cause a network request or access unrelated files during validation.
"""
from __future__ import annotations

import json
from typing import Any

from jsonschema import Draft202012Validator


def check_json_contract(schema: Any) -> dict[str, Any]:
    """Validate a Draft 2020-12 schema without permitting external references."""
    if not isinstance(schema, dict):
        raise ValueError("JSON Schema root must be an object.")

    def check_refs(value: Any) -> None:
        """Walk schema positions, not arbitrary instance values or property names."""
        if not isinstance(value, dict):
            return
        for key in ("$id", "$dynamicRef", "$recursiveRef"):
            if key in value:
                raise ValueError(f"JSON Schema keyword {key!r} is unsupported.")
        if "$ref" in value:
            ref = value["$ref"]
            if not isinstance(ref, str) or (
                ref != "#" and not ref.startswith("#/")
            ):
                raise ValueError("JSON Schema permits only local $ref pointers.")

        # A mapping under these keywords contains subschemas as its values.
        for keyword in ("$defs", "definitions", "properties", "patternProperties",
                        "dependentSchemas"):
            children = value.get(keyword)
            if isinstance(children, dict):
                for child in children.values():
                    check_refs(child)

        # These keywords hold one nested schema.
        for keyword in ("additionalProperties", "unevaluatedProperties",
                        "propertyNames", "items", "additionalItems",
                        "unevaluatedItems", "contains", "not", "if", "then", "else",
                        "contentSchema"):
            check_refs(value.get(keyword))

        # These keywords hold lists of subschemas.
        for keyword in ("allOf", "anyOf", "oneOf", "prefixItems"):
            children = value.get(keyword)
            if isinstance(children, list):
                for child in children:
                    check_refs(child)

    check_refs(schema)
    try:
        Draft202012Validator.check_schema(schema)
    except Exception as exc:
        raise ValueError(f"Invalid JSON Schema: {exc}") from exc
    return schema


def json_contract_error(answer: str, schema: dict[str, Any]) -> str | None:
    """Return the first actionable schema error, or None if the JSON matches."""
    value = json.loads(answer)
    validator = Draft202012Validator(
        schema, format_checker=Draft202012Validator.FORMAT_CHECKER
    )
    issue = next(validator.iter_errors(value), None)
    if issue is None:
        return None
    return f"{issue.json_path}: {issue.message}"[:600]
