"""Schema-neutral JSON reconciliation primitives for declarative Flow steps.

No knowledge of documents, OKF, themes, or application-specific fields.
Every record and reference is validated as untrusted JSON.
"""
from __future__ import annotations

from copy import deepcopy
from decimal import Decimal, InvalidOperation
import re
from typing import Any

from cli_agent.workflows.flow_json_operations import _JsonNumber

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
    if type(v) is not str or not v.strip():
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
        if type(item) is not str or not item.strip():
            raise ValueError(f"{name} has an invalid ID.")
        found.append(item)
    return found


def _unique(items: list[str], name: str) -> set[str]:
    result = set(items)
    if len(result) != len(items):
        raise ValueError(f"{name} contains duplicate IDs.")
    return result


def _json_distinct_key(value: Any) -> tuple[Any, ...]:
    """Hashable, type-preserving representation of a JSON value.

    Strings and booleans are never confused with JSON numbers. Integer and
    non-integer number literals compare by numeric value (1 == 1.0), just
    as JSON has a single number type. Nested arrays retain order; object
    property order is irrelevant. The runner's lossless _JsonNumber must
    be handled BEFORE str, which it subclasses.
    """
    if value is None:
        return ("null",)
    if isinstance(value, bool):
        return ("boolean", value)
    if isinstance(value, (int, float, _JsonNumber)):
        try:
            number = Decimal(str(value))
        except InvalidOperation as exc:
            raise ValueError("Invalid JSON number in distinct-value selector.") from exc
        if not number.is_finite():
            raise ValueError("Nonfinite JSON number in distinct-value selector.")
        return ("number", number)
    if isinstance(value, str):
        return ("string", value)
    if isinstance(value, list):
        return ("array", tuple(_json_distinct_key(entry) for entry in value))
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ValueError("JSON object keys must be strings.")
        return (
            "object",
            tuple(sorted(
                (key, _json_distinct_key(entry))
                for key, entry in value.items()
            )),
        )
    raise ValueError(f"Unsupported JSON distinct-value type: {type(value).__name__}")


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
    source_records_path: str | None = None,
    selected_path: str, references_path: str, excluded_path: str,
    source_members_path: str, result_members_path: str,
    source_status_path: str | None = "/status",
    source_warnings_path: str | None = "/warnings",
    result_status_path: str = "/status",
    result_warnings_path: str = "/warnings",
    result_id_path: str | None = None,
    result_id_prefix: str = "",
    result_id_pattern: str | None = None,
    member_fields: list[str] | tuple[str, ...] = ("/path",),
    member_patterns: dict[str, str] | None = None,
    result_path_field: str | None = None,
    result_path_template: str | None = None,
) -> dict[str, Any]:
    """Validate an exhaustive, disjoint partition and exact member unions.

    Eligibility is expressed as a generic minimum number of distinct selected
    JSON values. Wildcard indices can be referenced as {0}, {1}, ...

    Source status/warnings are inspected on source_records_path. By default
    this is the parent record collection containing the selected source
    groups, derived from source_path (e.g. /records/*/groups/* ->
    /records/*). Override it for differently nested JSON schemas.
    """
    if type(min_distinct) is not int or min_distinct < 1:
        raise ValueError("min_distinct must be positive.")
    if not isinstance(reference_template, str) or not reference_template:
        raise ValueError("reference_template must be nonempty.")
    if (result_path_field is None) != (result_path_template is None):
        raise ValueError("Both result path options are required together.")
    if result_id_path is None and (
        result_id_pattern is not None
        or result_path_field is not None
        or result_id_prefix
    ):
        raise ValueError(
            "result_id_path is required for ID patterns, ID prefixes and output path checks."
        )
    if source_records_path is not None:
        # RFC 6901 includes the empty pointer, which selects the JSON root.
        _tokens(source_records_path)
    status_tokens = _tokens(result_status_path)
    if len(status_tokens) != 1 or status_tokens[0] == "*":
        raise ValueError("Result status path must name a top-level field.")
    if result_id_pattern is not None and (not isinstance(result_id_pattern, str) or len(result_id_pattern) > 256):
        raise ValueError("Invalid result ID pattern.")
    if not isinstance(member_patterns, dict) and member_patterns is not None:
        raise ValueError("member_patterns must be a mapping.")
    validated_patterns: dict[str, re.Pattern[str]] = {}
    for pointer, pattern in (member_patterns or {}).items():
        if pointer not in member_fields or not isinstance(pattern, str) or len(pattern) > 256:
            raise ValueError("Invalid member pattern configuration.")
        try:
            validated_patterns[pointer] = re.compile(pattern)
        except re.error as exc:
            raise ValueError("Invalid member pattern.") from exc
    src: dict[str, tuple[bool, set[tuple[str, ...]]]] = {}
    incomplete = False

    def members(node: Any, selector: str) -> set[tuple[str, ...]]:
        items = _values(node, selector)
        result_set: set[tuple[str, ...]] = set()
        for value, _ in items:
            entry = tuple(_field(value, ptr, name="member field") for ptr in member_fields)
            for ptr, field_value in zip(member_fields, entry):
                pattern = validated_patterns.get(ptr)
                if pattern is not None and pattern.fullmatch(field_value) is None:
                    raise ValueError("Member field does not match configured pattern.")
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
        # Eligibility accepts any JSON value, not merely string reference IDs.
        # Canonical typed keys distinguish booleans/strings from numbers and
        # support unhashable arrays and objects without relying on repr().
        values = [_json_distinct_key(value) for value, _ in _values(raw, distinct_path)]
        src[ref] = (len(set(values)) >= min_distinct, members(raw, source_members_path))
    if not src:
        raise ValueError("No source groups to validate.")
    if source_status_path is not None or source_warnings_path is not None:
        if source_records_path is None:
            if not source_path.endswith("/*"):
                raise ValueError(
                    "source_records_path is required when source_path does not "
                    "select a complete array of groups."
                )
            # Remove the final /<group-array-name>/* pair. This works for
            # arbitrary outer field names and nested record collections.
            source_records_path = source_path.rsplit("/", 2)[0]
        records = _values(source, source_records_path)
        if not records:
            raise ValueError("No source status records selected.")
        for item, _ in records:
            record = _object(item, "source status record")
            if source_status_path is not None:
                if _value(record, source_status_path) != "ok":
                    incomplete = True
            if source_warnings_path is not None and _value(record, source_warnings_path):
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
            if (not target_id.startswith(result_id_prefix) or target_id in target_ids
                    or (result_id_pattern is not None and re.fullmatch(result_id_pattern, target_id) is None)):
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
    validated[status_tokens[0]] = (
        "needs_attention" if incomplete or status != "ok" or warnings else "ok"
    )
    validated["validated_group_count"] = len(src)
    validated["validated_eligible_count"] = sum(eligible for eligible, _ in src.values())
    return validated


def validate_allowed_values(
    *,
    allowed: Any,
    candidate: Any,
    allowed_path: str,
    values_path: str,
    null_policy: str = "forbid",
    on_invalid: str = "report",
) -> dict[str, Any]:
    """Compare JSON-selected values against an explicitly selected allowed set.

    All paths are RFC 6901 selectors with optional "*" array elements.
    A missing candidate field is reported as a correction finding rather than
    accidentally accepted. All other JSON primitive/structured types compare
    by canonical typed value. The complete original candidate is retained for
    downstream conditional repair or use via a Flow result_fallback.
    """
    if null_policy not in {"forbid", "allow", "when_allowed_empty"}:
        raise ValueError("null_policy must be forbid, allow or when_allowed_empty.")
    if on_invalid not in {"report", "raise"}:
        raise ValueError("on_invalid must be report or raise.")
    for pointer in (allowed_path, values_path):
        if "*" not in _tokens(pointer):
            raise ValueError("Both JSON selectors must include an array wildcard.")

    # The allowed side is authoritative. Invalid/missing allowed selectors
    # must fail closed; an explicitly empty array, however, is valid.
    allowed_entries = [value for value, _ in _values(allowed, allowed_path)]
    allowed_keys = {_json_distinct_key(value) for value in allowed_entries}
    permitted_null = (
        null_policy == "allow"
        or (null_policy == "when_allowed_empty" and not allowed_entries)
    )
    entries: list[tuple[str, Any]] = []
    findings: list[dict[str, Any]] = []

    def esc(part: str) -> str:
        return part.replace("~", "~0").replace("/", "~1")

    # Walk candidate selectors without assuming every LLM-generated object
    # contains the final field. Record its exact location for the repair LLM.
    nodes = [(candidate, "")]
    for token in _tokens(values_path):
        next_nodes: list[tuple[Any, str]] = []
        for node, pointer in nodes:
            if token == "*":
                if not isinstance(node, list):
                    findings.append({
                        "path": pointer or "", "reason": "expected_array",
                        "value": deepcopy(node),
                    })
                    continue
                next_nodes.extend(
                    (entry, f"{pointer}/{index}")
                    for index, entry in enumerate(node)
                )
            elif isinstance(node, dict):
                path = f"{pointer}/{esc(token)}"
                if token not in node:
                    findings.append({
                        "path": path, "reason": "missing_field",
                    })
                else:
                    next_nodes.append((node[token], path))
            elif isinstance(node, list) and token.isdecimal():
                i = int(token)
                path = f"{pointer}/{i}"
                if i >= len(node):
                    findings.append({"path": path, "reason": "missing_index"})
                else:
                    next_nodes.append((node[i], path))
            else:
                findings.append({
                    "path": pointer or "", "reason": "invalid_container",
                    "value": deepcopy(node),
                })
            if len(next_nodes) + len(findings) > _MAX_SELECTED:
                raise ValueError("JSON validation selection exceeds maximum items.")
        nodes = next_nodes
    entries = nodes
    if not entries and not findings:
        findings.append({"path": values_path, "reason": "no_values"})

    for pointer, value in entries:
        key = _json_distinct_key(value)
        if key in allowed_keys or (value is None and permitted_null):
            continue
        findings.append({
            "path": pointer,
            "reason": "null_not_allowed" if value is None else "not_allowed",
            "value": deepcopy(value),
        })
        if len(findings) > 1000:
            raise ValueError("JSON membership validation exceeds 1000 findings.")

    if on_invalid == "raise" and findings:
        details = ", ".join(
            f"{finding['path'] or '/'} ({finding['reason']})"
            for finding in findings[:10]
        )
        raise ValueError(
            f"{len(findings)} JSON value(s) outside allowed set: {details}"
        )

    return {
        "status": "needs_attention" if findings else "ok",
        "errors": findings,
        "checked_count": len(entries),
        "allowed_values": deepcopy(allowed_entries),
        "value": deepcopy(candidate),
    }
