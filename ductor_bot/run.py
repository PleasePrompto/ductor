"""Supervisor: watches for file changes, manages child process lifecycle.

Starts the Telegram bot as a child process and restarts it on:
- Python file changes in ductor_bot/ (hot-reload via watchfiles)
- Exit code 42 (agent-requested restart)
- Crashes (with exponential backoff)
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import sys
import time
from pathlib import Path

EXIT_RESTART = 42
EXIT_CLEAN = 0
FAST_CRASH_THRESHOLD = 10.0
MAX_BACKOFF = 30.0
SIGTERM_TIMEOUT = 10.0

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [supervisor] %(levelname)s %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("supervisor")

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WATCH_DIR = PROJECT_ROOT / "ductor_bot"


async def _terminate_child(proc: asyncio.subprocess.Process) -> int:
    """Send SIGTERM and wait for graceful shutdown."""
    if proc.returncode is not None:
        return proc.returncode
    proc.terminate()
    try:
        return await asyncio.wait_for(proc.wait(), timeout=SIGTERM_TIMEOUT)
    except TimeoutError:
        logger.warning("Child did not exit in %.0fs, sending SIGKILL", SIGTERM_TIMEOUT)
        proc.kill()
        return await proc.wait()


async def _run_child(cmd: list[str]) -> tuple[int, bool]:
    """Spawn child and watch for file changes concurrently.

    Returns (returncode, file_change_triggered).
    """
    proc = await asyncio.create_subprocess_exec(*cmd, cwd=str(PROJECT_ROOT))
    logger.info("Child started: pid=%d", proc.pid)

    file_change = False

    async def _watch_files() -> None:
        nonlocal file_change
        try:
            from watchfiles import awatch  # type: ignore[import-not-found]
        except ImportError:
            return  # watchfiles not installed, skip hot-reload

        async for changes in awatch(WATCH_DIR, recursive=True):
            relevant = [p for _, p in changes if p.endswith(".py")]
            if relevant:
                names = [Path(p).name for p in relevant[:5]]
                suffix = f" (+{len(relevant) - 5})" if len(relevant) > 5 else ""
                logger.info("File change detected: %s%s", ", ".join(names), suffix)
                file_change = True
                await _terminate_child(proc)
                return

    watch_task = asyncio.create_task(_watch_files())
    try:
        await proc.wait()
    except asyncio.CancelledError:
        await _terminate_child(proc)
        raise
    finally:
        watch_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await watch_task

    assert proc.returncode is not None
    return proc.returncode, file_change


async def supervisor() -> None:
    """Main supervisor loop with crash recovery."""
    os.environ["DUCTOR_SUPERVISOR"] = "1"
    fast_crash_count = 0
    cmd = [sys.executable, "-m", "ductor_bot"]

    while True:
        logger.info("Starting bot")
        start_time = time.monotonic()
        returncode, file_triggered = await _run_child(cmd)
        runtime = time.monotonic() - start_time

        logger.info(
            "Supervisor child exit_code=%d runtime=%.1fs file_triggered=%s",
            returncode,
            runtime,
            file_triggered,
        )

        if file_triggered:
            logger.info("File change restart, respawning immediately")
            fast_crash_count = 0
            continue

        if returncode == EXIT_CLEAN:
            logger.info("Clean shutdown, supervisor exiting")
            break

        if returncode == EXIT_RESTART:
            logger.info("Restart requested, respawning immediately")
            fast_crash_count = 0
            continue

        if runtime < FAST_CRASH_THRESHOLD:
            fast_crash_count += 1
        else:
            fast_crash_count = 0

        backoff = min(2.0**fast_crash_count, MAX_BACKOFF)
        logger.warning(
            "Crash detected, restarting in %.0fs (fast_crashes=%d)",
            backoff,
            fast_crash_count,
        )
        await asyncio.sleep(backoff)


def main() -> None:
    """Entry point with signal handling."""
    loop = asyncio.new_event_loop()
    task = loop.create_task(supervisor())

    if sys.platform != "win32":
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, task.cancel)

    try:
        loop.run_until_complete(task)
    except (asyncio.CancelledError, KeyboardInterrupt):
        logger.info("Supervisor interrupted")
    finally:
        loop.close()


if __name__ == "__main__":
    main()
