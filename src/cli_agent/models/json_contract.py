"""Bounded JSON Schema contracts for model-generated data.

Only local JSON references are permitted. A user-supplied schema must never
cause a network request or access unrelated files during validation.
"""
from __future__ import annotations

import json
from urllib.parse import unquote
from decimal import Decimal
from typing import Any

from jsonschema import Draft202012Validator, validators
from referencing import Registry
from referencing.exceptions import NoSuchResource


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
    # All schema-valued positions, including local-reference targets, are
    # inspected; const/enum values and property names are ordinary data.
    seen_pointers: set[str] = set()
    visited: set[int] = set()

    def walk(value: Any) -> None:
        if not isinstance(value, dict) or id(value) in visited:
            return
        visited.add(id(value))
        check_refs(value)
        ref = value.get("$ref")
        if isinstance(ref, str) and ref not in seen_pointers:
            seen_pointers.add(ref)
            target: Any = schema
            for token in (unquote(ref[2:]).split("/") if ref != "#" else []):
                token = token.replace("~1", "/").replace("~0", "~")
                try:
                    target = target[int(token)] if isinstance(target, list) else target[token]
                except (KeyError, IndexError, ValueError, TypeError) as exc:
                    raise ValueError(f"Unresolvable local JSON Schema reference: {ref}") from exc
            # A local pointer may refer to an otherwise unknown keyword.
            # The root metaschema validation will not inspect that target,
            # so validate it explicitly before allowing model execution.
            try:
                Draft202012Validator.check_schema(target)
            except Exception as exc:
                raise ValueError(
                    f"Invalid JSON Schema reference target {ref!r}: {exc}"
                ) from exc
            walk(target)
        for keyword in ("$defs", "definitions", "properties", "patternProperties", "dependentSchemas"):
            children = value.get(keyword)
            if isinstance(children, dict):
                for child in children.values():
                    walk(child)
        for keyword in ("additionalProperties", "unevaluatedProperties", "propertyNames",
                        "items", "additionalItems", "unevaluatedItems", "contains",
                        "not", "if", "then", "else", "contentSchema"):
            walk(value.get(keyword))
        for keyword in ("allOf", "anyOf", "oneOf", "prefixItems"):
            children = value.get(keyword)
            if isinstance(children, list):
                for child in children:
                    walk(child)

    walk(schema)
    try:
        Draft202012Validator.check_schema(schema)
    except Exception as exc:
        raise ValueError(f"Invalid JSON Schema: {exc}") from exc
    return schema


def json_contract_error(answer: str, schema: dict[str, Any]) -> str | None:
    """Return the first actionable schema error, or None if the JSON matches."""
    value = json.loads(answer, parse_float=Decimal)

    def decimal_numbers(node: Any) -> Any:
        # Decimal preserves exact decimal multipleOf arithmetic. Keep the
        # original JSON-compatible schema unchanged for model prompts.
        if isinstance(node, dict):
            return {key: decimal_numbers(child) for key, child in node.items()}
        if isinstance(node, list):
            return [decimal_numbers(child) for child in node]
        if type(node) is float:
            return Decimal(str(node))
        return node

    def deny_remote_retrieval(uri: str) -> Any:
        raise NoSuchResource(ref=uri)

    def is_integer(checker: Any, value: Any) -> bool:
        if isinstance(value, bool):
            return False
        if isinstance(value, Decimal):
            return value.is_finite() and value == value.to_integral_value()
        return Draft202012Validator.TYPE_CHECKER.is_type(value, "integer")

    decimal_validator = validators.extend(
        Draft202012Validator,
        type_checker=Draft202012Validator.TYPE_CHECKER.redefine(
            "integer", is_integer
        ),
    )
    validator = decimal_validator(
        decimal_numbers(schema),
        registry=Registry(retrieve=deny_remote_retrieval),
        format_checker=Draft202012Validator.FORMAT_CHECKER,
    )
    issue = next(validator.iter_errors(value), None)
    if issue is None:
        return None
    return f"{issue.json_path}: {issue.message}"[:600]
