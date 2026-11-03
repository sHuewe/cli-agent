"""Cross-process advisory lock for publishing and reading SQLite vector indexes.

The stable sidecar lock file coordinates cooperative readers and writers.
For read-only deployments, search remains available without the sidecar.
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
async def vector_index_lock(
    database: Path, *, shared: bool = False,
) -> AsyncIterator[None]:
    """Hold a cross-process index lock while operating on the SQLite file.

    Writers always take an exclusive lock for the complete rebuild/publication
    or inline upsert. Readers acquire a shared lock when the existing sidecar
    is readable, without needing write permissions to the directory or file.

    Read-only indexes may be deployed without a sidecar, or a sidecar may not
    be readable by a different user. In that case readers rely on SQLite's
    read-only snapshot behavior and never create a lockfile. On Windows, a
    concurrent publisher may then fail to replace an open database, but cannot
    silently corrupt it. The sidecar is never unlinked by this helper.
    """
    database = Path(database).resolve()
    lock_path = str(database) + ".lock"
    if not shared:
        database.parent.mkdir(parents=True, exist_ok=True)
    try:
        fd = os.open(
            lock_path,
            (os.O_RDWR if os.name == "nt" else os.O_RDONLY) if shared
            else os.O_CREAT | os.O_RDWR,
            0o600,
        )
    except OSError as exc:
        if shared and exc.errno in {
            errno.EACCES, errno.EPERM, errno.ENOENT, errno.EROFS,
        }:
            # A read-only deployment must not depend on permission to
            # create/read the advisory lockfile.
            yield
            return
        raise

    acquired = False
    try:
        while True:
            try:
                if os.name == "nt":
                    os.lseek(fd, 0, os.SEEK_SET)
                    msvcrt.locking(
                        fd, msvcrt.LK_NBRLCK if shared else msvcrt.LK_NBLCK, 1,
                    )
                else:
                    fcntl.flock(
                        fd, (fcntl.LOCK_SH if shared else fcntl.LOCK_EX)
                        | fcntl.LOCK_NB,
                    )
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
