"""Restart sentinel and request helpers for graceful hot-reload."""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

EXIT_RESTART = 42
"""Exit code that tells the supervisor to restart immediately."""


def write_restart_sentinel(
    chat_id: int,
    message: str = "Restart completed.",
    *,
    sentinel_path: Path,
) -> None:
    """Write a sentinel file so the bot can notify the user after restart."""
    sentinel_path.parent.mkdir(parents=True, exist_ok=True)
    data = {
        "chat_id": chat_id,
        "message": message,
        "timestamp": datetime.now(UTC).isoformat(),
    }
    content = json.dumps(data)
    fd, tmp_str = tempfile.mkstemp(dir=str(sentinel_path.parent), suffix=".tmp")
    tmp = Path(tmp_str)
    try:
        os.write(fd, content.encode())
        os.close(fd)
        tmp.replace(sentinel_path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.close(fd)
        tmp.unlink(missing_ok=True)
        raise
    logger.info("Restart sentinel written for chat=%d", chat_id)


def consume_restart_sentinel(*, sentinel_path: Path) -> dict[str, Any] | None:
    """Read and delete the sentinel file. Returns None if absent."""
    if not sentinel_path.exists():
        return None
    try:
        data: dict[str, Any] = json.loads(sentinel_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.exception("Failed to read restart sentinel")
        sentinel_path.unlink(missing_ok=True)
        return None
    else:
        sentinel_path.unlink(missing_ok=True)
        logger.info("Restart sentinel consumed for chat=%s", data.get("chat_id"))
        return data


def write_restart_marker(*, marker_path: Path) -> None:
    """Write a marker file that tells the running bot to shut down with EXIT_RESTART."""
    marker_path.parent.mkdir(parents=True, exist_ok=True)
    marker_path.write_text("1", encoding="utf-8")
    logger.info("Restart marker written")


def consume_restart_marker(*, marker_path: Path) -> bool:
    """Check and delete the restart marker. Returns True if it existed."""
    if not marker_path.exists():
        return False
    marker_path.unlink(missing_ok=True)
    return True
