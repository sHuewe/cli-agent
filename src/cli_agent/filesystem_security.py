from __future__ import annotations

import os
import stat
from contextlib import ExitStack
from pathlib import PureWindowsPath
from pathlib import Path


def path_entry_is_symlink_or_reparse(path: Path) -> bool:
    """Return whether the directory entry is a symlink or Windows reparse point."""

    try:
        status = os.lstat(path)
    except FileNotFoundError:
        return False

    if stat.S_ISLNK(status.st_mode):
        return True

    attributes = int(getattr(status, "st_file_attributes", 0))
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    return bool(attributes & reparse_flag)


def regular_file_has_multiple_links(path: Path) -> bool:
    """Return whether an existing regular file has more than one hardlink."""

    status = path.stat()
    return stat.S_ISREG(status.st_mode) and status.st_nlink > 1




def _windows_open_pinned_file(root: Path, relative_parts: tuple[str, ...], stack: ExitStack) -> int:
    """Pin each ancestor against replacement and refuse Windows reparse points.

    Unlike stat-then-open, parent directory handles deny FILE_SHARE_WRITE
    and FILE_SHARE_DELETE for the entire verified traversal. A competing
    reparse-point writer therefore cannot modify pinned ancestors, while
    CreateFileW opens each component with OPEN_REPARSE_POINT. This protects
    against both renames and in-place junction/reparse mutations.
    The returned CRT fd owns the final Win32 HANDLE.
    """
    import ctypes
    import msvcrt
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    create_file = kernel32.CreateFileW
    create_file.argtypes = (
        wintypes.LPCWSTR, wintypes.DWORD, wintypes.DWORD, ctypes.c_void_p,
        wintypes.DWORD, wintypes.DWORD, wintypes.HANDLE,
    )
    create_file.restype = wintypes.HANDLE
    close_handle = kernel32.CloseHandle
    close_handle.argtypes = (wintypes.HANDLE,)
    close_handle.restype = wintypes.BOOL
    attributes_for_handle = kernel32.GetFileInformationByHandleEx
    attributes_for_handle.argtypes = (
        wintypes.HANDLE, wintypes.INT, ctypes.c_void_p, wintypes.DWORD,
    )
    attributes_for_handle.restype = wintypes.BOOL

    class FileAttributeTagInfo(ctypes.Structure):
        _fields_ = (
            ("FileAttributes", wintypes.DWORD),
            ("ReparseTag", wintypes.DWORD),
        )

    file_read_attributes = 0x0080
    generic_read = 0x80000000
    file_share_read = 0x00000001
    open_existing = 3
    file_flag_backup_semantics = 0x02000000
    file_flag_open_reparse_point = 0x00200000
    file_attribute_directory = 0x00000010
    file_attribute_reparse_point = 0x00000400
    file_attribute_tag_info = 9
    invalid_handle = ctypes.c_void_p(-1).value

    root_path = PureWindowsPath(str(root.absolute()))
    if not root_path.is_absolute() or not root_path.anchor:
        raise ValueError("OKF-Root muss ein absoluter Windows-Pfad sein.")
    # Pin from the volume/share root, not just the configured OKF root.
    # Otherwise a junction in an ancestor of that root could be swapped too.
    components = (*root_path.parts[1:], *relative_parts)
    current = root_path.anchor

    def open_component(path: str, *, directory: bool) -> int:
        handle = create_file(
            path,
            file_read_attributes if directory else generic_read,
            # Do not allow concurrently opened write-capable handles on a
            # pinned parent directory: FSCTL_SET_REPARSE_POINT can otherwise
            # retarget it without renaming it.
            file_share_read,
            None,
            open_existing,
            file_flag_open_reparse_point
            | (file_flag_backup_semantics if directory else 0),
            None,
        )
        if handle == invalid_handle or handle is None:
            raise ctypes.WinError(ctypes.get_last_error())
        try:
            info = FileAttributeTagInfo()
            if not attributes_for_handle(
                handle, file_attribute_tag_info, ctypes.byref(info),
                ctypes.sizeof(info),
            ):
                raise ctypes.WinError(ctypes.get_last_error())
            if info.FileAttributes & file_attribute_reparse_point:
                raise ValueError("OKF-Dateipfad enthält einen Windows-Reparse-Point.")
            is_dir = bool(info.FileAttributes & file_attribute_directory)
            if is_dir != directory:
                raise ValueError("OKF-Dateipfad hat einen ungültigen Dateityp.")
        except Exception:
            close_handle(handle)
            raise
        return handle

    anchor_handle = open_component(current, directory=True)
    stack.callback(close_handle, anchor_handle)
    for component in components[:-1]:
        current = str(PureWindowsPath(current) / component)
        directory_handle = open_component(current, directory=True)
        stack.callback(close_handle, directory_handle)
    current = str(PureWindowsPath(current) / components[-1])
    final_handle = open_component(current, directory=False)
    try:
        return msvcrt.open_osfhandle(
            final_handle, os.O_RDONLY | getattr(os, "O_BINARY", 0)
        )
    except Exception:
        close_handle(final_handle)
        raise


def read_bounded_regular_file(
    root: Path,
    relative: str,
    *,
    max_bytes: int,
    expected_identity: tuple[int, int] | None = None,
) -> bytes:
    """Read a workspace-relative regular file via a bounded verified descriptor.

    POSIX opens every directory component relative to an already-open parent
    with O_NOFOLLOW; the final O_NONBLOCK open avoids hanging on a substituted
    FIFO. Validation is performed on the opened file descriptor, not just on
    a separately checked pathname. On Windows, reject reparse components and
    compare the opened descriptor's identity with the path after opening.
    """
    if (
        not isinstance(relative, str)
        or not relative
        or Path(relative).is_absolute()
        or PureWindowsPath(relative).is_absolute()
        or PureWindowsPath(relative).drive
        or any(part in {".", "..", ""} for part in Path(relative).parts)
        or max_bytes < 1
    ):
        raise ValueError("Ungültiger sicherer Dateipfad oder Leselimit.")
    parts = Path(relative).parts
    read_flags = os.O_RDONLY | getattr(os, "O_BINARY", 0)
    with ExitStack() as stack:
        if os.name == "posix":
            directory_flags = read_flags | os.O_DIRECTORY | os.O_NOFOLLOW
            parent_fd = os.open(root, directory_flags)
            stack.callback(os.close, parent_fd)
            for part in parts[:-1]:
                child_fd = os.open(part, directory_flags, dir_fd=parent_fd)
                stack.callback(os.close, child_fd)
                parent_fd = child_fd
            file_fd = os.open(
                parts[-1], read_flags | os.O_NOFOLLOW | os.O_NONBLOCK,
                dir_fd=parent_fd,
            )
        elif os.name == "nt":
            file_fd = _windows_open_pinned_file(root, parts, stack)
        else:
            raise OSError("Sicheres Öffnen ist auf diesem Betriebssystem nicht verfügbar.")
        stack.callback(os.close, file_fd)
        opened = os.fstat(file_fd)
        if not stat.S_ISREG(opened.st_mode):
            raise ValueError("Datei ist keine reguläre Datei.")
        if opened.st_nlink != 1:
            raise ValueError("Dateien mit mehreren Hardlinks werden nicht gelesen.")
        if expected_identity is not None and (
            opened.st_dev, opened.st_ino
        ) != expected_identity:
            raise ValueError("Die Datei wurde während des Öffnens ausgetauscht.")
        if opened.st_size > max_bytes:
            raise ValueError(f"Datei ist mit {opened.st_size} Bytes größer als das konfigurierte Leselimit von {max_bytes} Bytes.")
        # Windows ancestors and the final file are pinned in the same stack;
        # checking the pathname again would reintroduce a race.

        # Bound total bytes even when file size grows between fstat and read.
        chunks: list[bytes] = []
        remaining = max_bytes + 1
        while remaining > 0:
            block = os.read(file_fd, min(65536, remaining))
            if not block:
                break
            chunks.append(block)
            remaining -= len(block)
        if remaining == 0:
            raise ValueError("Datei ist größer als das konfigurierte Leselimit.")
        return b"".join(chunks)
