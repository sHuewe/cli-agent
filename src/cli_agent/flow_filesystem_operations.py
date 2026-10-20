"""Bounded, read-only filesystem operations for deterministic Flow steps.

This module is independent of OKF and never reads file contents.
"""
from __future__ import annotations

import os
import stat
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
    files: list[dict[str, Any]] = []
    directories: list[dict[str, str]] = []
    examined = 0

    # Open each path component through an already pinned directory descriptor.
    # On platforms without directory-fd/no-follow support, fail closed.
    if os.name != "posix" or not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY"):
        # Windows lacks portable openat-style directory descriptors in Python.
        # Fail closed for now rather than silently weakening the security contract.
        raise ValueError("Sichere Verzeichnisinventur benötigt POSIX dir_fd/O_NOFOLLOW.")

    root_fd = os.open(root, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        start_fd = os.dup(root_fd)
        try:
            for part in start.relative_to(root).parts:
                next_fd = os.open(
                    part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                    dir_fd=start_fd,
                )
                os.close(start_fd)
                start_fd = next_fd
            def scan(fd: int, relative_dir: Path) -> None:
                nonlocal examined
                try:
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
                        if name.casefold() in excluded:
                            continue
                        relative_path = relative_dir / name
                        rel = relative_path.as_posix()
                        metadata = os.stat(name, dir_fd=fd, follow_symlinks=False)
                        mode = metadata.st_mode
                        if stat.S_ISLNK(mode):
                            raise ValueError(f"Symlink/Reparse Point bei Inventur: {rel}")
                        if stat.S_ISDIR(mode):
                            directories.append({"name": name, "path": rel})
                            if recursive:
                                child_fd = os.open(
                                    name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW,
                                    dir_fd=fd,
                                )
                                scan(child_fd, relative_path)
                        elif stat.S_ISREG(mode):
                            if suffixes and not rel.casefold().endswith(suffixes):
                                continue
                            files.append({"name": name, "path": rel, "size_bytes": metadata.st_size})
                        else:
                            raise ValueError(f"Nicht unterstützter Dateityp bei Inventur: {rel}")
                finally:
                    os.close(fd)

            scan(start_fd, start.relative_to(root))
            start_fd = -1
        finally:
            if start_fd >= 0:
                os.close(start_fd)
    finally:
        os.close(root_fd)

    files.sort(key=lambda value: (value["path"].casefold(), value["path"]))
    directories.sort(key=lambda value: (value["path"].casefold(), value["path"]))
    return {
        "directory": start.relative_to(root).as_posix(),
        "files": files,
        "directories": directories,
        "file_count": len(files),
        "directory_count": len(directories),
    }
