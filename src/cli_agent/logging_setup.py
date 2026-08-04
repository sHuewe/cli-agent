from __future__ import annotations

import logging
from dataclasses import replace
from logging.handlers import RotatingFileHandler

from .config import LoggingConfig


def configure_logging(
    config: LoggingConfig,
    *,
    logger = None,
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

    config.file.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        config.file,
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
