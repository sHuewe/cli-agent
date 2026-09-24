from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import replace
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import BinaryIO

from .config import LoggingConfig


class _FileLock:
    """Small cross-platform advisory file lock used for log-file coordination."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._handle: BinaryIO | None = None

    def acquire(
        self,
        *,
        wait: bool = False,
        timeout_seconds: float | None = None,
    ) -> bool:
        if self._handle is not None:
            return True

        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = self.path.open("a+b")
        deadline = (
            time.monotonic() + timeout_seconds
            if timeout_seconds is not None
            else None
        )

        if os.name == "nt":
            import msvcrt

            handle.seek(0, os.SEEK_END)
            if handle.tell() == 0:
                handle.write(b"\0")
                handle.flush()

        while True:
            try:
                if os.name == "nt":
                    import msvcrt

                    handle.seek(0)
                    msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
                else:
                    import fcntl

                    fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                if not wait:
                    handle.close()
                    return False
                if deadline is not None and time.monotonic() >= deadline:
                    handle.close()
                    return False
                time.sleep(0.05)
                continue

            self._handle = handle
            return True

    def release(self) -> None:
        handle = self._handle
        if handle is None:
            return

        self._handle = None
        try:
            if os.name == "nt":
                import msvcrt

                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                import fcntl

                fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
        finally:
            handle.close()

    def __enter__(self) -> _FileLock:
        if not self.acquire(wait=True, timeout_seconds=30.0):
            raise RuntimeError(
                f"Log-Lock konnte nicht innerhalb von 30 Sekunden erworben werden: "
                f"{self.path}"
            )
        return self

    def __exit__(self, _exc_type, _exc, _tb) -> None:
        self.release()


class _ProcessRotatingFileHandler(RotatingFileHandler):
    def __init__(self, *args, process_lock: _FileLock, **kwargs) -> None:
        self._process_lock = process_lock
        super().__init__(*args, **kwargs)

    def close(self) -> None:
        try:
            super().close()
        finally:
            self._process_lock.release()


def process_log_file(path: Path, *, pid: int | None = None) -> Path:
    """Return the process-specific log path used by cli-agent."""

    process_id = os.getpid() if pid is None else pid
    return path.with_name(f"{path.stem}-{process_id}{path.suffix}")


def _process_lock_file(log_file: Path) -> Path:
    return log_file.with_name(f"{log_file.name}.lock")


def _cleanup_lock_file(base_file: Path) -> Path:
    return base_file.with_name(f"{base_file.name}.cleanup.lock")


def _process_log_pattern(base_file: Path) -> re.Pattern[str]:
    stem = re.escape(base_file.stem)
    suffix = re.escape(base_file.suffix)
    if suffix:
        expression = rf"^{stem}-(?P<pid>\d+){suffix}(?:\.\d+)?$"
    else:
        expression = rf"^{stem}-(?P<pid>\d+)(?:\.\d+)?$"
    return re.compile(expression)


def _process_lock_pattern(base_file: Path) -> re.Pattern[str]:
    stem = re.escape(base_file.stem)
    suffix = re.escape(base_file.suffix)
    if suffix:
        expression = rf"^{stem}-(?P<pid>\d+){suffix}\.lock$"
    else:
        expression = rf"^{stem}-(?P<pid>\d+)\.lock$"
    return re.compile(expression)


def _cleanup_process_log_files(
    base_file: Path,
    *,
    current_pid: int,
    keep_inactive: int,
) -> None:
    """Keep only the newest completed process-log families.

    Active cli-agent processes are identified through their per-process lock and
    are never removed. The caller holds the cleanup lock, so no new cli-agent
    process can create a process log while cleanup is in progress.
    """

    directory = base_file.parent
    log_pattern = _process_log_pattern(base_file)
    lock_pattern = _process_lock_pattern(base_file)
    families: dict[int, list[Path]] = {}
    lock_files: dict[int, Path] = {}

    for entry in directory.iterdir():
        log_match = log_pattern.fullmatch(entry.name)
        if log_match:
            pid = int(log_match.group("pid"))
            families.setdefault(pid, []).append(entry)
            continue

        lock_match = lock_pattern.fullmatch(entry.name)
        if lock_match:
            lock_files[int(lock_match.group("pid"))] = entry

    inactive: list[tuple[float, int, list[Path], Path | None]] = []
    for pid, paths in families.items():
        if pid == current_pid:
            continue

        lock_path = lock_files.get(pid)
        probe = _FileLock(lock_path) if lock_path is not None else None
        if probe is not None and not probe.acquire():
            continue

        try:
            newest_mtime = max(path.stat().st_mtime for path in paths)
            inactive.append((newest_mtime, pid, paths, lock_path))
        finally:
            if probe is not None:
                probe.release()

    inactive.sort(reverse=True)
    for _mtime, _pid, paths, lock_path in inactive[max(0, keep_inactive):]:
        for path in paths:
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        if lock_path is not None:
            try:
                lock_path.unlink()
            except FileNotFoundError:
                pass

    # Remove stale lock files that no longer have a corresponding log family.
    family_pids = set(families)
    for pid, lock_path in lock_files.items():
        if pid == current_pid or pid in family_pids:
            continue
        probe = _FileLock(lock_path)
        if not probe.acquire():
            continue
        probe.release()
        try:
            lock_path.unlink()
        except FileNotFoundError:
            pass


def configure_logging(
    config: LoggingConfig,
    *,
    logger=None,
    default_filename: str | None = None,
) -> None:
    if logger is None:
        logger = logging.getLogger("cli_agent")

    for handler in logger.handlers[:]:
        handler.close()
    logger.handlers.clear()
    logger.propagate = False

    if not config.enabled:
        logger.disabled = True
        return

    level = logging.getLevelName(config.level.upper())
    if not isinstance(level, int):
        raise ValueError(f"Ungültiger Logging-Level: {config.level}")

    if default_filename is not None:
        config = replace(config, file=config.file.with_name(default_filename))

    config.file.parent.mkdir(parents=True, exist_ok=True)
    log_file = process_log_file(config.file)
    process_lock = _FileLock(_process_lock_file(log_file))

    cleanup_lock = _FileLock(_cleanup_lock_file(config.file))
    with cleanup_lock:
        if not process_lock.acquire():
            raise RuntimeError(
                f"Prozess-Logdatei wird bereits verwendet: {log_file}"
            )
        try:
            _cleanup_process_log_files(
                config.file,
                current_pid=os.getpid(),
                keep_inactive=config.backup_count,
            )
            handler = _ProcessRotatingFileHandler(
                log_file,
                maxBytes=config.max_bytes,
                backupCount=config.backup_count,
                encoding="utf-8",
                process_lock=process_lock,
            )
        except Exception:
            process_lock.release()
            raise

    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )

    logger.disabled = False
    logger.setLevel(level)
    logger.addHandler(handler)
