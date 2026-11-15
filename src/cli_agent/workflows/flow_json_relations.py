"""Schema-neutral JSON reconciliation primitives for declarative Flow steps.

No knowledge of documents, OKF, themes, or application-specific fields.
Every record and reference is validated as untrusted JSON.
"""
from __future__ import annotations

from copy import deepcopy
import re
from typing import Any

_MAX_SELECTED = 1_000_000
_MAX_PATH_COMPONENTS = 32


def _tokens(pointer: str) -> tuple[str, ...]:
    if not isinstance(pointer, str) or (pointer and not pointer.startswith("/")):
        raise ValueError("JSON selector must be an RFC 6901 pointer.")
    if pointer == "":
        return ()
    parts = pointer[1:].split("/")
    if len(parts) > _MAX_PATH_COMPONENTS:
        raise ValueError("JSON selector is too deep.")
    for part in parts:
        if "~" in part.replace("~0", "").replace("~1", ""):
            raise ValueError("Invalid JSON pointer escape.")
    return tuple(p.replace("~1", "/").replace("~0", "~") for p in parts)


def _values(value: Any, path: str) -> list[tuple[Any, tuple[int, ...]]]:
    """Return selected values and all wildcard positions (1-based)."""
    parts = _tokens(path)
    current = [(value, ())]
    for part in parts:
        next_values = []
        for node, indices in current:
            if part == "*":
                if not isinstance(node, list):
                    raise ValueError(f"Wildcard requires JSON array: {path}")
                next_values.extend((item, (*indices, i)) for i, item in enumerate(node, 1))
            elif isinstance(node, dict) and part in node:
                next_values.append((node[part], indices))
            elif isinstance(node, list) and part.isdecimal() and int(part) < len(node):
                next_values.append((node[int(part)], indices))
            else:
                raise ValueError(f"JSON selector does not resolve: {path}")
            if len(next_values) > _MAX_SELECTED:
                raise ValueError("JSON selection exceeds maximum items.")
        current = next_values
    return current


def _value(value: Any, path: str) -> Any:
    matches = _values(value, path)
    if len(matches) != 1:
        raise ValueError(f"Expected exactly one value at {path!r}.")
    return matches[0][0]


def _field(record: Any, path: str, *, name: str) -> str:
    v = _value(record, path)
    if not isinstance(v, str) or not v.strip():
        raise ValueError(f"{name} must be a nonempty JSON string.")
    return v


def _record_list(value: Any, name: str) -> list[Any]:
    if not isinstance(value, list):
        raise ValueError(f"{name} must be a JSON array.")
    return value


def _object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError(f"{name} must be a JSON object.")
    return value


def _id_list(value: Any, path: str, name: str) -> list[str]:
    found: list[str] = []
    for item, _ in _values(value, path):
        if not isinstance(item, str) or not item.strip():
            raise ValueError(f"{name} has an invalid ID.")
        found.append(item)
    return found


def _unique(items: list[str], name: str) -> set[str]:
    result = set(items)
    if len(result) != len(items):
        raise ValueError(f"{name} contains duplicate IDs.")
    return result


def collect_arrays(
    records: Any, *, array_path: str,
    shared_paths: list[str] | tuple[str, ...] = (),
    unique_path: str | None = None,
    required_status: str | None = None,
    status_path: str = "/status",
    warnings_path: str | None = "/warnings",
) -> dict[str, Any]:
    """Append complete entries from same-shape JSON objects, retaining metadata."""
    rows = _record_list(records, "records")
    if not rows:
        raise ValueError("Cannot collect an empty record list.")
    first = _object(rows[0], "records[0]")
    _record_list(_value(first, array_path), "selected field")
    for p in shared_paths:
        _value(first, p)
    combined = deepcopy(first)
    target = _value(combined, array_path)
    target.clear()
    seen: set[str] = set()
    for index, raw in enumerate(rows):
        row = _object(raw, f"records[{index}]")
        if required_status is not None and _value(row, status_path) != required_status:
            raise ValueError(f"Record {index} has unsuccessful status.")
        if warnings_path is not None and _value(row, warnings_path):
            raise ValueError(f"Record {index} contains unresolved warnings.")
        for p in shared_paths:
            if _value(row, p) != _value(first, p):
                raise ValueError(f"Record {index} has conflicting metadata at {p}.")
        for entry in _record_list(_value(row, array_path), "selected field"):
            if unique_path is not None:
                key = _field(entry, unique_path, name="unique key")
                if key in seen:
                    raise ValueError(f"Duplicate record ID: {key}")
                seen.add(key)
            target.append(deepcopy(entry))
            if len(target) > _MAX_SELECTED:
                raise ValueError("Collected array exceeds maximum size.")
    return combined


def project_fields(record: Any, *, fields: dict[str, str]) -> dict[str, Any]:
    """Select and rename top-level output fields by exact JSON pointers."""
    if not isinstance(fields, dict) or not fields:
        raise ValueError("fields must be a nonempty field mapping.")
    result = {}
    for key, pointer in fields.items():
        if not isinstance(key, str) or not key or "/" in key:
            raise ValueError("Invalid output field name.")
        result[key] = deepcopy(_value(record, pointer))
    return result


def validate_ids(
    *, expected: Any, actual: Any,
    expected_path: str, actual_path: str,
    expected_status_path: str | None = None,
    required_status: str = "ok",
    expected_warnings_path: str | None = None,
) -> dict[str, Any]:
    """Require identical multisets of IDs, with no duplicates on either side."""
    if expected_status_path is not None:
        # Validate every object in a list, or a single object.
        records = expected if isinstance(expected, list) else [expected]
        for i, record in enumerate(records):
            if _value(record, expected_status_path) != required_status:
                raise ValueError(f"Expected record {i} has incomplete status.")
            if expected_warnings_path is not None and _value(record, expected_warnings_path):
                raise ValueError(f"Expected record {i} has unresolved warnings.")
    left = _id_list(expected, expected_path, "expected")
    right = _id_list(actual, actual_path, "actual")
    lset = _unique(left, "Expected IDs")
    rset = _unique(right, "Actual IDs")
    if lset != rset:
        missing = sorted(lset - rset)[:10]
        extra = sorted(rset - lset)[:10]
        raise ValueError(f"ID coverage mismatch, missing={missing}, unexpected={extra}.")
    return {"status": "ok", "count": len(left)}


def join_by_alias(
    *, items: Any, mapping: Any,
    items_path: str, item_id_path: str, item_key_path: str,
    mapping_path: str, mapping_id_path: str, mapping_alias_path: str,
    id_prefix: str = "", require_all_aliases: bool = True,
    source_field: str = "item_id", target_field: str = "topic_id",
    output_field: str = "assignments", wrap_batches: bool = True,
) -> Any:
    """Resolve each source key via an exhaustive unique alias-to-target map."""
    for field in (source_field, target_field, output_field):
        if not isinstance(field, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_-]*", field):
            raise ValueError("Output field name is invalid.")
    if source_field == target_field:
        raise ValueError("Output source and target fields must differ.")
    if not isinstance(id_prefix, str) or len(id_prefix) > 256:
        raise ValueError("Invalid ID prefix.")
    alias_map: dict[str, str] = {}
    for raw, _ in _values(mapping, mapping_path):
        target = _field(raw, mapping_id_path, name="target ID")
        aliases = _id_list(raw, mapping_alias_path, "aliases")
        if not aliases:
            raise ValueError("Mapping entry has no aliases.")
        for alias in aliases:
            if alias in alias_map:
                raise ValueError(f"Alias appears more than once: {alias}")
            alias_map[alias] = target
    if not alias_map:
        raise ValueError("Mapping has no aliases.")
    seen_ids: set[str] = set()
    used_aliases: set[str] = set()
    results = []
    for raw, _ in _values(items, items_path):
        key = _field(raw, item_key_path, name="source join key")
        identifier = id_prefix + _field(raw, item_id_path, name="source ID")
        if identifier in seen_ids:
            raise ValueError(f"Duplicate source ID: {identifier}")
        seen_ids.add(identifier)
        if key not in alias_map:
            raise ValueError(f"No mapping for source key: {key}")
        used_aliases.add(key)
        results.append({source_field: identifier, target_field: alias_map[key]})
        if len(results) > _MAX_SELECTED:
            raise ValueError("Too many join results.")
    if not results:
        raise ValueError("Join input contains no items.")
    if require_all_aliases and used_aliases != set(alias_map):
        raise ValueError("Unused mapping aliases: " + ", ".join(sorted(set(alias_map) - used_aliases)[:10]))
    record = {output_field: results}
    return [record] if wrap_batches else record


def validate_partition(
    *, source: Any, result: Any,
    source_path: str, reference_template: str,
    distinct_path: str, min_distinct: int,
    selected_path: str, references_path: str, excluded_path: str,
    source_members_path: str, result_members_path: str,
    source_status_path: str | None = "/status",
    source_warnings_path: str | None = "/warnings",
    result_status_path: str = "/status",
    result_warnings_path: str = "/warnings",
    result_id_path: str | None = None,
    result_id_prefix: str = "",
    member_fields: list[str] | tuple[str, ...] = ("/path",),
    result_path_field: str | None = None,
    result_path_template: str | None = None,
) -> dict[str, Any]:
    """Validate an exhaustive, disjoint partition and exact member unions.

    Eligibility is expressed as a generic minimum number of distinct selected
    JSON values. Wildcard indices can be referenced as {0}, {1}, ...
    """
    if type(min_distinct) is not int or min_distinct < 1:
        raise ValueError("min_distinct must be positive.")
    if not isinstance(reference_template, str) or not reference_template:
        raise ValueError("reference_template must be nonempty.")
    if (result_path_field is None) != (result_path_template is None):
        raise ValueError("Both result path options are required together.")
    src: dict[str, tuple[bool, set[tuple[str, ...]]]] = {}
    incomplete = False

    def members(node: Any, selector: str) -> set[tuple[str, ...]]:
        items = _values(node, selector)
        result_set: set[tuple[str, ...]] = set()
        for value, _ in items:
            entry = tuple(_field(value, ptr, name="member field") for ptr in member_fields)
            if entry in result_set:
                raise ValueError("Duplicate member in a group.")
            result_set.add(entry)
        if not result_set:
            raise ValueError("Group must have at least one member.")
        return result_set

    for raw, indices in _values(source, source_path):
        if not isinstance(raw, dict):
            raise ValueError("Source group must be an object.")
        try:
            ref = reference_template.format(*indices)
        except (IndexError, ValueError, KeyError, AttributeError) as exc:
            raise ValueError("Invalid source reference template.") from exc
        if not ref or len(ref) > 128 or ref in src:
            raise ValueError("Duplicate/invalid source group reference.")
        # Distinct-value predicate is structural, independent of domain.
        values = _id_list(raw, distinct_path, "distinct values")
        src[ref] = (len(set(values)) >= min_distinct, members(raw, source_members_path))
        if source_status_path is not None:
            # Optionally check status on the source group's parent fragments.
            # For nested input lists this is handled by checking all objects
            # which contain a status field, without presuming its property name.
            pass
    if not src:
        raise ValueError("No source groups to validate.")
    for item in (source.get("items", []) if isinstance(source, dict) else []):
        if source_status_path is not None:
            status = _value(item, source_status_path)
            if status != "ok":
                incomplete = True
        if source_warnings_path is not None and _value(item, source_warnings_path):
            incomplete = True

    output = _object(result, "result")
    status = _value(output, result_status_path)
    if status not in {"ok", "needs_attention"}:
        raise ValueError("Unexpected result status.")
    if incomplete and status == "ok":
        raise ValueError("Output must preserve incomplete input status.")
    all_seen: set[str] = set()
    target_ids: set[str] = set()
    selections = _values(output, selected_path)
    for target, _ in selections:
        if result_id_path is not None:
            target_id = _field(target, result_id_path, name="target ID")
            if not target_id.startswith(result_id_prefix) or target_id in target_ids:
                raise ValueError("Invalid or duplicate output ID.")
            target_ids.add(target_id)
            if result_path_field is not None:
                expected_path = result_path_template.replace("{id}", target_id)
                if _value(target, result_path_field) != expected_path:
                    raise ValueError("Output path does not match configured template.")
        refs = _id_list(target, references_path, "target references")
        if not refs:
            raise ValueError("Output target has no source references.")
        required_members: set[tuple[str, ...]] = set()
        for ref in refs:
            if ref not in src or ref in all_seen or not src[ref][0]:
                raise ValueError(f"Unknown, duplicate or ineligible source reference: {ref}")
            all_seen.add(ref)
            required_members.update(src[ref][1])
        if members(target, result_members_path) != required_members:
            raise ValueError("Output group members do not equal referenced member union.")
    for ref in _id_list(output, excluded_path, "excluded IDs"):
        if ref not in src or ref in all_seen or src[ref][0]:
            raise ValueError(f"Invalid or eligible excluded source reference: {ref}")
        all_seen.add(ref)
    if all_seen != set(src):
        raise ValueError("Missing source group references: " + ", ".join(sorted(set(src) - all_seen)[:10]))
    warnings = _value(output, result_warnings_path)
    if not isinstance(warnings, list):
        raise ValueError("Result warnings must be a JSON array.")
    validated = deepcopy(output)
    validated[result_status_path.strip("/")] = (
        "needs_attention" if incomplete or status != "ok" or warnings else "ok"
    )
    validated["validated_group_count"] = len(src)
    validated["validated_eligible_count"] = sum(eligible for eligible, _ in src.values())
    return validated
