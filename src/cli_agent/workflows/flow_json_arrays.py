"""Schema-independent, lossless JSON array fragmentation for Flow batching.

A configured JSON Pointer selects an array inside each input object. Arrays
may contain any JSON values. Non-selected fields are carried into every
fragment verbatim; selected array entries appear exactly once, in order.

No model, domain knowledge, or source text access is involved.
"""
from __future__ import annotations

from copy import deepcopy
from typing import Any, Callable

from cli_agent.workflows.flow_json_operations import (
    MAX_BATCHES, MAX_RESULT_BYTES, _serialized_size,
)

# Fragment allocation is bounded independently of the number of packed
# batches. A batch may contain multiple fragments.
MAX_ARRAY_FRAGMENTS = 50_000


def parse_array_paths(paths: Any) -> tuple[tuple[str, ...], ...]:
    """Validate non-overlapping RFC 6901 JSON Pointers to object array fields.

    Pointers traverse JSON objects only, not array indices. Nested object
    fields are supported; arrays themselves are atomic entry boundaries.
    """
    if not isinstance(paths, (list, tuple)) or not paths or len(paths) > 100:
        raise ValueError("batch_arrays.array_paths requires 1..100 JSON Pointers.")
    decoded: list[tuple[str, ...]] = []
    for raw in paths:
        if not isinstance(raw, str) or not raw.startswith("/"):
            raise ValueError("batch_arrays.array_paths entries must start with '/'.")
        parts = raw[1:].split("/")
        if any("~" in part.replace("~0", "").replace("~1", "") for part in parts):
            raise ValueError("batch_arrays.array_paths contains an invalid '~' escape.")
        path = tuple(part.replace("~1", "/").replace("~0", "~") for part in parts)
        if path in decoded:
            raise ValueError("batch_arrays.array_paths contains duplicate pointers.")
        if any(
            path[:len(other)] == other or other[:len(path)] == path
            for other in decoded
        ):
            raise ValueError("batch_arrays.array_paths must not overlap.")
        decoded.append(path)
    return tuple(decoded)


def _find_array(root: dict[str, Any], path: tuple[str, ...], index: int) -> list[Any]:
    node: Any = root
    for part in path:
        if not isinstance(node, dict) or part not in node:
            raise ValueError(
                f"batch_arrays: entry {index} has no object array at "
                f"/{'/'.join(path)}."
            )
        node = node[part]
    if not isinstance(node, list):
        raise ValueError(
            f"batch_arrays: entry {index} expects an array at /{'/'.join(path)}."
        )
    return node


def _set_array(root: dict[str, Any], path: tuple[str, ...], value: list[Any]) -> None:
    node: Any = root
    for part in path[:-1]:
        node = node[part]
    node[path[-1]] = value


def split_json_arrays(
    records: list[Any],
    *,
    array_paths: tuple[str, ...] | list[str],
    max_batch_bytes: int,
    target_fragment_bytes: int,
    serializer: Callable[[Any], str] | None = None,
) -> list[dict[str, Any]]:
    """Fragment selected arrays at complete element boundaries.

    If an individual element fits the hard batch limit but not the target,
    a larger single-element fragment is emitted. A too-large element or
    non-selected metadata causes a clear error; nothing is discarded.
    """
    paths = parse_array_paths(array_paths)

    def size(value: Any) -> int:
        return _serialized_size(value, serializer=serializer)

    # Reserve the outer {"id":"b1000","items":[...]} envelope and one
    # comma for safety. Use the exact Flow UTF-8 serializer.
    envelope = size({"id": f"b{MAX_BATCHES:04d}", "items": []})
    hard_limit = max_batch_bytes - envelope - 1
    if hard_limit < 1:
        raise ValueError("batch_arrays: max_batch_bytes is too small.")

    fragments: list[dict[str, Any]] = []
    result_bytes = 0

    def emit(fragment: dict[str, Any], record_index: int) -> None:
        nonlocal result_bytes
        bytes_used = size(fragment)
        if bytes_used > hard_limit:
            raise ValueError(
                f"batch_arrays: entry {record_index} exceeds max_batch_bytes."
            )
        if len(fragments) >= MAX_ARRAY_FRAGMENTS:
            raise ValueError("batch_arrays exceeds the fragment count limit.")
        result_bytes += bytes_used + (1 if fragments else 0)
        if result_bytes > MAX_RESULT_BYTES:
            raise ValueError("batch_arrays exceeds the JSON result limit.")
        fragments.append(fragment)

    for record_index, record in enumerate(records):
        if not isinstance(record, dict):
            raise ValueError(
                f"batch_arrays: entry {record_index} must be a JSON object."
            )
        sources = [_find_array(record, path, record_index) for path in paths]
        # Deep copy only for the skeleton so original inputs are never
        # mutated, including shared/nested arrays.
        skeleton = deepcopy(record)
        for path in paths:
            _set_array(skeleton, path, [])

        if size(skeleton) > hard_limit:
            raise ValueError(
                f"batch_arrays: non-selected fields in entry {record_index} "
                "exceed max_batch_bytes."
            )

        current = deepcopy(skeleton)
        has_values = False
        for path, entries in zip(paths, sources):
            for item_index, entry in enumerate(entries):
                destination = _find_array(current, path, record_index)
                destination.append(entry)
                used = size(current)
                # The soft target can exceed the envelope-adjusted hard
                # limit (e.g. max_batch_bytes=1024, target=1024). Flush a
                # nonempty fragment on EITHER bound before deciding whether
                # the new element is indivisible.
                if has_values and (
                    used > target_fragment_bytes or used > hard_limit
                ):
                    destination.pop()
                    emit(current, record_index)
                    current = deepcopy(skeleton)
                    has_values = False
                    destination = _find_array(current, path, record_index)
                    destination.append(entry)
                    used = size(current)
                if used > hard_limit:
                    raise ValueError(
                        f"batch_arrays: indivisible array entry {item_index} "
                        f"at /{'/'.join(path)} of input {record_index} "
                        "exceeds max_batch_bytes."
                    )
                has_values = True
        # Keep metadata-only records (all selected arrays empty).
        emit(current, record_index)

    return fragments
