"""Bounded, read-only filesystem operations for deterministic Flow steps.

This module is independent of OKF and never reads file contents.
"""
from __future__ import annotations

import os
import hashlib
import re
import stat
from pathlib import Path, PureWindowsPath
from typing import Any

from cli_agent.security.filesystem_security import path_entry_is_symlink_or_reparse


MAX_INVENTORY_ENTRIES = 10_000


def _add_stable_slugs(result: dict[str, Any]) -> dict[str, Any]:
    """Derive stable, collision-resistant module IDs without model input."""
    seen: set[str] = set()
    for item in result["files"]:
        path = item["path"]
        prefix = re.sub(r"[^a-z0-9]+", "-", path.casefold()).strip("-")[:60].strip("-")
        digest = hashlib.sha256(path.encode("utf-8")).hexdigest()[:16]
        slug = f"{prefix or 'source'}-{digest}"
        if slug in seen:
            raise ValueError("Mehrdeutiger Source-Slug im Inventar.")
        seen.add(slug)
        item["slug"] = slug
    return result



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
    stable_slugs: bool = False,
) -> dict[str, Any]:
    """Return a sorted inventory of regular files and directories.

    The max_entries resource budget counts ALL encountered entries, including
    filtered-out ones. Symlinks/reparse points are rejected, never followed.
    The inventory does not inspect or modify the contents of any file.
    """
    if type(stable_slugs) is not bool:
        raise ValueError("filesystem_operation.stable_slugs muss boolesch sein.")
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
    files: list[dict[str, Any]] = []
    directories: list[dict[str, str]] = []
    examined = 0

    if os.name == "nt":
        from cli_agent.workflows.flow_filesystem_windows import inventory_windows
        result = inventory_windows(
            root, start, suffixes=suffixes, excluded=excluded,
            recursive=recursive, max_entries=max_entries,
        )
        return _add_stable_slugs(result) if stable_slugs else result

    # Open each path component through an already pinned directory descriptor.
    # On platforms without directory-fd/no-follow support, fail closed.
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        # Windows lacks portable openat-style directory descriptors in Python.
        # Fail closed for now rather than silently weakening the security contract.
        raise ValueError("Sichere Verzeichnisinventur benötigt POSIX dir_fd/O_NOFOLLOW.")

    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        # Keep only relative paths on the work stack, never open descriptors.
        # Each directory is reopened through the pinned root using no-follow
        # handles; this bounds descriptor usage independently of tree width
        # and avoids Python recursion limits for deep trees.
        pending = [start.relative_to(root)]
        while pending:
            relative_dir = pending.pop()
            fd = os.dup(root_fd)
            try:
                for part in relative_dir.parts:
                    child_fd = os.open(
                        part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                        dir_fd=fd,
                    )
                    os.close(fd)
                    fd = child_fd
                with os.scandir(fd) as iterator:
                    entries = []
                    for entry in iterator:
                        examined += 1
                        if examined > max_entries:
                            raise ValueError(
                                f"filesystem_operation.inventory überschreitet max_entries={max_entries}."
                            )
                        entries.append(entry.name)
                entries.sort(key=lambda name: (name.casefold(), name))
                for name in entries:
                    relative_path = relative_dir / name
                    rel = relative_path.as_posix()
                    metadata = os.stat(name, dir_fd=fd, follow_symlinks=False)
                    mode = metadata.st_mode
                    if stat.S_ISLNK(mode):
                        raise ValueError(f"Symlink/Reparse Point bei Inventur: {rel}")
                    if not stat.S_ISDIR(mode) and not stat.S_ISREG(mode):
                        raise ValueError(f"Nicht unterstützter Dateityp bei Inventur: {rel}")
                    if name.casefold() in excluded:
                        continue
                    if stat.S_ISDIR(mode):
                        directories.append({"name": name, "path": rel})
                        if recursive:
                            pending.append(relative_path)
                    elif stat.S_ISREG(mode):
                        if suffixes and not rel.casefold().endswith(suffixes):
                            continue
                        files.append({"name": name, "path": rel, "size_bytes": metadata.st_size})
                    else:
                        raise ValueError(f"Nicht unterstützter Dateityp bei Inventur: {rel}")
            finally:
                os.close(fd)
    finally:
        os.close(root_fd)

    files.sort(key=lambda value: (value["path"].casefold(), value["path"]))
    directories.sort(key=lambda value: (value["path"].casefold(), value["path"]))
    result = {
        "directory": start.relative_to(root).as_posix(),
        "files": files,
        "directories": directories,
        "file_count": len(files),
        "directory_count": len(directories),
    }
    return _add_stable_slugs(result) if stable_slugs else result

