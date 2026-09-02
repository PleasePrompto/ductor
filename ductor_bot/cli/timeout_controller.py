"""Timeout management with warnings and activity-based extensions.

Provides a reusable ``TimeoutController`` that wraps an awaitable with
configurable warning callbacks and deadline extension when the subprocess
is actively producing output.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TypeVar

logger = logging.getLogger(__name__)

T = TypeVar("T")

# Activity within the last N seconds counts as "recent" for extension decisions.
_ACTIVITY_RECENCY_SECONDS = 30.0


@dataclass(slots=True)
class TimeoutWarning:
    """Emitted when approaching timeout."""

    remaining_seconds: float
    total_seconds: float
    extensions_used: int


@dataclass(frozen=True, slots=True)
class TimeoutConfig:
    """Grouped configuration for :class:`TimeoutController`."""

    timeout_seconds: float
    warning_intervals: list[float] = field(default_factory=list)
    extend_on_activity: bool = True
    activity_extension: float = 120.0
    max_extensions: int = 3


class TimeoutController:
    """Manages timeout with warnings and activity-based extensions.

    The controller wraps an arbitrary coroutine and:

    1. Fires optional warning callbacks at configurable intervals before the
       deadline.
    2. Extends the deadline when ``record_activity()`` has been called
       recently and the maximum number of extensions has not been reached.
    3. Cancels the wrapped coroutine and raises ``TimeoutError`` when the
       (possibly extended) deadline is exceeded without recent activity.

    ``record_activity()`` is synchronous and cheap -- designed to be called
    from a hot readline loop.
    """

    def __init__(
        self,
        cfg: TimeoutConfig,
        *,
        on_warning: Callable[[TimeoutWarning], Awaitable[None]] | None = None,
    ) -> None:
        self._cfg = cfg
        self._warning_intervals = sorted(cfg.warning_intervals, reverse=True)
        self._on_warning = on_warning

        self._extensions_used = 0
        self._last_activity: float = 0.0
        self._started_at: float = 0.0
        self._deadline: float = 0.0

    # ------------------------------------------------------------------
    # Public helpers
    # ------------------------------------------------------------------

    def record_activity(self) -> None:
        """Record that the subprocess produced output (non-async, fast)."""
        self._last_activity = time.monotonic()

    @property
    def remaining(self) -> float:
        """Seconds remaining until timeout (0 if not running)."""
        if self._deadline <= 0:
            return 0.0
        return max(0.0, self._deadline - time.monotonic())

    @property
    def timeout_seconds(self) -> float:
        """Configured base timeout in seconds."""
        return self._cfg.timeout_seconds

    @property
    def activity_extension_seconds(self) -> float:
        """Configured activity extension duration in seconds."""
        return self._cfg.activity_extension

    def begin(self) -> None:
        """Initialize timing state. Call once before the main operation."""
        self._started_at = time.monotonic()
        self._deadline = self._started_at + self._cfg.timeout_seconds
        self._last_activity = self._started_at

    def start_warning_loop(self) -> asyncio.Task[None] | None:
        """Start the background warning loop task.

        Returns the task (so the caller can cancel it), or ``None`` if no
        warnings are configured.
        """
        if self._warning_intervals and self._on_warning:
            return asyncio.create_task(self._warning_loop())
        return None

    def try_extend(self) -> bool:
        """Try to extend the deadline due to recent activity.

        Returns ``True`` if the deadline was extended, ``False`` if the
        timeout should fire (budget exhausted or no recent activity).
        """
        if not self._cfg.extend_on_activity:
            return False
        now = time.monotonic()

        # 0 means unlimited, and there the deadline is a pure idle timer: it
        # sits one window after the last sign of life, wherever that was. The
        # bounded rule below asks whether output appeared in the last 30
        # seconds *at the instant the deadline fires*, which kills an agent
        # that has been working for hours but happens to be inside a slow tool
        # call at the wrong second. For a turn that may legitimately run all
        # night, "N hours of silence" has to mean exactly that.
        if not self._cfg.max_extensions:
            idle = now - self._last_activity
            if idle >= self._cfg.activity_extension:
                return False
            self._extensions_used += 1
            self._deadline = self._last_activity + self._cfg.activity_extension
            logger.info(
                "Idle deadline moved: %.0fs since last output, %.0fs remaining",
                idle,
                self._deadline - now,
            )
            return True

        if self._extensions_used >= self._cfg.max_extensions:
            return False
        if now - self._last_activity > _ACTIVITY_RECENCY_SECONDS:
            return False
        self._extensions_used += 1
        self._deadline = now + self._cfg.activity_extension
        logger.info(
            "Timeout extended (%d/%d): +%.0fs",
            self._extensions_used,
            self._cfg.max_extensions,
            self._cfg.activity_extension,
        )
        return True

    # ------------------------------------------------------------------
    # Coroutine wrapper (for oneshot / non-generator use)
    # ------------------------------------------------------------------

    async def run_with_timeout(self, coro: Awaitable[T]) -> T:
        """Execute *coro* with managed timeout, warnings, and extensions.

        Use this for one-shot operations (e.g. ``process.communicate``).
        For streaming generators, use :meth:`begin` / :meth:`try_extend`
        directly in the read loop instead.
        """
        self.begin()

        main_task: asyncio.Task[T] = asyncio.ensure_future(coro)
        warning_task = self.start_warning_loop()

        try:
            while True:
                remaining = self._deadline - time.monotonic()
                if remaining <= 0 and not self.try_extend():
                    main_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await main_task
                    raise TimeoutError
                if remaining <= 0:
                    continue

                done, _ = await asyncio.wait(
                    {main_task},
                    timeout=min(remaining, 1.0),
                )
                if main_task in done:
                    return main_task.result()

                if time.monotonic() >= self._deadline and not self.try_extend():
                    main_task.cancel()
                    with contextlib.suppress(asyncio.CancelledError):
                        await main_task
                    raise TimeoutError
        finally:
            if warning_task and not warning_task.done():
                warning_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await warning_task
            if not main_task.done():
                main_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await main_task

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    async def _warning_loop(self) -> None:
        """Fire warning callbacks at the configured intervals before timeout."""
        warned: set[float] = set()
        try:
            while True:
                remaining = self._deadline - time.monotonic()
                if remaining <= 0:
                    return

                next_sleep = remaining
                for interval in self._warning_intervals:
                    if interval in warned:
                        continue
                    if remaining <= interval:
                        warned.add(interval)
                        if self._on_warning:
                            await self._on_warning(
                                TimeoutWarning(
                                    remaining_seconds=remaining,
                                    total_seconds=self._cfg.timeout_seconds,
                                    extensions_used=self._extensions_used,
                                )
                            )
                        break
                    time_until_warning = remaining - interval
                    next_sleep = min(next_sleep, time_until_warning)

                await asyncio.sleep(min(next_sleep, 0.5))
        except asyncio.CancelledError:
            return
