from __future__ import annotations

import hashlib
import json
import re
from typing import Any

_CONTRACT_PATTERN = re.compile(r"^sha256:[0-9a-f]{64}$")


def tool_contract_fingerprint(tool_name: str, input_schema: Any) -> str:
    """Return a stable fingerprint for the callable MCP tool contract.

    Descriptions and server instructions are intentionally not part of this
    contract. The fingerprint binds the native tool name to its complete JSON
    input schema so that a persistent auto-approval stops applying when the
    callable interface changes.
    """

    name = str(tool_name).strip()
    if not name:
        raise ValueError("MCP-Toolname darf für einen Contract nicht leer sein.")
    try:
        canonical = json.dumps(
            {
                "version": 1,
                "tool_name": name,
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
