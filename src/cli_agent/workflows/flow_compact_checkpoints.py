"""Durable, bounded completion records for compact child flow invocations.

Intermediate output deletion is deliberately restricted to paths reported by
the executing child; this module never expands wildcard output templates.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
import stat
import tempfile
from pathlib import Path
from typing import Any, Callable, NamedTuple

from cli_agent.context.file_context import read_existing_output_text
from cli_agent.workflows.flow_json_operations import _JsonNumber
from cli_agent.security.filesystem_security import path_entry_is_symlink_or_reparse

MARKER_DIRECTORY = ".cli-agent/flow-checkpoints"
MAX_MARKERS = 100_000
MAX_MARKER_BYTES = 100_000_000


class CompletionRecord(NamedTuple):
    result: str
    is_json: bool
    # Paths are returned only after the entire marker was verified.
    preserved_json: tuple[tuple[Path, bytes], ...]
    preserved_outputs: tuple[tuple[Path, bool, bool], ...]


def _read_marker(path: Path) -> str:
    # Markers live under the deliberately LLM-inaccessible .cli-agent tree.
    # The ordinary output reader intentionally rejects that directory.
    if path_entry_is_symlink_or_reparse(path):
        raise ValueError("Unsafe compact checkpoint marker.")
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    fd = os.open(path, flags)
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_MARKER_BYTES:
            raise ValueError("Invalid compact checkpoint marker type or size.")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            data = handle.read(MAX_MARKER_BYTES + 1)
        if len(data) > MAX_MARKER_BYTES:
            raise ValueError("Compact checkpoint marker exceeds size limit.")
        return data.decode("utf-8").removeprefix("\ufeff").replace("\r\n", "\n").replace("\r", "\n")
    finally:
        os.close(fd)


def _digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _typed_identity_value(value: Any) -> Any:
    """Canonical and injective encoding of the supported flow-input JSON types.

    _JsonNumber is a *str subclass*. Plain json.dumps would conflate the
    numeric literal 1.5 with the string "1.5", yielding unsafe cache hits.
    Every node receives a type discriminator; object keys are sorted.
    """
    if isinstance(value, _JsonNumber):
        return ["number", str(value)]
    if value is None:
        return ["null"]
    if type(value) is bool:
        return ["bool", value]
    if type(value) is int:
        return ["integer", str(value)]
    if type(value) is float:
        if not math.isfinite(value):
            raise ValueError("Nonfinite flow input cannot be checkpointed.")
        return ["float", repr(value)]
    if type(value) is str:
        return ["string", value]
    if isinstance(value, list):
        return ["array", [_typed_identity_value(item) for item in value]]
    if isinstance(value, dict):
        if not all(type(key) is str for key in value):
            raise TypeError("Flow input keys must be strings.")
        return [
            "object",
            [[key, _typed_identity_value(value[key])] for key in sorted(value)],
        ]
    raise TypeError(f"Unsupported flow input identity type: {type(value).__name__}")


def checkpoint_path(
    workspace: Path, flow_source: Path, namespace: str, flow_input: dict[str, Any],
) -> tuple[Path, str]:
    """Hash a concrete invocation, preserving every input's JSON type."""
    identity = json.dumps(
        {
            "flow": flow_source.relative_to(workspace).as_posix(),
            "definition_sha256": hashlib.sha256(flow_source.read_bytes()).hexdigest(),
            "namespace": namespace,
            "input": _typed_identity_value(flow_input),
        },
        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
    )
    key = _digest(identity)
    return workspace / MARKER_DIRECTORY / (key + ".json"), key


def snapshot_markers(workspace: Path) -> dict[str, bytes]:
    """Capture existing marker fingerprints before execution begins.

    Newly generated marker files are never accepted during the same run.
    """
    folder = workspace / MARKER_DIRECTORY
    if not folder.exists():
        return {}
    if path_entry_is_symlink_or_reparse(folder) or not folder.is_dir():
        raise ValueError("Unsafe compact checkpoint directory.")
    # Check every ancestor, including .cli-agent, for indirection.
    current = workspace
    for name in Path(MARKER_DIRECTORY).parts:
        current = current / name
        if path_entry_is_symlink_or_reparse(current):
            raise ValueError("Unsafe compact checkpoint directory.")
    result: dict[str, bytes] = {}
    with os.scandir(folder) as entries:
        for entry in entries:
            if not entry.name.endswith(".json"):
                continue
            if len(result) >= MAX_MARKERS:
                raise ValueError("Too many compact checkpoint markers.")
            path = Path(entry.path)
            if entry.is_symlink() or not entry.is_file(follow_symlinks=False):
                raise ValueError("Unsafe compact checkpoint entry.")
            if path.stat().st_size > MAX_MARKER_BYTES:
                raise ValueError("Compact checkpoint marker exceeds size limit.")
            text = _read_marker(path)
            result[entry.name] = hashlib.sha256(text.encode("utf-8")).digest()
    return result


def read_checkpoint(
    workspace: Path,
    flow_source: Path,
    namespace: str,
    flow_input: dict[str, Any],
    run_start_markers: dict[str, bytes],
    resolve_path: Callable[[Path], Path],
) -> CompletionRecord | None:
    path, key = checkpoint_path(workspace, flow_source, namespace, flow_input)
    fingerprint = run_start_markers.get(path.name)
    if fingerprint is None:
        return None
    # Re-check the protected parent directories at the point of use; a
    # run-start validation alone must not authorize later path replacement.
    current = workspace
    for component in Path(MARKER_DIRECTORY).parts:
        current = current / component
        if path_entry_is_symlink_or_reparse(current):
            raise ValueError("Compact checkpoint directory was replaced by a link.")
    if path.is_symlink() or path_entry_is_symlink_or_reparse(path):
        raise ValueError("Compact checkpoint marker was replaced by a link.")
    text = _read_marker(path)
    if hashlib.sha256(text.encode("utf-8")).digest() != fingerprint:
        raise ValueError(f"Compact checkpoint changed since run startup: {path}")
    try:
        payload = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"Invalid compact checkpoint: {path}") from exc
    if (
        not isinstance(payload, dict)
        or payload.get("version") != 1
        or payload.get("identity") != key
        or not isinstance(payload.get("result"), str)
        or type(payload.get("is_json")) is not bool
        or not isinstance(payload.get("preserved"), list)
    ):
        raise ValueError(f"Invalid compact checkpoint: {path}")
    preserved_json: list[tuple[Path, bytes]] = []
    preserved_outputs: list[tuple[Path, bool, bool]] = []
    for file in payload["preserved"]:
        if (
            not isinstance(file, dict)
            or not isinstance(file.get("path"), str)
            or not isinstance(file.get("sha256"), str)
            or type(file.get("is_json")) is not bool
            or type(file.get("allow_replace", False)) is not bool
        ):
            raise ValueError("Invalid preserved output in compact checkpoint.")
        target = resolve_path(Path(file["path"]))
        if not target.is_file():
            raise ValueError(f"Preserved compact checkpoint output is missing: {target}")
        current = read_existing_output_text(
            workspace, target, max_bytes=MAX_MARKER_BYTES,
        )
        if _digest(current) != file["sha256"]:
            raise ValueError(f"Preserved compact checkpoint output changed: {target}")
        preserved_outputs.append((
            target, file["is_json"], file.get("allow_replace", False),
        ))
        if file["is_json"]:
            preserved_json.append((target, hashlib.sha256(current.encode("utf-8")).digest()))
    return CompletionRecord(
        payload["result"], payload["is_json"],
        tuple(preserved_json), tuple(preserved_outputs),
    )


def write_checkpoint(
    workspace: Path,
    flow_source: Path,
    namespace: str,
    flow_input: dict[str, Any],
    result: str,
    is_json: bool,
    preserved: list[dict[str, Any]],
    ensure_parent: Callable[[Path], None],
) -> None:
    """Atomically publish completion before callers remove intermediates."""
    path, key = checkpoint_path(workspace, flow_source, namespace, flow_input)
    payload = json.dumps(
        {
            "version": 1, "identity": key, "result": result,
            "is_json": is_json, "preserved": preserved,
        },
        ensure_ascii=False, separators=(",", ":"),
    )
    if len(payload.encode("utf-8")) > MAX_MARKER_BYTES:
        raise ValueError("Compact checkpoint result exceeds size limit.")
    ensure_parent(path)
    if path_entry_is_symlink_or_reparse(path):
        raise ValueError("Unsafe compact checkpoint target.")
    fd, temporary = tempfile.mkstemp(prefix=".compact-", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(payload)
            handle.flush()
            os.fsync(handle.fileno())
        if path_entry_is_symlink_or_reparse(path):
            raise ValueError("Unsafe compact checkpoint target.")
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def cleanup_files(
    workspace: Path,
    entries: list[tuple[Path, str]],
    resolve_path: Callable[[Path], Path],
) -> int:
    """Delete only owned, unchanged files after the completion record exists."""
    removed = 0
    for path, expected_hash in entries:
        target = resolve_path(path.relative_to(workspace))
        if not target.is_file():
            continue
        content = read_existing_output_text(
            workspace, target, max_bytes=MAX_MARKER_BYTES,
        )
        if _digest(content) != expected_hash:
            raise ValueError(f"Intermediate checkpoint changed before cleanup: {target}")
        target.unlink()
        removed += 1
    return removed
