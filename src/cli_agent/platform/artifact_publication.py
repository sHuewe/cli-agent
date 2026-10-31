"""Publish only current-session MCP artifacts for authenticated HTTP download."""
from __future__ import annotations

import mimetypes
import re
import uuid
from dataclasses import dataclass
from pathlib import Path

PUBLISH_TOOL_NAME = "agent__publish_artifact"
PUBLISH_TOOL = {
    "type": "function",
    "function": {
        "name": PUBLISH_TOOL_NAME,
        "description": "Offer a previously produced session MCP artifact as a downloadable document. Only paths from tool-result workspace_path are accepted. The published file remains available until the next question or session close.",
        "parameters": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "Verified workspace_path from a prior tool result"},
                "filename": {"type": "string", "description": "Safe suggested download filename"}
            },
            "required": ["path", "filename"],
            "additionalProperties": False
        }
    }
}
_FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")

@dataclass(frozen=True)
class PublishedArtifact:
    path: str
    filename: str
    mime_type: str

def register_artifact(store, path: str, filename: str) -> tuple[str, PublishedArtifact]:
    if not isinstance(filename, str) or not _FILENAME.fullmatch(filename) or filename in {".", ".."}:
        raise ValueError("Ungültiger Download-Dateiname.")
    if not isinstance(path, str):
        raise ValueError("Ungültiger Artefaktpfad.")
    # Verify ownership and inode, never follow client/model-selected arbitrary paths.
    store.read_registered(path)
    item = PublishedArtifact(path=path, filename=filename,
        mime_type=mimetypes.guess_type(filename)[0] or "application/octet-stream")
    return uuid.uuid4().hex, item
