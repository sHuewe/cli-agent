"""Pure, bounded JSON transformations for Flow Runner steps."""
from __future__ import annotations

import json
from typing import Any, Callable

MAX_BATCHES = 1000
MAX_ROUTE_FRAGMENTS = 50_000
MAX_RESULT_BYTES = 100_000_000


def _serialized_size(
    value: Any, *, serializer: Callable[[Any], str] | None = None,
) -> int:
    # The Flow Runner preserves JSON number literals and emits raw UTF-8.
    # Measure with that exact serializer rather than json.dumps defaults.
    serialized = (
        serializer(value) if serializer is not None
        else json.dumps(
            value, ensure_ascii=False, allow_nan=False,
            separators=(",", ":"),
        )
    )
    return len(serialized.encode("utf-8"))



def _split_route_records(
    routes: list[Any],
    *,
    max_batch_bytes: int,
    serializer: Callable[[Any], str] | None,
) -> list[dict[str, Any]]:
    """Split large route inventories along complete JSON item boundaries.

    The original routes are not modified. Every candidate, knowledge item and
    warning is emitted exactly once, with original provenance metadata on
    every fragment. Each fragment must fit inside a one-item batch, including
    the batch envelope; even a single oversized item is rejected.
    """
    def size(value: Any) -> int:
        return _serialized_size(value, serializer=serializer)

    # Reserve space for the final batch envelope, not merely its items.
    fragment_limit = max_batch_bytes - size(
        {"id": f"b{MAX_BATCHES:04d}", "items": []}
    )
    parts = ("candidate_concepts", "knowledge_items", "warnings")
    fragments: list[dict[str, Any]] = []
    result_bytes = 0

    def emit(fragment: dict[str, Any], fragment_size: int) -> None:
        nonlocal result_bytes
        if len(fragments) >= MAX_ROUTE_FRAGMENTS:
            raise ValueError("batch_routes überschreitet das Fragment-Anzahllimit.")
        result_bytes += fragment_size + (1 if fragments else 0)
        if result_bytes > MAX_RESULT_BYTES:
            raise ValueError("batch_routes überschreitet das JSON-Ergebnislimit.")
        fragments.append(fragment)

    for route_index, raw in enumerate(routes):
        if not isinstance(raw, dict):
            raise ValueError(
                f"batch_routes erwartet Routing-Objekte; Eintrag {route_index} ist ungültig."
            )
        for field in ("document_id", "chunk_id", "folder_id"):
            if not isinstance(raw.get(field), str) or not raw[field]:
                raise ValueError(
                    f"batch_routes benötigt {field!r} in Route {route_index}."
                )
        for field in parts:
            if field in raw and not isinstance(raw[field], list):
                raise ValueError(
                    f"batch_routes erwartet eine Liste für {field!r} "
                    f"in Route {route_index}."
                )
        # Keep any additional route-level metadata (never discard unknown fields).
        metadata = {key: value for key, value in raw.items() if key not in parts}

        def empty_fragment() -> dict[str, Any]:
            return {**metadata, **{field: [] for field in parts}}

        fragment = empty_fragment()
        fragment_size = size(fragment)
        if fragment_size > fragment_limit:
            raise ValueError(
                f"Routing-Metadaten von Route {route_index} überschreiten "
                "max_batch_bytes."
            )
        has_entries = False
        for field in parts:
            for item_index, item in enumerate(raw.get(field, [])):
                item_size = size(item)
                increment = item_size + (1 if fragment[field] else 0)
                if fragment_size + increment > fragment_limit and has_entries:
                    emit(fragment, fragment_size)
                    fragment = empty_fragment()
                    fragment_size = size(fragment)
                    has_entries = False
                    increment = item_size
                if fragment_size + increment > fragment_limit:
                    raise ValueError(
                        f"Ein einzelner Eintrag {field}[{item_index}] aus "
                        f"Route {route_index} überschreitet max_batch_bytes."
                    )
                fragment[field].append(item)
                fragment_size += increment
                has_entries = True
        if has_entries or not any(raw.get(field) for field in parts):
            emit(fragment, fragment_size)
    return fragments


def transform_json(
    *,
    kind: str,
    value: Any,
    batch_size: int = 5,
    max_batch_bytes: int = 80_000,
    serializer: Callable[[Any], str] | None = None,
) -> dict[str, Any]:
    def size(item: Any) -> int:
        return _serialized_size(item, serializer=serializer)

    if not isinstance(value, list):
        raise ValueError("JSON-Operation benötigt eine JSON-Liste.")
    if kind == "flatten":
        result: list[Any] = []
        for entry in value:
            if not isinstance(entry, list):
                raise ValueError("flatten erwartet ausschließlich Listen.")
            if len(result) + len(entry) > 1_000_000:
                raise ValueError("flatten überschreitet das Elementlimit.")
            result.extend(entry)
        if size({"items": result}) > MAX_RESULT_BYTES:
            raise ValueError("flatten überschreitet das JSON-Ergebnislimit.")
        return {"items": result}
    if kind not in {"batch", "batch_routes"}:
        raise ValueError(f"Unbekannte JSON-Operation: {kind!r}")
    if not 1 <= batch_size <= 1000:
        raise ValueError("batch_size muss zwischen 1 und 1000 liegen.")
    if not 1024 <= max_batch_bytes <= 10_000_000:
        raise ValueError("max_batch_bytes muss zwischen 1024 und 10000000 liegen.")
    if kind == "batch_routes":
        fragments = _split_route_records(
            value, max_batch_bytes=max_batch_bytes, serializer=serializer,
        )
        return transform_json(
            kind="batch", value=fragments, batch_size=batch_size,
            max_batch_bytes=max_batch_bytes, serializer=serializer,
        )

    batches: list[dict[str, Any]] = []
    current: list[Any] = []
    count = 0
    next_id = "b0001"
    current_size = size({"id": next_id, "items": []})
    result_size = size({"batches": [], "total_items": len(value)})

    def finish() -> None:
        nonlocal current, current_size, result_size, next_id
        if not current:
            return
        if len(batches) >= MAX_BATCHES:
            raise ValueError("batch überschreitet das Batch-Anzahllimit.")
        increment = current_size + (1 if batches else 0)
        if result_size + increment > MAX_RESULT_BYTES:
            raise ValueError("batch überschreitet das JSON-Ergebnislimit.")
        result_size += increment
        batches.append({"id": next_id, "items": current})
        current = []
        next_id = f"b{len(batches)+1:04d}"
        current_size = size({"id": next_id, "items": []})

    for entry in value:
        entry_size = size(entry)
        required = entry_size + (1 if current else 0)
        if current and (
            count >= batch_size or current_size + required > max_batch_bytes
        ):
            finish()
            count = 0
            required = entry_size
        if current_size + required > max_batch_bytes:
            raise ValueError("Ein JSON-Eintrag überschreitet max_batch_bytes.")
        if len(batches) >= MAX_BATCHES:
            raise ValueError("batch überschreitet das Batch-Anzahllimit.")
        if result_size + current_size + required > MAX_RESULT_BYTES:
            raise ValueError("batch überschreitet das JSON-Ergebnislimit.")
        current.append(entry)
        current_size += required
        count += 1

    finish()
    return {"batches": batches, "total_items": len(value)}


class _JsonNumber(str):
    """Lossless representation of a validated JSON non-integer number."""


def _json_dump_string(value: str) -> str:
    dumped = json.dumps(value, ensure_ascii=False)
    return "".join(
        f"\\u{ord(char):04x}"
        if 0xD800 <= ord(char) <= 0xDFFF
        else char
        for char in dumped
    )


def _json_dumps_preserving_numbers(value: Any) -> str:
    if isinstance(value, _JsonNumber):
        return str(value)
    if value is None:
        return "null"
    if value is True:
        return "true"
    if value is False:
        return "false"
    if isinstance(value, str):
        return _json_dump_string(value)
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        try:
            return json.dumps(
                value,
                ensure_ascii=False,
                allow_nan=False,
                separators=(",", ":"),
            )
        except ValueError as exc:
            raise TypeError(
                "Nicht unterstützter JSON-Wert: nicht-endlicher float"
            ) from exc
    if isinstance(value, list):
        return "[" + ",".join(
            _json_dumps_preserving_numbers(item)
            for item in value
        ) + "]"
    if isinstance(value, dict):
        parts: list[str] = []
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("JSON-Objektschlüssel müssen Strings sein.")
            parts.append(
                _json_dump_string(key)
                + ":"
                + _json_dumps_preserving_numbers(item)
            )
        return "{" + ",".join(parts) + "}"
    raise TypeError(
        f"Nicht unterstützter JSON-Wert: {type(value).__name__}"
    )


