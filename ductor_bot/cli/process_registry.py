"""Centralized registry for active CLI subprocesses."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

logger = logging.getLogger(__name__)

_SIGTERM_GRACE_SECONDS = 2.0


@dataclass(slots=True)
class TrackedProcess:
    """A registered subprocess with metadata."""

    process: asyncio.subprocess.Process
    chat_id: int
    label: str
    registered_at: float = field(default_factory=time.time)


class ProcessRegistry:
    """Global registry of active CLI subprocesses, keyed by *chat_id*."""

    def __init__(self) -> None:
        self._processes: dict[int, list[TrackedProcess]] = {}
        self._aborted: set[int] = set()

    def register(
        self, chat_id: int, process: asyncio.subprocess.Process, label: str
    ) -> TrackedProcess:
        """Register a subprocess. Returns the tracking handle."""
        tracked = TrackedProcess(
            process=process,
            chat_id=chat_id,
            label=label,
        )
        self._processes.setdefault(chat_id, []).append(tracked)
        logger.debug(
            "Process registered: chat=%d label=%s pid=%s",
            chat_id,
            label,
            process.pid,
        )
        return tracked

    def unregister(self, tracked: TrackedProcess) -> None:
        """Remove a tracked process (idempotent)."""
        entries = self._processes.get(tracked.chat_id)
        if entries is None:
            return
        try:
            entries.remove(tracked)
        except ValueError:
            return
        if not entries:
            del self._processes[tracked.chat_id]
        logger.debug(
            "Process unregistered: chat=%d label=%s pid=%s",
            tracked.chat_id,
            tracked.label,
            tracked.process.pid,
        )

    async def kill_all(self, chat_id: int) -> int:
        """Kill every active process for *chat_id*. Returns count killed."""
        self._aborted.add(chat_id)
        entries = self._processes.pop(chat_id, [])
        if not entries:
            return 0
        return await _kill_processes(entries)

    def was_aborted(self, chat_id: int) -> bool:
        """Check whether *chat_id* has been aborted since last clear."""
        return chat_id in self._aborted

    def clear_abort(self, chat_id: int) -> None:
        """Clear the abort flag for *chat_id*."""
        self._aborted.discard(chat_id)

    def has_active(self, chat_id: int) -> bool:
        """Return True if *chat_id* has at least one running subprocess."""
        entries = self._processes.get(chat_id, [])
        return any(e.process.returncode is None for e in entries)

    async def kill_stale(self, max_age_seconds: float) -> int:
        """Kill processes older than *max_age_seconds* (wall-clock). Returns count killed."""
        now = time.time()
        stale: list[TrackedProcess] = []
        for entries in self._processes.values():
            for tracked in entries:
                if tracked.process.returncode is not None:
                    continue
                age = now - tracked.registered_at
                if age > max_age_seconds:
                    logger.warning(
                        "Stale process: pid=%s label=%s chat=%d age=%.0fs",
                        tracked.process.pid,
                        tracked.label,
                        tracked.chat_id,
                        age,
                    )
                    stale.append(tracked)
        if not stale:
            return 0
        killed = await _kill_processes(stale)
        for tracked in stale:
            self.unregister(tracked)
        return killed


def _send_sigterm(entries: list[TrackedProcess]) -> int:
    """Terminate all live processes. Returns count signalled."""
    count = 0
    for tracked in entries:
        if tracked.process.returncode is not None:
            continue
        try:
            tracked.process.terminate()
            logger.debug("Terminate sent: pid=%s label=%s", tracked.process.pid, tracked.label)
            count += 1
        except ProcessLookupError:
            pass
    return count


def _send_sigkill(entries: list[TrackedProcess]) -> None:
    """Send SIGKILL to processes still alive after grace period."""
    for tracked in entries:
        if tracked.process.returncode is not None:
            continue
        try:
            tracked.process.kill()
            logger.debug("SIGKILL sent: pid=%s label=%s", tracked.process.pid, tracked.label)
        except ProcessLookupError:
            pass


async def _reap(entries: list[TrackedProcess]) -> None:
    """Wait for all processes to exit."""
    for tracked in entries:
        if tracked.process.returncode is None:
            try:
                await asyncio.wait_for(tracked.process.wait(), timeout=5.0)
            except TimeoutError:
                logger.warning("Process did not exit after SIGKILL: pid=%s", tracked.process.pid)


async def _kill_processes(entries: list[TrackedProcess]) -> int:
    """SIGTERM -> wait -> SIGKILL for each process. Returns count killed."""
    if not entries:
        return 0
    killed = _send_sigterm(entries)
    if not killed:
        return 0
    await asyncio.sleep(_SIGTERM_GRACE_SECONDS)
    _send_sigkill(entries)
    await _reap(entries)
    logger.info("Killed %d CLI process(es)", killed)
    return killed
