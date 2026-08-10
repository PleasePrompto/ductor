"""Generation-scoped admission for graceful marker restarts.

Counting work after it has started cannot close the zero-observation race.  A
root unit therefore acquires a lease before it can mutate a session or send a
user-visible result.  Closing admission and recording that lease are one
event-loop operation; leases already held (including child tasks) survive the
close and must finish before a clean restart may exit.
"""

from __future__ import annotations

import asyncio
import contextvars
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager


class AdmissionClosed(RuntimeError):
    """Raised when a new root unit arrives after its generation was closed."""


_lease: contextvars.ContextVar["AdmissionLease | None"] = contextvars.ContextVar(
    "ductor_restart_lease", default=None
)


class AdmissionLease:
    """One root admission; nested work inherits this lease through ContextVar."""

    def __init__(self, coordinator: "RestartAdmissionCoordinator", generation: int) -> None:
        self._coordinator = coordinator
        self.generation = generation
        self._released = False

    async def release(self) -> None:
        if not self._released:
            self._released = True
            await self._coordinator._release(self.generation)


class RestartAdmissionCoordinator:
    """Atomically admit roots and prove a closed generation has quiesced."""

    def __init__(self) -> None:
        self._generation = 1
        self._closed = False
        self._active: dict[int, int] = {self._generation: 0}
        self._changed = asyncio.Condition()

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def closed(self) -> bool:
        return self._closed

    def active_count(self, generation: int | None = None) -> int:
        return self._active.get(generation or self._generation, 0)

    @asynccontextmanager
    async def lease(self, _label: str = "") -> AsyncIterator[AdmissionLease]:
        """Admit a root unit, or inherit its parent's already-admitted lease."""
        inherited = _lease.get()
        if inherited is not None and inherited._coordinator is self and not inherited._released:
            yield inherited
            return
        acquired = self.reserve()
        token = _lease.set(acquired)
        try:
            yield acquired
        finally:
            _lease.reset(token)
            await acquired.release()

    def reserve(self) -> AdmissionLease:
        """Synchronously reserve a root lease before spawning an asyncio task.

        This runs only on the owning event loop: no await can interleave the
        close check and increment, which gives task creation the same atomic
        admission guarantee as ``lease()``.
        """
        if self._closed:
            raise AdmissionClosed("restart generation is quiescing")
        generation = self._generation
        self._active[generation] = self._active.get(generation, 0) + 1
        return AdmissionLease(self, generation)

    @asynccontextmanager
    async def adopt(self, lease: AdmissionLease) -> AsyncIterator[AdmissionLease]:
        """Run a spawned child under a previously reserved root lease."""
        token = _lease.set(lease)
        try:
            yield lease
        finally:
            _lease.reset(token)
            await lease.release()

    async def close(self) -> int:
        """Atomically close root admission and return the closed generation."""
        async with self._changed:
            self._closed = True
            self._changed.notify_all()
            return self._generation

    async def wait_for_quiescence(self, generation: int, timeout: float) -> bool:
        """Wait until every admitted root from *generation* has released."""
        async with self._changed:
            try:
                await asyncio.wait_for(
                    self._changed.wait_for(lambda: self._active.get(generation, 0) == 0), timeout
                )
            except TimeoutError:
                return False
            return True

    async def _release(self, generation: int) -> None:
        async with self._changed:
            active = self._active.get(generation, 0)
            if active <= 0:
                raise RuntimeError("admission lease released twice")
            self._active[generation] = active - 1
            self._changed.notify_all()
