"""Bounded, untrusted reference text supplied by the Python SDK."""
from __future__ import annotations

from typing import Any

MAX_SDK_CONTEXTS = 32
MAX_SDK_CONTEXT_BYTES = 128 * 1024
MAX_SDK_TOTAL_CONTEXT_BYTES = 512 * 1024


def add_reference_context(runtime: Any, text: str, name: str | None) -> str:
    """Add a snapshot for future requests without modifying conversation history."""
    if not text.strip():
        raise ValueError("Context must not be empty")
    size = len(text.encode("utf-8"))
    if size > MAX_SDK_CONTEXT_BYTES:
        raise ValueError("Context exceeds per-entry size limit")
    if name is not None and (
        not isinstance(name, str) or not name.strip()
        or len(name) > 128
    ):
        raise ValueError("Context name must be a nonempty string of at most 128 characters")
    entries = getattr(runtime, "_sdk_reference_contexts", None)
    if entries is None:
        entries = []
        runtime._sdk_reference_contexts = entries
    if len(entries) >= MAX_SDK_CONTEXTS:
        raise ValueError("Too many SDK contexts")
    if size + sum(entry["bytes"] for entry in entries) > MAX_SDK_TOTAL_CONTEXT_BYTES:
        raise ValueError("SDK context total size limit exceeded")
    identifier = f"sdk:{len(entries)}"
    entries.append({
        "id": identifier,
        "label": name or f"Python context {len(entries) + 1}",
        "content": text,
        "bytes": size,
    })
    return identifier
