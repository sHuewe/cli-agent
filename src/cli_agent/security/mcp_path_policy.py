"""Bounded, private file transport for built-in MCP filesystem policies."""

from __future__ import annotations

import json
from pathlib import Path

MAX_POLICY_BYTES = 16 * 1024 * 1024
MAX_POLICY_PATHS = 100_000
_POLICY_KEYS = frozenset({"protected_paths", "mutation_protected_paths", "allowed_paths"})


def _validate_policy(data: object) -> dict[str, tuple]:
    if not isinstance(data, dict) or (set(data) - _POLICY_KEYS) or not {"protected_paths", "mutation_protected_paths"} <= set(data):
        raise ValueError("Ungültige MCP-Pfad-Policy.")
    result: dict[str, tuple] = {}
    for key in ("protected_paths", "mutation_protected_paths"):
        entries = data[key]
        if (
            not isinstance(entries, list)
            or len(entries) > MAX_POLICY_PATHS
            or any(not isinstance(item, str) or not item or "\x00" in item for item in entries)
        ):
            raise ValueError(f"Ungültige MCP-Pfadliste: {key}.")
        result[key] = tuple(Path(item) for item in entries)
    grants = data.get("allowed_paths", [])
    if not isinstance(grants, list) or len(grants) > MAX_POLICY_PATHS:
        raise ValueError("Ungültige MCP-allowed_paths.")
    checked = []
    for grant in grants:
        if (not isinstance(grant, dict) or set(grant) != {"path", "access"}
                or not isinstance(grant["path"], str) or not grant["path"]
                or "\x00" in grant["path"] or grant["access"] not in {"read", "write"}):
            raise ValueError("Ungültiger MCP-allowed_paths-Eintrag.")
        checked.append({"path": grant["path"], "access": grant["access"]})
    result["allowed_paths"] = tuple(checked)
    return result


def write_path_policy(path: Path, data: dict) -> None:
    validated = _validate_policy(data)
    serialized = json.dumps(
        {**{key: [str(value) for value in validated[key]] for key in ("protected_paths", "mutation_protected_paths")}, "allowed_paths": list(validated["allowed_paths"])},
        ensure_ascii=True,
    ).encode("utf-8")
    if len(serialized) > MAX_POLICY_BYTES:
        raise ValueError("MCP-Pfad-Policy überschreitet das Größenlimit.")
    with path.open("xb") as handle:
        handle.write(serialized)


def read_path_policy(path: Path | None) -> dict[str, tuple]:
    if path is None:
        return {"protected_paths": (), "mutation_protected_paths": (), "allowed_paths": ()}
    try:
        with path.open("rb") as handle:
            raw = handle.read(MAX_POLICY_BYTES + 1)
        if len(raw) > MAX_POLICY_BYTES:
            raise ValueError("MCP-Pfad-Policy überschreitet das Größenlimit.")
        return _validate_policy(json.loads(raw.decode("utf-8")))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"MCP-Pfad-Policy nicht lesbar oder ungültig: {path}") from exc
