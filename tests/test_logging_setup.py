from __future__ import annotations

import logging

from cli_agent.config import LoggingConfig
from cli_agent.logging_setup import (
    _process_lock_file,
    configure_logging,
    process_log_file,
)


def test_locked_inactive_log_does_not_block_new_process_logging(
    tmp_path,
    monkeypatch,
) -> None:
    base_file = tmp_path / "cli-agent.log"
    inactive_log = process_log_file(base_file, pid=424242)
    inactive_lock = _process_lock_file(inactive_log)
    inactive_log.write_text("old log", encoding="utf-8")
    inactive_lock.write_bytes(b"\0")

    original_unlink = type(inactive_log).unlink

    def unlink(path, *args, **kwargs):
        if path == inactive_log:
            raise PermissionError("simulated Windows file lock")
        return original_unlink(path, *args, **kwargs)

    monkeypatch.setattr(type(inactive_log), "unlink", unlink)

    logger = logging.getLogger("cli_agent.test_locked_inactive_log")
    logger.handlers.clear()

    configure_logging(
        LoggingConfig(file=base_file, backup_count=0),
        logger=logger,
    )
    try:
        logger.info("new process is running")
        assert process_log_file(base_file).exists()
        assert inactive_log.exists()
        # Keep the matching lock marker when cleanup of its log family was
        # incomplete, so another live process is never misclassified later.
        assert inactive_lock.exists()
    finally:
        for handler in logger.handlers[:]:
            handler.close()
        logger.handlers.clear()
