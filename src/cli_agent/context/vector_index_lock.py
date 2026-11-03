"""Cross-process advisory lock for publishing and editing SQLite vector indexes.

A stable sidecar lock file (never deleted) synchronizes SDK record writes and
index rebuilds, including the atomic replacement of the SQLite database.
"""
from __future__ import annotations

import asyncio
import errno
import os
from contextlib import asynccontextmanager
from pathlib import Path
from typing import AsyncIterator

if os.name == "nt":
    import msvcrt
else:
    import fcntl


@asynccontextmanager
async def vector_index_lock(database: Path) -> AsyncIterator[None]:
    """Exclusive per-database lock, including waits across embedding awaits.

    Uses nonblocking OS-level locks rather than a process-local asyncio.Lock,
    so separate SDK instances/processes participate. The sidecar is deliberately
    not unlinked: deleting it could cause concurrent waiters to lock different
    inodes. OS locks are released even if the process exits unexpectedly.
    """
    database = Path(database).resolve()
    database.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(str(database) + ".lock", os.O_CREAT | os.O_RDWR, 0o600)
    acquired = False
    try:
        if os.name == "nt" and os.fstat(fd).st_size == 0:
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, b"\0")
        while True:
            try:
                if os.name == "nt":
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError as exc:
                if exc.errno not in {errno.EACCES, errno.EAGAIN}:
                    raise
                await asyncio.sleep(0.05)
            else:
                acquired = True
                break
        yield
    finally:
        try:
            if acquired:
                if os.name == "nt":
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
                else:
                    fcntl.flock(fd, fcntl.LOCK_UN)
        finally:
            os.close(fd)
