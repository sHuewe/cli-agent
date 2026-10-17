"""Bounded, private file transport for built-in MCP filesystem policies."""

from __future__ import annotations

import json
from pathlib import Path

MAX_POLICY_BYTES = 16 * 1024 * 1024
MAX_POLICY_PATHS = 100_000
_POLICY_KEYS = frozenset({"protected_paths", "mutation_protected_paths"})


def _validate_policy(data: object) -> dict[str, tuple[Path, ...]]:
    if not isinstance(data, dict) or set(data) != _POLICY_KEYS:
        raise ValueError("Ungültige MCP-Pfad-Policy.")
    result: dict[str, tuple[Path, ...]] = {}
    for key in _POLICY_KEYS:
        entries = data[key]
        if (
            not isinstance(entries, list)
            or len(entries) > MAX_POLICY_PATHS
            or any(not isinstance(item, str) or not item or "\x00" in item for item in entries)
        ):
            raise ValueError(f"Ungültige MCP-Pfadliste: {key}.")
        result[key] = tuple(Path(item) for item in entries)
    return result


def write_path_policy(path: Path, data: dict[str, list[str]]) -> None:
    validated = _validate_policy(data)
    serialized = json.dumps(
        {key: [str(value) for value in validated[key]] for key in sorted(_POLICY_KEYS)},
        ensure_ascii=True,
    ).encode("utf-8")
    if len(serialized) > MAX_POLICY_BYTES:
        raise ValueError("MCP-Pfad-Policy überschreitet das Größenlimit.")
    with path.open("xb") as handle:
        handle.write(serialized)


def read_path_policy(path: Path | None) -> dict[str, tuple[Path, ...]]:
    if path is None:
        return {"protected_paths": (), "mutation_protected_paths": ()}
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_POLICY_BYTES + 1)
        if len(raw) > MAX_POLICY_BYTES:
            raise ValueError("MCP-Pfad-Policy überschreitet das Größenlimit.")
        return _validate_policy(json.loads(raw.decode("utf-8")))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"MCP-Pfad-Policy nicht lesbar oder ungültig: {path}") from exc
