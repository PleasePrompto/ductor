"""PID lockfile: prevents multiple bot instances from running simultaneously."""

from __future__ import annotations

import contextlib
import logging
import os
import signal
import sys
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_KILL_WAIT_SECONDS = 5.0
_KILL_POLL_INTERVAL = 0.2

_IS_WINDOWS = sys.platform == "win32"


def _is_process_alive(pid: int) -> bool:
    """Check if a process with the given PID is still running."""
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _terminate_process(pid: int) -> None:
    """Send termination signal (SIGTERM on POSIX, TerminateProcess on Windows)."""
    os.kill(pid, signal.SIGTERM)


def _force_kill_process(pid: int) -> None:
    """Force-kill process (SIGKILL on POSIX, TerminateProcess on Windows)."""
    if _IS_WINDOWS:
        os.kill(pid, signal.SIGTERM)
    else:
        os.kill(pid, signal.SIGKILL)


def _kill_and_wait(pid: int) -> None:
    """Send termination signal, wait for exit, escalate to force-kill if needed."""
    logger.info("Stopping existing bot instance (pid=%d)", pid)
    try:
        _terminate_process(pid)
    except OSError:
        logger.warning("Failed to terminate pid=%d", pid, exc_info=True)
        return

    deadline = time.monotonic() + _KILL_WAIT_SECONDS
    while time.monotonic() < deadline:
        if not _is_process_alive(pid):
            logger.info("Previous instance (pid=%d) exited cleanly", pid)
            return
        time.sleep(_KILL_POLL_INTERVAL)

    logger.warning("pid=%d did not exit after %.0fs, force killing", pid, _KILL_WAIT_SECONDS)
    with contextlib.suppress(OSError):
        _force_kill_process(pid)
    time.sleep(_KILL_POLL_INTERVAL)


def acquire_lock(*, pid_file: Path, kill_existing: bool = False) -> None:
    """Write PID file after ensuring no other instance is running.

    Args:
        pid_file: Path to the PID lockfile.
        kill_existing: If True, kill any running instance before acquiring.
                       If False, raise ``SystemExit`` when another instance is found.
    """
    pid_file.parent.mkdir(parents=True, exist_ok=True)

    if pid_file.exists():
        try:
            existing_pid = int(pid_file.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            existing_pid = None

        if existing_pid is not None and _is_process_alive(existing_pid):
            if kill_existing:
                _kill_and_wait(existing_pid)
            else:
                logger.error(
                    "Another bot instance is already running (pid=%d). "
                    "Kill it first or delete %s if stale.",
                    existing_pid,
                    pid_file,
                )
                raise SystemExit(1)
        else:
            logger.warning("Stale PID file found (pid=%s), overwriting", existing_pid)

    # Write atomically: temp file + rename so that a partial write can never
    # leave a corrupt PID file, and the final rename is atomic on POSIX.
    import tempfile
    fd, tmp_str = tempfile.mkstemp(dir=str(pid_file.parent), suffix=".tmp")
    tmp = Path(tmp_str)
    try:
        os.write(fd, str(os.getpid()).encode())
        os.close(fd)
        tmp.replace(pid_file)
    except BaseException:
        with contextlib.suppress(OSError):
            os.close(fd)
        tmp.unlink(missing_ok=True)
        raise
    logger.info("PID lock acquired (pid=%d)", os.getpid())


def release_lock(*, pid_file: Path) -> None:
    """Remove PID file if it belongs to the current process."""
    if not pid_file.exists():
        return
    try:
        stored_pid = int(pid_file.read_text(encoding="utf-8").strip())
    except (ValueError, OSError):
        pid_file.unlink(missing_ok=True)
        return

    if stored_pid == os.getpid():
        pid_file.unlink(missing_ok=True)
        logger.info("PID lock released")
    else:
        logger.debug("PID file belongs to pid=%d, not removing", stored_pid)
