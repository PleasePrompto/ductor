"""Centralized registry for active CLI subprocesses."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from dataclasses import dataclass, field

from ductor_bot.infra.process_tree import (
    force_kill_process_tree,
    interrupt_process,
    terminate_process_tree,
)

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
        self._aborted_labels: set[tuple[int, str]] = set()
        self._interrupted: set[int] = set()

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

    async def kill_all_active(self) -> int:
        """Kill active processes across all chats. Returns total count killed."""
        total = 0
        for chat_id in list(self._processes):
            total += await self.kill_all(chat_id)
        return total

    def was_aborted(self, chat_id: int) -> bool:
        """Check whether *chat_id* has been aborted since last clear."""
        return chat_id in self._aborted

    def clear_abort(self, chat_id: int) -> None:
        """Clear the abort flag for *chat_id*."""
        self._aborted.discard(chat_id)

    def was_interrupted(self, chat_id: int) -> bool:
        """Check whether *chat_id* was soft-interrupted since last clear."""
        return chat_id in self._interrupted

    def clear_interrupt(self, chat_id: int) -> None:
        """Clear the interrupt flag for *chat_id*."""
        self._interrupted.discard(chat_id)

    def has_active(self, chat_id: int) -> bool:
        """Return True if *chat_id* has at least one running subprocess."""
        entries = self._processes.get(chat_id, [])
        return any(e.process.returncode is None for e in entries)

    async def kill_by_label(self, chat_id: int, label: str) -> int:
        """Kill processes matching *label* for *chat_id*. Returns count killed."""
        self._aborted_labels.add((chat_id, label))
        entries = self._processes.get(chat_id, [])
        to_kill = [e for e in entries if e.label == label and e.process.returncode is None]
        if not to_kill:
            return 0
        remaining = [e for e in entries if e not in to_kill]
        if remaining:
            self._processes[chat_id] = remaining
        else:
            self._processes.pop(chat_id, None)
        return await _kill_processes(to_kill)

    def was_label_aborted(self, chat_id: int, label: str) -> bool:
        """Check whether a specific label was aborted."""
        return (chat_id, label) in self._aborted_labels

    def clear_label_abort(self, chat_id: int, label: str) -> None:
        """Clear the abort flag for a specific label."""
        self._aborted_labels.discard((chat_id, label))

    def interrupt_all(self, chat_id: int) -> int:
        """Send SIGINT to every active process for *chat_id*.

        Unlike :meth:`kill_all` this does NOT terminate or unregister the
        processes — it sends a soft interrupt so the CLI can cancel the
        current operation (equivalent to pressing ESC in the terminal).
        Returns the count of processes signalled.
        """
        entries = self._processes.get(chat_id, [])
        if not entries:
            return 0
        self._interrupted.add(chat_id)
        count = 0
        for tracked in entries:
            if tracked.process.returncode is not None:
                continue
            interrupt_process(tracked.process.pid)
            logger.debug(
                "SIGINT sent: pid=%s label=%s chat=%d",
                tracked.process.pid,
                tracked.label,
                tracked.chat_id,
            )
            count += 1
        if count:
            logger.info("Interrupted %d CLI process(es) for chat=%d", count, chat_id)
        return count

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
            _close_stdin(tracked.process)
            terminate_process_tree(tracked.process.pid)
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
            _close_stdin(tracked.process)
            force_kill_process_tree(tracked.process.pid)
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


def _close_stdin(process: asyncio.subprocess.Process) -> None:
    """Best-effort stdin close so readers can unwind promptly."""
    stdin = getattr(process, "stdin", None)
    if stdin is None:
        return
    with contextlib.suppress(OSError, RuntimeError, ValueError):
        stdin.close()
