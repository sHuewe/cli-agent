from __future__ import annotations

import hashlib
import json
import re
from typing import Any

_CONTRACT_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


def _normalize_tool_description(description: str | None) -> str:
    """Normalize formatting-only differences in an MCP tool description."""

    text = "" if description is None else str(description)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.rstrip(" \t") for line in text.split("\n")]
    while lines and lines[-1] == "":
        lines.pop()
    return "\n".join(lines)


def tool_contract_fingerprint(tool_name: str, description: str | None, input_schema: Any) -> str:
    """Return a stable fingerprint for the model-visible MCP tool contract.

    The fingerprint binds the native tool name, normalized model-visible tool
    description and complete JSON input schema. Line-ending differences and
    trailing spaces or tabs in the description are ignored so formatting-only
    changes do not invalidate a persistent auto-approval.
    """

    name = str(tool_name).strip()
    if not name:
        raise ValueError("MCP-Toolname darf für einen Contract nicht leer sein.")
    try:
        canonical = json.dumps(
            {
                "version": 2,
                "tool_name": name,
                "description": _normalize_tool_description(description),
                "input_schema": input_schema,
            },
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError) as exc:
        raise ValueError(
            f"Input-Schema von MCP-Tool {name!r} ist nicht JSON-serialisierbar."
        ) from exc
    digest = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return f"sha256:{digest}"


def validate_tool_contract_fingerprint(value: str, *, section: str) -> str:
    normalized = value.strip().lower()
    if not _CONTRACT_PATTERN.fullmatch(normalized):
        raise ValueError(
            f"{section}.contract_sha256 muss 'sha256:' gefolgt von exakt "
            "64 hexadezimalen Zeichen enthalten."
        )
    return normalized
