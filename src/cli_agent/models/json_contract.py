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
        if isinstance(value, dict):
            for key, child in value.items():
                if key in {"$id", "$dynamicRef", "$recursiveRef"}:
                    raise ValueError(f"JSON Schema keyword {key!r} is unsupported.")
                if key == "$ref" and (
                    not isinstance(child, str)
                    or (child != "#" and not child.startswith("#/"))
                ):
                    raise ValueError("JSON Schema permits only local $ref pointers.")
                check_refs(child)
        elif isinstance(value, list):
            for child in value:
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
