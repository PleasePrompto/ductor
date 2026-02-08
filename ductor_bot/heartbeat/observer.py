"""Heartbeat observer: periodic background agent turns in the main session."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import TYPE_CHECKING

from ductor_bot.config import resolve_user_timezone
from ductor_bot.log_context import set_log_context

if TYPE_CHECKING:
    from ductor_bot.config import AgentConfig, HeartbeatConfig

logger = logging.getLogger(__name__)

# Callback signature: (chat_id, alert_text)
HeartbeatResultCallback = Callable[[int, str], Awaitable[None]]


def is_quiet_hour(now_hour: int, quiet_start: int, quiet_end: int) -> bool:
    """Check if *now_hour* falls within the quiet window.

    Handles wrap-around: quiet_start=21, quiet_end=8 means 21-23 and 0-7 are quiet.
    """
    if quiet_start <= quiet_end:
        return quiet_start <= now_hour < quiet_end
    return now_hour >= quiet_start or now_hour < quiet_end


class HeartbeatObserver:
    """Sends periodic heartbeat prompts through the main session.

    Follows the CronObserver lifecycle pattern: start/stop with an asyncio
    background task. Results are delivered via a callback set by
    ``set_result_handler``.
    """

    def __init__(self, config: AgentConfig) -> None:
        self._config = config
        self._on_result: HeartbeatResultCallback | None = None
        self._handle_heartbeat: Callable[[int], Awaitable[str | None]] | None = None
        self._is_chat_busy: Callable[[int], bool] | None = None
        self._stale_cleanup: Callable[[], Awaitable[int]] | None = None
        self._task: asyncio.Task[None] | None = None
        self._running = False

    @property
    def _hb(self) -> HeartbeatConfig:
        return self._config.heartbeat

    def set_result_handler(self, handler: HeartbeatResultCallback) -> None:
        """Set callback for delivering alert messages to the user."""
        self._on_result = handler

    def set_heartbeat_handler(
        self,
        handler: Callable[[int], Awaitable[str | None]],
    ) -> None:
        """Set the function that executes a heartbeat turn (orchestrator.handle_heartbeat)."""
        self._handle_heartbeat = handler

    def set_busy_check(self, check: Callable[[int], bool]) -> None:
        """Set the function that checks if a chat has active CLI processes."""
        self._is_chat_busy = check

    def set_stale_cleanup(self, cleanup: Callable[[], Awaitable[int]]) -> None:
        """Set the function that kills stale CLI processes (wall-clock based)."""
        self._stale_cleanup = cleanup

    async def start(self) -> None:
        """Start the heartbeat background loop."""
        if not self._hb.enabled:
            logger.info("Heartbeat disabled in config")
            return
        if self._handle_heartbeat is None:
            logger.error("Heartbeat handler not set, cannot start")
            return
        self._running = True
        self._task = asyncio.create_task(self._loop())
        self._task.add_done_callback(_log_task_crash)
        logger.info(
            "Heartbeat started (every %dm, quiet %d:00-%d:00)",
            self._hb.interval_minutes,
            self._hb.quiet_start,
            self._hb.quiet_end,
        )

    async def stop(self) -> None:
        """Stop the heartbeat background loop."""
        self._running = False
        if self._task:
            task = self._task
            self._task = None
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        logger.info("Heartbeat stopped")

    async def _loop(self) -> None:
        """Sleep -> check -> execute -> repeat."""
        interval = self._hb.interval_minutes * 60
        last_wall = time.time()
        try:
            while self._running:
                await asyncio.sleep(interval)
                if not self._running or not self._hb.enabled:
                    continue

                # Detect system suspend via wall-clock gap.
                now_wall = time.time()
                wall_elapsed = now_wall - last_wall
                if wall_elapsed > interval * 2:
                    logger.warning(
                        "Wall-clock gap: %.0fs (expected ~%ds) -- system likely suspended",
                        wall_elapsed,
                        interval,
                    )
                last_wall = now_wall

                try:
                    await self._tick()
                except asyncio.CancelledError:
                    raise
                except Exception:
                    logger.exception("Heartbeat tick failed (continuing)")
        except asyncio.CancelledError:
            logger.debug("Heartbeat loop cancelled")

    async def _tick(self) -> None:
        """Run one heartbeat cycle for all allowed users."""
        # Cleanup stale processes first (catches suspend hangovers).
        if self._stale_cleanup:
            try:
                killed = await self._stale_cleanup()
                if killed:
                    logger.info("Cleaned up %d stale process(es)", killed)
            except Exception:
                logger.exception("Stale process cleanup failed")

        tz = resolve_user_timezone(self._config.user_timezone)
        now_hour = datetime.now(tz).hour
        if is_quiet_hour(now_hour, self._hb.quiet_start, self._hb.quiet_end):
            logger.debug("Heartbeat skipped: quiet hours (%d:00 %s)", now_hour, tz.key)
            return

        logger.debug("Heartbeat tick: checking %d chat(s)", len(self._config.allowed_user_ids))
        for chat_id in self._config.allowed_user_ids:
            await self._run_for_chat(chat_id)

    async def _run_for_chat(self, chat_id: int) -> None:
        """Execute a single heartbeat for one chat."""
        set_log_context(operation="hb", chat_id=chat_id)

        if self._is_chat_busy and self._is_chat_busy(chat_id):
            logger.debug("Heartbeat skipped: chat is busy")
            return

        if self._handle_heartbeat is None:
            return

        try:
            alert_text = await self._handle_heartbeat(chat_id)
        except Exception:
            logger.exception("Heartbeat execution error")
            return

        if alert_text is None:
            return

        if self._on_result:
            try:
                await self._on_result(chat_id, alert_text)
            except Exception:
                logger.exception("Heartbeat result delivery error")


def _log_task_crash(task: asyncio.Task[None]) -> None:
    """Log if the heartbeat background task crashes unexpectedly."""
    if task.cancelled():
        return
    exc = task.exception()
    if exc is not None:
        logger.error("Heartbeat loop crashed: %s", exc, exc_info=exc)
