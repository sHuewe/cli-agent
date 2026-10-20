"""Bounded, read-only filesystem operations for deterministic Flow steps.

This module is independent of OKF and never reads file contents.
"""
from __future__ import annotations

import os
from pathlib import Path, PureWindowsPath
from typing import Any

from .filesystem_security import path_entry_is_symlink_or_reparse


MAX_INVENTORY_ENTRIES = 10_000


def _workspace_directory(workspace: Path, directory: str) -> Path:
    if not isinstance(directory, str) or not directory.strip():
        raise ValueError("filesystem_operation.directory muss ein Verzeichnispfad sein.")
    raw = Path(directory)
    windows = PureWindowsPath(directory)
    if raw.is_absolute() or windows.is_absolute() or windows.drive:
        raise ValueError("filesystem_operation.directory muss relativ zum Workspace sein.")
    if ".." in raw.parts or ".." in windows.parts:
        raise ValueError("filesystem_operation.directory darf '..' nicht enthalten.")

    root = workspace.resolve(strict=True)
    target = root
    # Reject every intermediate symlink/reparse point, not only the final path.
    for component in raw.parts:
        if component in ("", "."):
            continue
        target = target / component
        if path_entry_is_symlink_or_reparse(target):
            raise ValueError(
                f"filesystem_operation.directory darf keinen Symlink/Reparse Point enthalten: {target}"
            )
    resolved = target.resolve(strict=True)
    try:
        resolved.relative_to(root)
    except ValueError as exc:
        raise ValueError("filesystem_operation.directory verlässt den Workspace.") from exc
    if not resolved.is_dir():
        raise ValueError(f"filesystem_operation.directory ist kein Ordner: {directory}")
    return resolved


def inventory_directory(
    workspace: Path,
    directory: str,
    *,
    recursive: bool = False,
    extensions: tuple[str, ...] = (),
    exclude_names: tuple[str, ...] = (),
    max_entries: int = MAX_INVENTORY_ENTRIES,
) -> dict[str, Any]:
    """Return a sorted inventory of regular files and directories.

    The max_entries resource budget counts ALL encountered entries, including
    filtered-out ones. Symlinks/reparse points are rejected, never followed.
    The inventory does not inspect or modify the contents of any file.
    """
    if type(recursive) is not bool:
        raise ValueError("filesystem_operation.recursive muss boolesch sein.")
    if type(max_entries) is not int or not 1 <= max_entries <= MAX_INVENTORY_ENTRIES:
        raise ValueError(
            f"filesystem_operation.max_entries muss zwischen 1 und {MAX_INVENTORY_ENTRIES} liegen."
        )
    if any(
        not isinstance(ext, str)
        or len(ext) < 2
        or ext[0] != "."
        or "/" in ext
        or "\\" in ext
        for ext in extensions
    ):
        raise ValueError("filesystem_operation.extensions enthält ungültige Endungen.")
    if any(
        not isinstance(name, str)
        or not name
        or name in {".", ".."}
        or "/" in name
        or "\\" in name
        for name in exclude_names
    ):
        raise ValueError("filesystem_operation.exclude_names enthält ungültige Namen.")
    suffixes = tuple(ext.casefold() for ext in extensions)
    excluded = {name.casefold() for name in exclude_names}
    root = workspace.resolve(strict=True)
    start = _workspace_directory(root, directory)
    pending = [start]
    files: list[dict[str, Any]] = []
    directories: list[dict[str, str]] = []
    examined = 0

    while pending:
        current = pending.pop()
        # Evaluate each scanned directory again before entering it, to reject
        # an entry swapped for a symlink after its original enumeration.
        if path_entry_is_symlink_or_reparse(current):
            raise ValueError(f"Symlink/Reparse Point bei Inventur: {current}")
        with os.scandir(current) as iterator:
            entries = sorted(iterator, key=lambda entry: (entry.name.casefold(), entry.name))
        for entry in entries:
            examined += 1
            if examined > max_entries:
                raise ValueError(
                    f"filesystem_operation.inventory überschreitet max_entries={max_entries}."
                )
            path = Path(entry.path)
            if path_entry_is_symlink_or_reparse(path):
                raise ValueError(f"Symlink/Reparse Point bei Inventur: {path}")
            # Defend against path-indirection and races without following links.
            relative = path.relative_to(root).as_posix()
            if entry.name.casefold() in excluded:
                continue
            if entry.is_dir(follow_symlinks=False):
                directories.append({"name": entry.name, "path": relative})
                if recursive:
                    pending.append(path)
            elif entry.is_file(follow_symlinks=False):
                if suffixes and not relative.casefold().endswith(suffixes):
                    continue
                size = entry.stat(follow_symlinks=False).st_size
                files.append({
                    "name": entry.name,
                    "path": relative,
                    "size_bytes": size,
                })
            else:
                raise ValueError(f"Nicht unterstützter Dateityp bei Inventur: {relative}")

    files.sort(key=lambda value: (value["path"].casefold(), value["path"]))
    directories.sort(key=lambda value: (value["path"].casefold(), value["path"]))
    return {
        "directory": start.relative_to(root).as_posix(),
        "files": files,
        "directories": directories,
        "file_count": len(files),
        "directory_count": len(directories),
    }
