"""Pure, bounded JSON transformations for Flow Runner steps."""
from __future__ import annotations

import json
from typing import Any, Callable

MAX_BATCHES = 1000
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
    if kind != "batch":
        raise ValueError(f"Unbekannte JSON-Operation: {kind!r}")
    if not 1 <= batch_size <= 1000:
        raise ValueError("batch_size muss zwischen 1 und 1000 liegen.")
    if not 1024 <= max_batch_bytes <= 10_000_000:
        raise ValueError("max_batch_bytes muss zwischen 1024 und 10000000 liegen.")
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
