from __future__ import annotations

import os
import stat
from pathlib import Path


def path_entry_is_symlink_or_reparse(path: Path) -> bool:
    """Return whether a path entry is unsafe filesystem indirection.

    Besides symlinks and Windows reparse points, regular Windows files with
    multiple hardlinks are treated as unsafe here. ``DirEntry.stat()`` can
    report an incomplete link count on Windows, while a direct ``Path.stat()``
    performs the metadata lookup needed by the Git metadata scanner.
    """

    try:
        status = os.lstat(path)
    except FileNotFoundError:
        return False

    if stat.S_ISLNK(status.st_mode):
        return True

    attributes = int(getattr(status, "st_file_attributes", 0))
    reparse_flag = int(getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))
    if attributes & reparse_flag:
        return True

    if os.name == "nt" and stat.S_ISREG(status.st_mode):
        try:
            return path.stat().st_nlink > 1
        except OSError:
            # Callers use this helper as a traversal gate. If metadata cannot
            # be inspected reliably, fail closed rather than following it.
            return True

    return False


def regular_file_has_multiple_links(path: Path) -> bool:
    """Return whether an existing regular file has more than one hardlink."""

    status = path.stat()
    return stat.S_ISREG(status.st_mode) and status.st_nlink > 1
