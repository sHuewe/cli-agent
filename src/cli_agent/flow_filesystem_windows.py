"""Windows inventory backend: read-only, bounded and reparse-point rejecting."""
from __future__ import annotations

import ctypes
import os
from ctypes import wintypes
from pathlib import Path
from typing import Any

_REPARSE = 0x400
_DIRECTORY = 0x10
_BACKUP_SEMANTICS = 0x02000000
_OPEN_REPARSE_POINT = 0x00200000


class _FILETIME(ctypes.Structure):
    _fields_ = [("low", wintypes.DWORD), ("high", wintypes.DWORD)]


class _FILEINFO(ctypes.Structure):
    _fields_ = [
        ("attributes", wintypes.DWORD),
        ("creation", _FILETIME), ("access", _FILETIME), ("write", _FILETIME),
        ("volume", wintypes.DWORD), ("size_high", wintypes.DWORD),
        ("size_low", wintypes.DWORD), ("links", wintypes.DWORD),
        ("index_high", wintypes.DWORD), ("index_low", wintypes.DWORD),
    ]


def _handle_info(path: Path) -> tuple[int, int, bool]:
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    create = kernel.CreateFileW
    create.argtypes = [
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    ]
    create.restype = wintypes.HANDLE
    get_info = kernel.GetFileInformationByHandle
    get_info.argtypes = [wintypes.HANDLE, ctypes.POINTER(_FILEINFO)]
    get_info.restype = wintypes.BOOL
    final_path = kernel.GetFinalPathNameByHandleW
    final_path.argtypes = [wintypes.HANDLE, wintypes.LPWSTR, wintypes.DWORD, wintypes.DWORD]
    final_path.restype = wintypes.DWORD
    close = kernel.CloseHandle
    close.argtypes = [wintypes.HANDLE]
    close.restype = wintypes.BOOL
    handle = create(str(path), 0x80, 7, None, 3, _BACKUP_SEMANTICS | _OPEN_REPARSE_POINT, None)
    if handle is None or handle == ctypes.c_void_p(-1).value:
        raise OSError(ctypes.get_last_error(), f"Cannot open inventory entry: {path}")
    try:
        info = _FILEINFO()
        if not get_info(handle, ctypes.byref(info)):
            raise OSError(ctypes.get_last_error(), f"Cannot inspect inventory entry: {path}")
        if info.attributes & _REPARSE:
            raise ValueError(f"Symlink/Reparse Point bei Inventur: {path}")
        length = final_path(handle, None, 0, 0)
        if not length or length > 32768:
            raise ValueError(f"Inventurpfad nicht auflösbar: {path}")
        buffer = ctypes.create_unicode_buffer(length + 1)
        copied = final_path(handle, buffer, len(buffer), 0)
        if not copied or copied >= len(buffer):
            raise ValueError(f"Inventurpfad nicht auflösbar: {path}")
        def normalize(value: str) -> str:
            if value.startswith("\\\\?\\UNC\\"):
                value = "\\\\" + value[8:]
            elif value.startswith("\\\\?\\"):
                value = value[4:]
            return os.path.normcase(os.path.normpath(value))
        if normalize(buffer.value) != normalize(str(path)):
            raise ValueError(f"Inventurpfad wurde umgeleitet: {path}")
        identity = (info.volume << 64) | (info.index_high << 32) | info.index_low
        size = (info.size_high << 32) | info.size_low
        return identity, size, bool(info.attributes & _DIRECTORY)
    finally:
        close(handle)


def inventory_windows(
    root: Path, start: Path, *, recursive: bool,
    suffixes: tuple[str, ...], excluded: set[str], max_entries: int,
) -> dict[str, Any]:
    def checked(path: Path, expected_dir: bool | None = None) -> tuple[int, int, bool]:
        current = root
        _handle_info(root)
        for part in path.relative_to(root).parts[:-1]:
            current = current / part
            if not _handle_info(current)[2]:
                raise ValueError(f"Kein Verzeichnis: {current}")
        result = _handle_info(path)
        if expected_dir is not None and result[2] != expected_dir:
            raise ValueError(f"Dateityp während Inventur geändert: {path}")
        return result

    pending = [start.relative_to(root)]
    files: list[dict[str, Any]] = []
    directories: list[dict[str, str]] = []
    examined = 0
    while pending:
        relative_dir = pending.pop()
        current = root / relative_dir
        identity = checked(current, True)[0]
        with os.scandir(current) as iterator:
            names = []
            for entry in iterator:
                examined += 1
                if examined > max_entries:
                    raise ValueError(
                        f"filesystem_operation.inventory überschreitet max_entries={max_entries}."
                    )
                names.append(entry.name)
        if checked(current, True)[0] != identity:
            raise ValueError(f"Verzeichnis während Inventur geändert: {current}")
        for name in sorted(names, key=lambda n: (n.casefold(), n)):
            relative_path = relative_dir / name
            _, size, is_dir = checked(root / relative_path)
            if name.casefold() in excluded:
                continue
            rel = relative_path.as_posix()
            if is_dir:
                directories.append({"name": name, "path": rel})
                if recursive:
                    pending.append(relative_path)
            elif not suffixes or rel.casefold().endswith(suffixes):
                files.append({"name": name, "path": rel, "size_bytes": size})
        if checked(current, True)[0] != identity:
            raise ValueError(f"Verzeichnis während Inventur geändert: {current}")
    files.sort(key=lambda v: (v["path"].casefold(), v["path"]))
    directories.sort(key=lambda v: (v["path"].casefold(), v["path"]))
    return {
        "directory": start.relative_to(root).as_posix(), "files": files,
        "directories": directories, "file_count": len(files),
        "directory_count": len(directories),
    }
