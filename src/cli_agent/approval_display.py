from __future__ import annotations

import json
from typing import Any

SENSITIVE_ARGUMENT_MARKERS = (
    "auth",
    "api_key",
    "apikey",
    "authorization",
    "credential",
    "password",
    "secret",
    "token",
)
MAX_APPROVAL_STRING_CHARS = 2_000
MAX_APPROVAL_STRING_HEAD = 1_500
MAX_APPROVAL_STRING_TAIL = 500
MAX_APPROVAL_COLLECTION_ITEMS = 30


def _is_sensitive_argument_name(name: str) -> bool:
    normalized = name.casefold().replace("-", "_")
    return any(marker in normalized for marker in SENSITIVE_ARGUMENT_MARKERS)


def _preview_string(value: str) -> str:
    if len(value) <= MAX_APPROVAL_STRING_CHARS:
        return value
    omitted = len(value) - MAX_APPROVAL_STRING_HEAD - MAX_APPROVAL_STRING_TAIL
    return (
        value[:MAX_APPROVAL_STRING_HEAD]
        + f"\n... <{omitted} Zeichen gekürzt; insgesamt {len(value)} Zeichen> ...\n"
        + value[-MAX_APPROVAL_STRING_TAIL:]
    )


def _approval_value(name: str, value: object) -> object:
    if _is_sensitive_argument_name(name):
        return "<verborgen>"
    if isinstance(value, dict):
        items = list(value.items())
        result = {
            str(key): _approval_value(str(key), item)
            for key, item in items[:MAX_APPROVAL_COLLECTION_ITEMS]
        }
        if len(items) > MAX_APPROVAL_COLLECTION_ITEMS:
            result["<gekürzt>"] = f"{len(items) - MAX_APPROVAL_COLLECTION_ITEMS} weitere Einträge"
        return result
    if isinstance(value, (list, tuple)):
        result = [
            _approval_value(name, item)
            for item in value[:MAX_APPROVAL_COLLECTION_ITEMS]
        ]
        if len(value) > MAX_APPROVAL_COLLECTION_ITEMS:
            result.append(f"<{len(value) - MAX_APPROVAL_COLLECTION_ITEMS} weitere Elemente gekürzt>")
        return result
    if isinstance(value, bytes):
        return f"<{len(value)} Bytes>"
    if isinstance(value, str):
        return _preview_string(value)
    return value


def approval_arguments(arguments: dict[str, object]) -> str:
    summary = {
        str(name): _approval_value(str(name), value)
        for name, value in arguments.items()
    }
    return json.dumps(summary, ensure_ascii=False, sort_keys=True)
