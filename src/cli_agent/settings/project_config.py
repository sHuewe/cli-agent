"""Workspace-local project configuration and private system-prompt files.

The project configuration is loaded by the host, never through workspace MCP
tools. Files directly under .cli-agent are protected by the Workspace policy;
existing explicitly permitted Data MCP subdirectories remain accessible.
"""
from __future__ import annotations

import stat
import tomllib
from pathlib import Path

from cli_agent.security.filesystem_security import (
    path_entry_is_symlink_or_reparse,
    regular_file_has_multiple_links,
)

PROJECT_DIR = ".cli-agent"
PROJECT_CONFIG_NAME = "project.toml"
MAX_PRIVATE_CONFIG_BYTES = 256 * 1024
MAX_SYSTEM_PROMPT_BYTES = 256 * 1024


def _project_directory(workspace: Path) -> Path:
    root = workspace.expanduser().resolve(strict=True)
    if not root.is_dir():
        raise ValueError("Workspace must be an existing directory.")
    folder = root / PROJECT_DIR
    if path_entry_is_symlink_or_reparse(folder):
        raise ValueError("The .cli-agent project directory must not be a symlink or junction.")
    return folder


def _read_private_file(path: Path, *, limit: int) -> bytes:
    if path_entry_is_symlink_or_reparse(path):
        raise ValueError(f"Project file must not be a symlink or junction: {path.name}")
    try:
        details = path.stat()
    except FileNotFoundError as exc:
        raise FileNotFoundError(f"Project file not found: {path}") from exc
    if not stat.S_ISREG(details.st_mode):
        raise ValueError(f"Project file must be a regular file: {path.name}")
    if regular_file_has_multiple_links(path):
        raise ValueError(f"Project file must not have multiple hardlinks: {path.name}")
    if details.st_size > limit:
        raise ValueError(f"Project file exceeds the {limit}-byte limit: {path.name}")
    data = path.read_bytes()
    if len(data) > limit:
        raise ValueError(f"Project file exceeds the {limit}-byte limit: {path.name}")
    return data


def read_project_overrides(workspace: Path) -> dict:
    """Load the optional partial project.toml; do not silently ignore errors."""
    folder = _project_directory(workspace)
    path = folder / PROJECT_CONFIG_NAME
    if not path.exists() and not path.is_symlink():
        return {}
    try:
        values = tomllib.loads(_read_private_file(path, limit=MAX_PRIVATE_CONFIG_BYTES).decode("utf-8"))
    except UnicodeError as exc:
        raise ValueError("Project configuration must be UTF-8.") from exc
    if not isinstance(values, dict):
        raise ValueError("Project configuration must be a TOML table.")
    return values


def merge_project_overrides(global_values: dict, project_values: dict) -> dict:
    """Deep-merge tables; explicit scalar values and arrays replace the base."""
    merged = dict(global_values)
    for key, value in project_values.items():
        previous = merged.get(key)
        if isinstance(value, dict) and isinstance(previous, dict):
            merged[key] = merge_project_overrides(previous, value)
        else:
            merged[key] = value
    return merged


def read_project_system_prompt(workspace: Path, filename: str | None) -> str | None:
    """Read a named direct child of .cli-agent without exposing it to MCP tools."""
    if filename is None:
        return None
    if (
        not isinstance(filename, str)
        or not filename.strip()
        or filename != filename.strip()
        or filename in {".", ".."}
        or "/" in filename
        or "\\" in filename
        or ":" in filename
        or "\x00" in filename
    ):
        raise ValueError("agent.system_prompt must be a filename directly inside .cli-agent.")
    folder = _project_directory(workspace)
    data = _read_private_file(folder / filename, limit=MAX_SYSTEM_PROMPT_BYTES)
    try:
        content = data.decode("utf-8-sig")
    except UnicodeError as exc:
        raise ValueError("The system prompt must be a UTF-8 text file.") from exc
    if not content.strip():
        raise ValueError("The system prompt must not be empty.")
    return content


_UI_TEMPLATE_RE = __import__("re").compile(r"ui/[a-zA-Z0-9_-]+\.html\Z")
MAX_UI_TEMPLATE_BYTES = 128 * 1024


def validate_project_ui_template_name(filename: str) -> None:
    """Allow one HTML file immediately beneath the project's UI folder."""
    if not isinstance(filename, str) or not _UI_TEMPLATE_RE.fullmatch(filename):
        raise ValueError("ui.template must name a file such as 'ui/custom.html'.")


def read_project_ui_template(workspace: Path, filename: str | None) -> str | None:
    """Read a project UI template without traversing links or arbitrary paths."""
    if filename is None:
        return None
    validate_project_ui_template_name(filename)
    ui_dir = _project_directory(workspace) / "ui"
    if path_entry_is_symlink_or_reparse(ui_dir) or not ui_dir.is_dir():
        raise ValueError(".cli-agent/ui must be a real directory.")
    data = _read_private_file(ui_dir / filename.removeprefix("ui/"), limit=MAX_UI_TEMPLATE_BYTES)
    try:
        content = data.decode("utf-8-sig")
    except UnicodeError as exc:
        raise ValueError("UI templates must use UTF-8.") from exc
    if not content.strip():
        raise ValueError("UI templates must not be empty.")
    return content
