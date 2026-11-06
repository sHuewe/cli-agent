"""Versioned, project-local snapshots of completed Agent API sessions.

Snapshots contain only durable conversation/evidence data. They do not
restore MCP transports, approvals, temporary tool artifacts or active runs.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat
import uuid
from typing import Any

from cli_agent.security.filesystem_security import (
    path_entry_is_symlink_or_reparse,
    regular_file_has_multiple_links,
)
from cli_agent.state.evidence_state import EvidenceState

SCHEMA_VERSION = 1
MAX_SNAPSHOT_BYTES = 32 * 1024 * 1024
MAX_SAVED_SESSIONS = 200
MAX_NAME_LENGTH = 120
_SNAPSHOT_ID = re.compile(r"[a-f0-9]{32}\Z")
_ARTIFACT_FIELDS = {"workspace_path", "records_path", "record_sets"}


def _remove_artifact_paths(value: Any) -> Any:
    """Discard ephemeral Data MCP references without losing source provenance."""
    if isinstance(value, dict):
        return {key: _remove_artifact_paths(item) for key, item in value.items()
                if key not in _ARTIFACT_FIELDS}
    if isinstance(value, list):
        return [_remove_artifact_paths(item) for item in value]
    return value


def _conversation(messages: Any, *, allow_ids: bool = False) -> list[dict[str, str]]:
    if not isinstance(messages, list) or len(messages) > 500:
        raise ValueError("Invalid snapshot conversation.")
    clean: list[dict[str, str]] = []
    for message in messages:
        if not isinstance(message, dict):
            raise ValueError("Invalid snapshot message.")
        role, content = message.get("role"), message.get("content")
        if role not in {"user", "assistant"} or not isinstance(content, str):
            raise ValueError("Snapshot messages must be user/assistant text.")
        if len(content) > 100_000:
            raise ValueError("Snapshot message is too large.")
        entry = {"role": role, "content": content}
        if allow_ids and isinstance(message.get("id"), str):
            entry["id"] = message["id"][:128]
        clean.append(entry)
    return clean


def _validate_id(snapshot_id: str) -> str:
    if not isinstance(snapshot_id, str) or not _SNAPSHOT_ID.fullmatch(snapshot_id):
        raise ValueError("Invalid saved session ID.")
    return snapshot_id


def _directory(path: Path, *, create: bool = False) -> None:
    if path_entry_is_symlink_or_reparse(path):
        raise ValueError("Session storage must not contain links.")
    if create:
        path.mkdir(mode=0o700, exist_ok=True)
    if not path.is_dir() or path_entry_is_symlink_or_reparse(path):
        raise ValueError("Session storage must be a real directory.")


class SessionPersistence:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.expanduser().resolve(strict=True)
        if not self.workspace.is_dir():
            raise ValueError("Workspace is not a directory.")
        self.root = self.workspace / ".cli-agent" / "sessions"

    def _root(self, *, create: bool = False) -> Path:
        _directory(self.workspace / ".cli-agent", create=create)
        _directory(self.root, create=create)
        return self.root

    def _file(self, snapshot_id: str, *, create: bool = False) -> Path:
        root = self._root(create=create)
        folder = root / _validate_id(snapshot_id)
        _directory(folder, create=create)
        return folder / "session.json"

    def _read(self, snapshot_id: str) -> dict[str, Any]:
        path = self._file(snapshot_id)
        if path_entry_is_symlink_or_reparse(path):
            raise ValueError("Snapshot file must not be a link.")
        info = path.stat()
        if not stat.S_ISREG(info.st_mode) or regular_file_has_multiple_links(path):
            raise ValueError("Snapshot must be a single regular file.")
        if info.st_size > MAX_SNAPSHOT_BYTES:
            raise ValueError("Snapshot exceeds the size limit.")
        with path.open("rb") as stream:
            raw = stream.read(MAX_SNAPSHOT_BYTES + 1)
        if len(raw) > MAX_SNAPSHOT_BYTES:
            raise ValueError("Snapshot exceeds the size limit.")
        try:
            document = json.loads(raw)
        except (UnicodeError, ValueError) as exc:
            raise ValueError("Invalid snapshot JSON.") from exc
        if not isinstance(document, dict) or document.get("schema_version") != SCHEMA_VERSION:
            raise ValueError("Unsupported session snapshot schema.")
        if document.get("id") != snapshot_id:
            raise ValueError("Snapshot ID does not match its directory.")
        return document

    def list(self) -> list[dict[str, Any]]:
        root = self.workspace / ".cli-agent"
        if not root.exists() and not root.is_symlink():
            return []
        snapshots = self.root
        if not snapshots.exists() and not snapshots.is_symlink():
            _directory(root)
            return []
        entries = self._root().iterdir()
        results = []
        for entry in entries:
            if not _SNAPSHOT_ID.fullmatch(entry.name):
                continue
            # Reject rather than follow a malicious directory/link.
            try:
                document = self._read(entry.name)
                results.append({
                    "id": entry.name,
                    "name": document["name"],
                    "saved_at": document["saved_at"],
                })
            except (OSError, ValueError, KeyError, TypeError):
                # A damaged snapshot is not loadable.
                continue
            if len(results) >= MAX_SAVED_SESSIONS:
                break
        return sorted(results, key=lambda row: row["saved_at"], reverse=True)

    def load(self, snapshot_id: str) -> dict[str, Any]:
        document = self._read(snapshot_id)
        name = document.get("name")
        if not isinstance(name, str) or not name.strip() or len(name) > MAX_NAME_LENGTH:
            raise ValueError("Invalid saved session name.")
        history = _conversation(document.get("history"))
        ui_history = _conversation(document.get("ui_messages"))
        agui_history = _conversation(document.get("agui_messages"), allow_ids=True)
        state = document.get("evidence")
        if not isinstance(state, dict):
            raise ValueError("Invalid saved evidence.")
        evidence = EvidenceState()
        stored = state.get("evidences")
        sources = state.get("tool_results")
        if not isinstance(stored, list) or not isinstance(sources, dict):
            raise ValueError("Invalid saved evidence registry.")
        if len(stored) > 200 or len(sources) > 1000:
            raise ValueError("Saved evidence registry is too large.")
        if not isinstance(state.get("next_source_number"), int) or state["next_source_number"] < 1:
            raise ValueError("Invalid source sequence.")
        for entry in stored:
            if not isinstance(entry, dict) or not isinstance(entry.get("id"), str):
                raise ValueError("Invalid evidence entry.")
            if entry["id"] in evidence.evidences:
                raise ValueError("Duplicate evidence entry.")
            evidence.evidences[entry["id"]] = _remove_artifact_paths(entry)
        for call_id, source in sources.items():
            if not isinstance(call_id, str) or not isinstance(source, dict):
                raise ValueError("Invalid evidence source.")
            evidence.tool_results[call_id] = _remove_artifact_paths(source)
        # Plan belongs to the prior task; keep no active plan when continuing.
        # Existing evidence must not contain dead plan-step references.
        for entry in evidence.evidences.values():
            entry.pop("plan_step_id", None)
        evidence._next_source_number = state["next_source_number"]
        evidence._refresh_live_tool_definition()
        return {
            "id": snapshot_id, "name": name,
            "history": history, "ui_messages": ui_history,
            "agui_messages": agui_history, "evidence": evidence,
        }

    def save(self, session: Any, name: str, *, snapshot_id: str | None = None) -> dict[str, str]:
        if not isinstance(name, str) or not name.strip() or len(name) > MAX_NAME_LENGTH:
            raise ValueError("Please provide a session name (max 120 characters).")
        if session._busy or session._closed or session._pending_interrupt is not None:
            raise RuntimeError("Only idle sessions can be saved.")
        agent = session.agent
        if agent is None:
            raise RuntimeError("Session is not ready.")
        evidence = getattr(agent, "_evidence_state", None)
        if evidence is None:
            evidence = EvidenceState()
        if snapshot_id is None:
            if len(self.list()) >= MAX_SAVED_SESSIONS:
                raise RuntimeError("Maximum number of saved sessions reached.")
            snapshot_id = uuid.uuid4().hex
        else:
            _validate_id(snapshot_id)
            self._read(snapshot_id)  # no overwrite of an unvalidated existing slot
        document = {
            "schema_version": SCHEMA_VERSION,
            "id": snapshot_id,
            "name": name.strip(),
            "saved_at": datetime.now(timezone.utc).isoformat(),
            "history": _conversation(agent.history),
            "ui_messages": _conversation(session.ui_messages),
            "agui_messages": _conversation(session.agui_messages, allow_ids=True),
            "evidence": {
                "evidences": _remove_artifact_paths(copy.deepcopy(list(evidence.evidences.values()))),
                "tool_results": _remove_artifact_paths(copy.deepcopy(evidence.tool_results)),
                "next_source_number": evidence._next_source_number,
            },
        }
        raw = json.dumps(document, ensure_ascii=False, allow_nan=False).encode("utf-8")
        if len(raw) > MAX_SNAPSHOT_BYTES:
            raise ValueError("Session snapshot exceeds 32 MiB.")
        path = self._file(snapshot_id, create=True)
        if path.exists() or path.is_symlink():
            if path_entry_is_symlink_or_reparse(path) or regular_file_has_multiple_links(path):
                raise ValueError("Unsafe session snapshot target.")
            if snapshot_id is None:
                raise ValueError("Snapshot already exists.")
        temp = path.parent / (".tmp-" + uuid.uuid4().hex)
        try:
            with temp.open("xb") as stream:
                os.chmod(temp, 0o600)
                stream.write(raw)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temp, path)
        finally:
            temp.unlink(missing_ok=True)
        return {"id": snapshot_id, "name": document["name"], "saved_at": document["saved_at"]}

    def delete(self, snapshot_id: str) -> None:
        path = self._file(snapshot_id)
        self._read(snapshot_id)
        path.unlink()
        path.parent.rmdir()
