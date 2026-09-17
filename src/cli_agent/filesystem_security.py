from __future__ import annotations

import os
import stat
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
