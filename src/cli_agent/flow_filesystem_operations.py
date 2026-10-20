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

    if os.name == "nt":
        return _inventory_windows(
            root, start, suffixes=suffixes, excluded=excluded,
            recursive=recursive, max_entries=max_entries,
        )

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
    return {
        "directory": start.relative_to(root).as_posix(),
        "files": files,
        "directories": directories,
        "file_count": len(files),
        "directory_count": len(directories),
    }


def _inventory_windows(
    root: Path,
    start: Path,
    *,
    suffixes: tuple[str, ...],
    excluded: set[str],
    recursive: bool,
    max_entries: int,
) -> dict[str, Any]:
    """Windows inventory with fail-closed reparse checks and pinned handle verification.

    Every directory and file is checked using a Win32 handle opened with
    FILE_FLAG_OPEN_REPARSE_POINT. The final path is checked against the root,
    and directories are checked again after enumeration. Windows Python
    scandir is path-based, so this is not an atomic openat-style traversal;
    callers requiring protection against concurrent hostile filesystem
    mutation must use the POSIX implementation.
    """
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel32.CreateFileW
    create.argtypes = (
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, wintypes.LPVOID,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    )
    create.restype = wintypes.HANDLE
    attributes = kernel32.GetFileInformationByHandle
    attributes.argtypes = (wintypes.HANDLE, wintypes.LPVOID)
    attributes.restype = wintypes.BOOL
    final_path = kernel32.GetFinalPathNameByHandleW
    final_path.argtypes = (wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD)
    final_path.restype = wintypes.DWORD
    close = kernel32.CloseHandle
    close.argtypes = (wintypes.HANDLE,)
    close.restype = wintypes.BOOL

    class FileTime(ctypes.Structure):
        _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]

    class FileInfo(ctypes.Structure):
        _fields_ = [
            ("attributes", wintypes.DWORD),
            ("creation", FileTime), ("access", FileTime), ("write", FileTime),
            ("volume", wintypes.DWORD), ("size_high", wintypes.DWORD),
            ("size_low", wintypes.DWORD), ("links", wintypes.DWORD),
            ("index_high", wintypes.DWORD), ("index_low", wintypes.DWORD),
        ]

    def checked(path: Path, want_directory: bool | None) -> tuple[int, int, int]:
        handle = create(
            str(path), 0x80, 0x7, None, 3,
            0x02000000 | 0x00200000, None,  # BACKUP_SEMANTICS | OPEN_REPARSE_POINT
        )
        if handle == wintypes.HANDLE(-1).value:
            raise OSError(ctypes.get_last_error(), f"Cannot inspect inventory entry: {path}")
        try:
            info = FileInfo()
            if not attributes(handle, ctypes.byref(info)):
                raise OSError(ctypes.get_last_error(), f"Cannot inspect inventory entry: {path}")
            if info.attributes & 0x400:  # FILE_ATTRIBUTE_REPARSE_POINT
                raise ValueError(f"Symlink/Reparse Point bei Inventur: {path}")
            is_dir = bool(info.attributes & 0x10)
            if want_directory is not None and is_dir != want_directory:
                raise ValueError(f"Dateityp während Inventur geändert: {path}")
            if info.attributes & 0x40:  # DEVICE
                raise ValueError(f"Nicht unterstützter Dateityp bei Inventur: {path}")
            length = final_path(handle, None, 0, 0)
            if not length:
                raise OSError(ctypes.get_last_error(), f"Cannot resolve inventory entry: {path}")
            buffer = ctypes.create_unicode_buffer(length + 1)
            if not final_path(handle, buffer, len(buffer), 0):
                raise OSError(ctypes.get_last_error(), f"Cannot resolve inventory entry: {path}")
            actual = buffer.value
            if actual.startswith("\\\\?\\"):
                actual = actual[4:]
            try:
                Path(actual).relative_to(root)
            except ValueError as exc:
                raise ValueError(f"Inventurpfad verlässt Workspace: {path}") from exc
            return (
                (info.volume << 64) | (info.index_high << 32) | info.index_low,
                (info.size_high << 32) | info.size_low,
                info.attributes,
            )
        finally:
            close(handle)

    # Revalidate every ancestor on each visit, not only the leaf entry.
    def check_chain(path: Path, is_directory: bool | None) -> tuple[int, int, int]:
        relative = path.relative_to(root)
        current = root
        checked(current, True)
        for part in relative.parts[:-1]:
            current = current / part
            checked(current, True)
        return checked(path, is_directory)

    files: list[dict[str, Any]] = []
    directories: list[dict[str, str]] = []
    examined = 0
    pending = [start.relative_to(root)]
    while pending:
        rel_dir = pending.pop()
        current_dir = root / rel_dir
        before = check_chain(current_dir, True)[0]
        with os.scandir(current_dir) as iterator:
            names = []
            for entry in iterator:
                examined += 1
                if examined > max_entries:
                    raise ValueError(
                        f"filesystem_operation.inventory überschreitet max_entries={max_entries}."
                    )
                names.append(entry.name)
        names.sort(key=lambda name: (name.casefold(), name))
        for name in names:
            rel_path = rel_dir / name
            path = root / rel_path
            _, size, flags = check_chain(path, None)
            is_dir = bool(flags & 0x10)
            if name.casefold() in excluded:
                continue
            if is_dir:
                directories.append({"name": name, "path": rel_path.as_posix()})
                if recursive:
                    pending.append(rel_path)
            elif not suffixes or name.casefold().endswith(suffixes):
                files.append({
                    "name": name, "path": rel_path.as_posix(), "size_bytes": size,
                })
        if check_chain(current_dir, True)[0] != before:
            raise ValueError(f"Verzeichnis während Inventur geändert: {current_dir}")

    files.sort(key=lambda value: (value["path"].casefold(), value["path"]))
    directories.sort(key=lambda value: (value["path"].casefold(), value["path"]))
    return {
        "directory": start.relative_to(root).as_posix(),
        "files": files,
        "directories": directories,
        "file_count": len(files),
        "directory_count": len(directories),
    }
