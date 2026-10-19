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
        else:
            current = root
            for part in parts:
                current = current / part
                if path_entry_is_symlink_or_reparse(current):
                    raise ValueError("Dateipfad enthält einen Link oder Reparse-Point.")
            file_fd = os.open(root.joinpath(*parts), read_flags | getattr(os, "O_NOINHERIT", 0))
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
        if os.name != "posix":
            target = root.joinpath(*parts)
            if path_entry_is_symlink_or_reparse(target):
                raise ValueError("Dateipfad ist ein Reparse-Point.")
            current = target.stat()
            if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
                raise ValueError("Die Datei wurde während des Öffnens ausgetauscht.")

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
