from __future__ import annotations

import logging
import os
from dataclasses import replace
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import LoggingConfig


def process_log_file(path: Path, *, pid: int | None = None) -> Path:
    """Return the process-specific log path used by cli-agent.

    RotatingFileHandler is not safe for multiple independent processes writing
    and rotating the same file. Including the process ID keeps each cli-agent
    instance on its own log file without requiring a platform-specific locking
    mechanism.
    """

    process_id = os.getpid() if pid is None else pid
    return path.with_name(f"{path.stem}-{process_id}{path.suffix}")


def configure_logging(
    config: LoggingConfig,
    *,
    logger=None,
    default_filename: str | None = None,
) -> None:
    if logger is None:
        logger = logging.getLogger("cli_agent")
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

    log_file = process_log_file(config.file)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        log_file,
        maxBytes=config.max_bytes,
        backupCount=config.backup_count,
        encoding="utf-8",
    )
    handler.setFormatter(
        logging.Formatter(
            "%(asctime)s %(levelname)s %(name)s %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
    )

    logger.disabled = False
    logger.setLevel(level)
    logger.addHandler(handler)
