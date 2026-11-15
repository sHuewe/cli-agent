"""Bounded opt-in SDK tool argument capture; never used for standard events.

The caller who opts in is responsible for appropriately protecting AgentResult
and any derived files. Redaction is best effort, not a secrets detector.
"""
from __future__ import annotations

import json
import re
from typing import Any

MAX_ARGUMENT_BYTES = 8192
MAX_TOTAL_BYTES = 65536
MAX_TOOL_CALLS = 256
REDACTED = "[REDACTED]"
DEFAULT_SECRET_KEYS = frozenset({
    "password", "passwd", "pwd", "secret", "token", "access_token",
    "refresh_token", "id_token", "api_key", "apikey", "private_key",
    "client_secret", "authorization", "auth", "cookie", "set_cookie",
    "credential", "credentials", "bearer", "session_key",
})


def _normalize_key(key: str) -> str:
    return re.sub(r"[^a-z0-9]", "", key.casefold())


def validate_redact_keys(keys: tuple[str, ...]) -> tuple[str, ...]:
    if not isinstance(keys, (tuple, list)) or len(keys) > 128:
        raise TypeError("redact_keys must be a sequence of at most 128 names")
    if any(not isinstance(k, str) or not k.strip() or len(k) > 128 for k in keys):
        raise ValueError("redact_keys entries must be nonempty strings up to 128 chars")
    return tuple(keys)


def _safe_value(value: Any, secrets: frozenset[str], depth: int = 0) -> Any:
    if depth > 16:
        return "[DEPTH LIMIT]"
    if isinstance(value, dict):
        if len(value) > 2048:
            return "[TOO MANY FIELDS]"
        return {
            str(k): REDACTED if _normalize_key(str(k)) in secrets
            else _safe_value(v, secrets, depth + 1)
            for k, v in value.items()
        }
    if isinstance(value, (list, tuple)):
        if len(value) > 2048:
            return "[TOO MANY ITEMS]"
        return [_safe_value(v, secrets, depth + 1) for v in value]
    if value is None or isinstance(value, (str, int, bool)):
        return value
    if isinstance(value, float):
        return value if value == value and abs(value) < float("inf") else "[NONFINITE]"
    return "[UNSERIALIZABLE]"


class ToolArgumentCollector:
    """Collect call_id-keyed argument snapshots, bounded per call and per run."""

    def __init__(self, *, extra_redact_keys: tuple[str, ...] = ()) -> None:
        keys = DEFAULT_SECRET_KEYS | frozenset(extra_redact_keys)
        self._secrets = frozenset(_normalize_key(k) for k in keys)
        self._records: dict[str, tuple[dict | None, bool]] = {}
        self._bytes = 0
        self.total_records = 0

    def record(self, call_id: str, arguments: Any) -> None:
        if call_id in self._records:
            return
        if self.total_records >= MAX_TOOL_CALLS:
            return
        self.total_records += 1
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except (ValueError, TypeError):
                self._records[call_id] = (None, True)
                return
        if not isinstance(arguments, dict):
            self._records[call_id] = (None, True)
            return
        redacted = _safe_value(arguments, self._secrets)
        if not isinstance(redacted, dict):
            self._records[call_id] = (None, True)
            return
        try:
            size = len(json.dumps(redacted, ensure_ascii=False, allow_nan=False).encode("utf-8"))
        except (TypeError, ValueError, UnicodeError):
            self._records[call_id] = (None, True)
            return
        if size > MAX_ARGUMENT_BYTES or self._bytes + size > MAX_TOTAL_BYTES:
            # Never retain an unparseable partial JSON or a partial secret.
            self._records[call_id] = (None, True)
            return
        self._bytes += size
        self._records[call_id] = (redacted, False)

    def pop(self, call_id: str) -> tuple[dict | None, bool]:
        return self._records.pop(call_id, (None, False))
