"""Pure, bounded JSON transformations for Flow Runner steps.

Operations never access the filesystem or an LLM. Input files are resolved and
verified separately by the Flow Runner's existing json_inputs machinery.
"""
from __future__ import annotations

from typing import Any


def transform_json(
    *,
    kind: str,
    value: Any,
    batch_size: int = 5,
    max_batch_bytes: int = 80_000,
) -> dict[str, Any]:
    import json

    if not isinstance(value, list):
        raise ValueError("JSON-Operation benötigt eine JSON-Liste.")
    if kind == "flatten":
        result: list[Any] = []
        for entry in value:
            if not isinstance(entry, list):
                raise ValueError("flatten erwartet ausschließlich Listen.")
            result.extend(entry)
        return {"items": result}
    if kind != "batch":
        raise ValueError(f"Unbekannte JSON-Operation: {kind!r}")
    if not 1 <= batch_size <= 1000:
        raise ValueError("batch_size muss zwischen 1 und 1000 liegen.")
    if not 1024 <= max_batch_bytes <= 10_000_000:
        raise ValueError("max_batch_bytes muss zwischen 1024 und 10000000 liegen.")
    batches: list[dict[str, Any]] = []
    current: list[Any] = []
    size = 2
    for entry in value:
        entry_size = len(json.dumps(entry, ensure_ascii=True, allow_nan=False).encode("utf-8"))
        if entry_size + 2 > max_batch_bytes:
            raise ValueError("Ein JSON-Eintrag überschreitet max_batch_bytes.")
        if current and (
            len(current) >= batch_size or size + entry_size + 1 > max_batch_bytes
        ):
            batches.append({"id": f"b{len(batches)+1:04d}", "items": current})
            current = []
            size = 2
        current.append(entry)
        size += entry_size + (1 if len(current) > 1 else 0)
    if current:
        batches.append({"id": f"b{len(batches)+1:04d}", "items": current})
    return {"batches": batches, "total_items": len(value)}
