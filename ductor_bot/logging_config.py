"""Centralized logging setup: console (colored) + rotating file.

Call ``setup_logging()`` once at startup.  All modules use ``logging.getLogger(__name__)``.
"""

from __future__ import annotations

import atexit
import logging
import queue
import sys
from logging.handlers import QueueHandler, QueueListener, RotatingFileHandler
from pathlib import Path

MAX_BYTES = 5 * 1024 * 1024  # 5 MB per file
BACKUP_COUNT = 3

CONSOLE_FMT = "%(asctime)s %(levelname)s %(name)s: %(ctx)s%(message)s"
FILE_FMT = "%(asctime)s [%(levelname)s] %(name)s:%(filename)s:%(lineno)d: %(ctx)s%(message)s"
DATE_FMT = "%H:%M:%S"
FILE_DATE_FMT = "%Y-%m-%d %H:%M:%S"

logger = logging.getLogger(__name__)

_ANSI = {
    "DEBUG": "\x1b[36m",
    "INFO": "\x1b[32m",
    "WARNING": "\x1b[33m",
    "ERROR": "\x1b[31m",
    "CRITICAL": "\x1b[35m",
}
_RESET = "\x1b[0m"

_queue_listener: QueueListener | None = None
_atexit_registered: bool = False


def _stop_queue_listener() -> None:
    global _queue_listener  # noqa: PLW0603
    if _queue_listener is not None:
        _queue_listener.stop()
        _queue_listener = None


class _ColorFormatter(logging.Formatter):
    """ANSI-colored level names for terminal output."""

    def __init__(self, fmt: str, datefmt: str | None = None, use_color: bool = True) -> None:
        super().__init__(fmt, datefmt)
        self._use_color = use_color

    def format(self, record: logging.LogRecord) -> str:
        original = record.levelname
        if self._use_color:
            color = _ANSI.get(original, "")
            record.levelname = f"{color}{original:<8}{_RESET}"
        else:
            record.levelname = f"{original:<8}"
        try:
            return super().format(record)
        finally:
            record.levelname = original


def setup_logging(
    level: int = logging.INFO,
    verbose: bool = False,
    log_dir: Path | None = None,
) -> None:
    """Configure root logger with console + optional rotating file handler.

    Args:
        level: Minimum log level. DEBUG if verbose, else INFO.
        verbose: If True, sets DEBUG level and enables debug output.
        log_dir: Directory for log files. If None, file logging is skipped.
    """
    if verbose:
        level = logging.DEBUG

    _stop_queue_listener()

    from ductor_bot.log_context import ContextFilter

    ctx_filter = ContextFilter()

    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    # pythonw.exe (Windows service) sets sys.stderr to None
    if sys.stderr is not None:
        console_handler = logging.StreamHandler(sys.stderr)
        console_handler.setLevel(level)
        console_handler.addFilter(ctx_filter)
        use_color = hasattr(sys.stderr, "isatty") and sys.stderr.isatty()
        console_handler.setFormatter(
            _ColorFormatter(CONSOLE_FMT, datefmt=DATE_FMT, use_color=use_color)
        )
        root.addHandler(console_handler)

    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        log_file = log_dir / "agent.log"
        file_handler = RotatingFileHandler(
            log_file,
            maxBytes=MAX_BYTES,
            backupCount=BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setLevel(logging.DEBUG)
        file_handler.setFormatter(logging.Formatter(FILE_FMT, datefmt=FILE_DATE_FMT))

        log_queue: queue.Queue[logging.LogRecord] = queue.Queue()
        queue_handler = QueueHandler(log_queue)
        queue_handler.setLevel(logging.DEBUG)
        queue_handler.addFilter(ctx_filter)
        root.addHandler(queue_handler)

        global _queue_listener, _atexit_registered  # noqa: PLW0603
        listener = QueueListener(log_queue, file_handler, respect_handler_level=True)
        listener.start()
        _queue_listener = listener
        if not _atexit_registered:
            atexit.register(_stop_queue_listener)
            _atexit_registered = True

    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    logging.getLogger("telegram").setLevel(logging.WARNING)
    logging.getLogger("telegram.ext").setLevel(logging.WARNING)

    logger.info("Logging initialized (level=%s)", logging.getLevelName(level))
