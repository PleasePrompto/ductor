"""Self-update observer: periodic version check + upgrade execution."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path

from ductor_bot.infra.version import VersionInfo, check_pypi

logger = logging.getLogger(__name__)

_CHECK_INTERVAL_S = 3600  # 60 minutes
_INITIAL_DELAY_S = 60  # 1 minute after startup

VersionCallback = Callable[[VersionInfo], Awaitable[None]]

_UPGRADE_SENTINEL_NAME = "upgrade-sentinel.json"


class UpdateObserver:
    """Background task that checks PyPI for new versions periodically."""

    def __init__(self, *, notify: VersionCallback) -> None:
        self._notify = notify
        self._task: asyncio.Task[None] | None = None
        self._last_notified: str = ""

    def start(self) -> None:
        self._task = asyncio.create_task(self._loop())

    async def stop(self) -> None:
        if self._task:
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def _loop(self) -> None:
        await asyncio.sleep(_INITIAL_DELAY_S)
        while True:
            try:
                info = await check_pypi()
                if info and info.update_available and info.latest != self._last_notified:
                    self._last_notified = info.latest
                    await self._notify(info)
            except Exception:
                logger.debug("Update check failed", exc_info=True)
            await asyncio.sleep(_CHECK_INTERVAL_S)


# ---------------------------------------------------------------------------
# Upgrade execution
# ---------------------------------------------------------------------------


async def perform_upgrade() -> tuple[bool, str]:
    """Run the package upgrade. Returns ``(success, output)``.

    Refuses to upgrade dev/editable installs -- those should use ``git pull``.
    """
    from ductor_bot.infra.install import detect_install_mode

    mode = detect_install_mode()
    if mode == "dev":
        return False, "Running from source (editable install). Use `git pull` to update."
    if mode == "pipx":
        cmd = ["pipx", "upgrade", "ductor"]
    else:
        cmd = [sys.executable, "-m", "pip", "install", "--upgrade", "ductor"]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    stdout, _ = await proc.communicate()
    output = stdout.decode(errors="replace") if stdout else ""
    return (proc.returncode or 0) == 0, output


# ---------------------------------------------------------------------------
# Upgrade sentinel (post-restart notification)
# ---------------------------------------------------------------------------


def write_upgrade_sentinel(
    sentinel_dir: Path,
    *,
    chat_id: int,
    old_version: str,
    new_version: str,
) -> None:
    """Write sentinel so the bot can notify the user after upgrade restart."""
    sentinel_dir.mkdir(parents=True, exist_ok=True)
    path = sentinel_dir / _UPGRADE_SENTINEL_NAME
    path.write_text(
        json.dumps(
            {
                "chat_id": chat_id,
                "old_version": old_version,
                "new_version": new_version,
            }
        ),
        encoding="utf-8",
    )


def consume_upgrade_sentinel(sentinel_dir: Path) -> dict[str, str | int] | None:
    """Read and delete the upgrade sentinel. Returns None if absent."""
    path = sentinel_dir / _UPGRADE_SENTINEL_NAME
    if not path.exists():
        return None
    try:
        data: dict[str, str | int] = json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.exception("Failed to read upgrade sentinel")
        path.unlink(missing_ok=True)
        return None
    else:
        path.unlink(missing_ok=True)
        logger.info(
            "Upgrade sentinel consumed: %s -> %s", data.get("old_version"), data.get("new_version")
        )
        return data
