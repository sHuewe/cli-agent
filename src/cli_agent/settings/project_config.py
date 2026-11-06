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


def merge_project_overrides(
    global_values: dict, project_values: dict, *, _path: tuple[str, ...] = (),
) -> dict:
    """Deep-merge tables; button visibility/labels also merge across representations.

    A project-only label preserves an inherited boolean visibility value,
    and a project-only boolean preserves an inherited custom label.
    Unrelated scalar and array values continue to replace their base values.
    """
    merged = dict(global_values)
    for key, value in project_values.items():
        previous = merged.get(key)
        if _path in {("ui", "buttons"), ("ui", "buttons", "commands")} and key != "commands":
            if isinstance(previous, bool) and isinstance(value, dict):
                previous = {"visible": previous}
            elif isinstance(previous, dict) and isinstance(value, bool):
                value = {"visible": value}
        if isinstance(value, dict) and isinstance(previous, dict):
            merged[key] = merge_project_overrides(previous, value, _path=(*_path, key))
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


_UI_STYLESHEET_RE = __import__("re").compile(r"ui/[a-zA-Z0-9_-]+\.css\Z")
MAX_UI_STYLESHEET_BYTES = 128 * 1024
MAX_UI_STYLESHEETS_TOTAL_BYTES = 512 * 1024


def validate_project_ui_stylesheet_name(filename: str) -> None:
    """Allow CSS files directly within .cli-agent/ui, with no traversal."""
    if not isinstance(filename, str) or not _UI_STYLESHEET_RE.fullmatch(filename):
        raise ValueError("ui.stylesheets entries must look like 'ui/custom.css'.")


def read_project_ui_stylesheets(workspace: Path, filenames: tuple[str, ...]) -> dict[str, str]:
    """Load bounded CSS snapshots via the host, not through workspace MCP."""
    if not filenames:
        return {}
    if len(filenames) > 8 or len(set(filenames)) != len(filenames):
        raise ValueError("ui.stylesheets must contain at most 8 distinct CSS files.")
    folder = _project_directory(workspace) / "ui"
    if path_entry_is_symlink_or_reparse(folder) or not folder.is_dir():
        raise ValueError(".cli-agent/ui must be a real directory.")
    result: dict[str, str] = {}
    total_bytes = 0
    for filename in filenames:
        validate_project_ui_stylesheet_name(filename)
        name = filename.removeprefix("ui/")
        data = _read_private_file(folder / name, limit=MAX_UI_STYLESHEET_BYTES)
        total_bytes += len(data)
        if total_bytes > MAX_UI_STYLESHEETS_TOTAL_BYTES:
            raise ValueError("Project UI stylesheets exceed the total size limit.")
        try:
            result[name] = data.decode("utf-8-sig")
        except UnicodeError as exc:
            raise ValueError("Project UI stylesheets must use UTF-8.") from exc
    return result


_UI_IMAGE_RE = __import__("re").compile(r"ui/[a-zA-Z0-9_-]+\.(?:png|jpg|jpeg|webp)\Z")
MAX_UI_ICON_BYTES = 1024 * 1024


def validate_project_ui_image_name(filename: str) -> None:
    """Allow only local raster images directly beneath .cli-agent/ui."""
    if not isinstance(filename, str) or not _UI_IMAGE_RE.fullmatch(filename):
        raise ValueError("ui.icon must name a PNG, JPEG or WebP file like 'ui/logo.png'.")


def read_project_ui_icon(workspace: Path, filename: str | None) -> tuple[str, bytes, str] | None:
    """Read the configured image once via the private host reader.

    Do not serve files by user-supplied path or expose them through OS MCP.
    """
    if filename is None or not filename.startswith("ui/"):
        return None
    validate_project_ui_image_name(filename)
    folder = _project_directory(workspace) / "ui"
    if path_entry_is_symlink_or_reparse(folder) or not folder.is_dir():
        raise ValueError(".cli-agent/ui must be a real directory.")
    name = filename.removeprefix("ui/")
    data = _read_private_file(folder / name, limit=MAX_UI_ICON_BYTES)
    lower = name.rsplit(".", 1)[-1]
    if lower == "png":
        valid = (
            len(data) >= 33
            and data.startswith(b"\x89PNG\r\n\x1a\n")
            and data[12:16] == b"IHDR"
            and int.from_bytes(data[8:12], "big") == 13
            and 0 < int.from_bytes(data[16:20], "big") <= 4096
            and 0 < int.from_bytes(data[20:24], "big") <= 4096
        )
        mime_type = "image/png"
    elif lower in {"jpg", "jpeg"}:
        valid = len(data) >= 16 and data.startswith(b"\xff\xd8\xff") and data.endswith(b"\xff\xd9")
        mime_type = "image/jpeg"
    else:
        valid = (
            len(data) >= 20
            and data.startswith(b"RIFF")
            and data[8:12] == b"WEBP"
            and data[12:16] in {b"VP8 ", b"VP8L", b"VP8X"}
            and int.from_bytes(data[4:8], "little") + 8 == len(data)
        )
        mime_type = "image/webp"
    if not valid:
        raise ValueError("ui.icon image contents do not match the configured file format.")
    return name, data, mime_type
