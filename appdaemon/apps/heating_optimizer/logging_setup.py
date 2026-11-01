"""Application logger: a rotating file plus forwarding to the AppDaemon log."""

from __future__ import annotations

import logging
from collections.abc import Callable
from logging.handlers import RotatingFileHandler
from pathlib import Path

LOGGER_NAME = "heating_optimizer"


class _ForwardHandler(logging.Handler):
    """Forwards records to AppDaemon's ``self.log`` so they also show up in the add-on log."""

    def __init__(self, forward: Callable[[str, str], None]):
        super().__init__()
        self._forward = forward

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self._forward(self.format(record), record.levelname)
        except Exception:  # never let logging break the app
            self.handleError(record)


def setup_logger(
    log_dir: str | None,
    level: str,
    rotate_mb: float,
    backups: int,
    forward: Callable[[str, str], None] | None = None,
    name: str = LOGGER_NAME,
) -> tuple[logging.Logger, str | None]:
    """Configure the app logger. Returns the logger and an error message if the file log could not be opened.

    Safe to call again on an AppDaemon reload: existing handlers are replaced.
    """
    logger = logging.getLogger(name)
    logger.setLevel(level)
    logger.propagate = False
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()

    if forward is not None:
        fwd = _ForwardHandler(forward)
        fwd.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(fwd)

    error = None
    if log_dir:
        try:
            Path(log_dir).mkdir(parents=True, exist_ok=True)
            file_handler = RotatingFileHandler(
                Path(log_dir) / "heating_optimizer.log",
                maxBytes=int(rotate_mb * 1024 * 1024),
                backupCount=backups,
                encoding="utf-8",
            )
            file_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(message)s"))
            logger.addHandler(file_handler)
        except OSError as exc:
            error = f"cannot write log files in {log_dir}: {exc}"
    return logger, error
