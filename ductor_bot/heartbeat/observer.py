"""Heartbeat observer: periodic background agent turns in the main session."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from ductor_bot.infra.base_observer import BaseObserver
from ductor_bot.log_context import set_log_context
from ductor_bot.utils.quiet_hours import check_quiet_hour

if TYPE_CHECKING:
    from ductor_bot.config import AgentConfig, HeartbeatConfig, HeartbeatTarget

logger = logging.getLogger(__name__)

# Callback signature: (chat_id, alert_text, topic_id)
HeartbeatResultCallback = Callable[[int, str, int | None], Awaitable[None]]

# Handler signature: (chat_id, topic_id, prompt_override, ack_token_override)
HeartbeatHandler = Callable[[int, int | None, str | None, str | None], Awaitable[str | None]]

# Validator signature: (chat_id) -> is_accessible
ChatValidator = Callable[[int], Awaitable[bool]]

_VALIDATION_TTL = 3600


class HeartbeatObserver(BaseObserver):
    """Sends periodic heartbeat prompts through the main session.

    Follows the CronObserver lifecycle pattern: start/stop with an asyncio
    background task. Results are delivered via a callback set by
    ``set_result_handler``.
    """

    def __init__(self, config: AgentConfig) -> None:
        super().__init__()
        self._config = config
        self._on_result: HeartbeatResultCallback | None = None
        self._handle_heartbeat: HeartbeatHandler | None = None
        self._is_chat_busy: Callable[[int], bool] | None = None
        self._stale_cleanup: Callable[[], Awaitable[int]] | None = None
        self._chat_validator: ChatValidator | None = None
        self._valid_targets: dict[int, float] = {}
        self._target_last_run: dict[tuple[int, int | None], float] = {}
        self._target_tasks: dict[tuple[int | None, int | None], asyncio.Task[None]] = {}

    @property
    def _hb(self) -> HeartbeatConfig:
        return self._config.heartbeat

    def set_result_handler(self, handler: HeartbeatResultCallback) -> None:
        """Set callback for delivering alert messages to the user."""
        self._on_result = handler

    def set_heartbeat_handler(self, handler: HeartbeatHandler) -> None:
        """Set the function that executes a heartbeat turn (orchestrator.handle_heartbeat)."""
        self._handle_heartbeat = handler

    def set_busy_check(self, check: Callable[[int], bool]) -> None:
        """Set the function that checks if a chat has active CLI processes."""
        self._is_chat_busy = check

    def set_stale_cleanup(self, cleanup: Callable[[], Awaitable[int]]) -> None:
        """Set the function that kills stale CLI processes (wall-clock based)."""
        self._stale_cleanup = cleanup

    def set_chat_validator(self, validator: ChatValidator) -> None:
        """Set the function that validates whether a group chat is accessible."""
        self._chat_validator = validator

    async def start(self) -> None:
        """Start the heartbeat background loop and per-target loops."""
        if not self._hb.enabled:
            logger.info("Heartbeat disabled in config")
            return
        if self._handle_heartbeat is None:
            logger.error("Heartbeat handler not set, cannot start")
            return
        await super().start()
        self._start_target_loops()
        logger.info(
            "Heartbeat started (every %dm, quiet %d:00-%d:00, %d group target(s))",
            self._hb.interval_minutes,
            self._hb.quiet_start,
            self._hb.quiet_end,
            sum(1 for t in self._hb.group_targets if t.enabled and t.chat_id is not None),
        )

    async def stop(self) -> None:
        """Stop the heartbeat background loop and all target loops."""
        for task in self._target_tasks.values():
            task.cancel()
        for task in self._target_tasks.values():
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._target_tasks.clear()
        await super().stop()
        logger.info("Heartbeat stopped")

    def _start_target_loops(self) -> None:
        """Launch independent loops for group targets with custom intervals."""
        for target in self._hb.group_targets:
            if not target.enabled or target.chat_id is None:
                continue
            interval = target.interval_minutes or self._hb.interval_minutes
            if interval == self._hb.interval_minutes and target.interval_minutes is None:
                continue  # No custom interval → runs with global tick
            key = (target.chat_id, target.topic_id)
            task = asyncio.create_task(self._target_loop(target, interval))
            task.add_done_callback(lambda _: None)
            self._target_tasks[key] = task
            logger.info(
                "Heartbeat target %d/%s started (every %dm)",
                target.chat_id,
                target.topic_id,
                interval,
            )

    async def _target_loop(self, target: HeartbeatTarget, interval_minutes: int) -> None:
        """Independent loop for a single group target."""
        assert target.chat_id is not None
        try:
            while self._running:
                await asyncio.sleep(interval_minutes * 60)
                if not self._running or not self._hb.enabled:
                    continue
                if not target.enabled:
                    continue
                if not await self._validate_target(target.chat_id):
                    continue
                prompt, ack_token, quiet_start, quiet_end = self._resolve_target_settings(target)
                if self._is_target_quiet(target.chat_id, quiet_start, quiet_end):
                    continue
                await self._run_for_chat(
                    target.chat_id,
                    target.topic_id,
                    prompt=prompt,
                    ack_token=ack_token,
                    quiet_start=quiet_start,
                    quiet_end=quiet_end,
                )
        except asyncio.CancelledError:
            logger.debug("Target loop %d cancelled", target.chat_id)

    def _resolve_target_settings(self, target: HeartbeatTarget) -> tuple[str, str, int, int]:
        """Resolve per-target settings with global fallback.

        Returns ``(prompt, ack_token, quiet_start, quiet_end)``.
        """
        prompt = target.prompt or self._hb.prompt
        ack_token = target.ack_token or self._hb.ack_token
        quiet_start = target.quiet_start if target.quiet_start is not None else self._hb.quiet_start
        quiet_end = target.quiet_end if target.quiet_end is not None else self._hb.quiet_end
        return prompt, ack_token, quiet_start, quiet_end

    async def _validate_target(self, chat_id: int) -> bool:
        """Check if a group target is accessible, with TTL cache."""
        if self._chat_validator is None:
            return True

        now = time.time()
        last = self._valid_targets.get(chat_id)
        if last is not None and (now - last) < _VALIDATION_TTL:
            return True

        try:
            valid = await self._chat_validator(chat_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning("Chat validation failed for %d", chat_id)
            return False

        if valid:
            self._valid_targets[chat_id] = now
            return True

        logger.warning("Heartbeat target %d is not accessible, skipping", chat_id)
        return False

    def _should_skip_target_interval(self, target: HeartbeatTarget, now: float) -> bool:
        """Return True if the target has a custom interval that has not yet elapsed."""
        if target.interval_minutes is None:
            return False
        key = (target.chat_id or 0, target.topic_id)
        last_run = self._target_last_run.get(key, 0.0)
        if (now - last_run) < target.interval_minutes * 60:
            logger.debug(
                "Heartbeat target %s skipped: custom interval not elapsed",
                target.chat_id,
            )
            return True
        return False

    async def _run(self) -> None:
        """Sleep -> check -> execute -> repeat."""
        last_wall = time.time()
        try:
            while self._running:
                interval = self._hb.interval_minutes * 60
                await asyncio.sleep(interval)
                if not self._running or not self._hb.enabled:
                    continue

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

    async def _cleanup_stale(self) -> None:
        """Kill stale CLI processes (catches suspend hangovers)."""
        if not self._stale_cleanup:
            return
        try:
            killed = await self._stale_cleanup()
            if killed:
                logger.info("Cleaned up %d stale process(es)", killed)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Stale process cleanup failed")

    async def _tick(self) -> None:
        """Run one heartbeat cycle for all allowed users and group targets."""
        await self._cleanup_stale()

        is_quiet, now_hour, tz = check_quiet_hour(
            quiet_start=self._hb.quiet_start,
            quiet_end=self._hb.quiet_end,
            user_timezone=self._config.user_timezone,
            global_quiet_start=self._hb.quiet_start,
            global_quiet_end=self._hb.quiet_end,
        )
        if is_quiet:
            logger.debug("Heartbeat skipped: quiet hours (%d:00 %s)", now_hour, tz.key)
            return

        target_count = len(self._config.allowed_user_ids) + len(self._hb.group_targets)
        logger.debug("Heartbeat tick: checking %d chat(s)", target_count)

        for chat_id in self._config.allowed_user_ids:
            await self._run_for_chat(chat_id)

        await self._tick_group_targets()

    async def _tick_group_targets(self) -> None:
        """Iterate group targets that DON'T have their own loop (no custom interval)."""
        for target in self._hb.group_targets:
            if not target.enabled or target.chat_id is None:
                continue
            # Targets with custom intervals run in their own loop
            if (target.chat_id, target.topic_id) in self._target_tasks:
                continue
            if not await self._validate_target(target.chat_id):
                continue

            prompt, ack_token, quiet_start, quiet_end = self._resolve_target_settings(target)
            await self._run_for_chat(
                target.chat_id,
                target.topic_id,
                prompt=prompt,
                ack_token=ack_token,
                quiet_start=quiet_start,
                quiet_end=quiet_end,
            )

    def _is_target_quiet(
        self, chat_id: int, quiet_start: int | None, quiet_end: int | None
    ) -> bool:
        """Check per-target quiet hours. Returns True if quiet."""
        if quiet_start is None or quiet_end is None:
            return False
        is_quiet, _hour, _tz = check_quiet_hour(
            quiet_start=quiet_start,
            quiet_end=quiet_end,
            user_timezone=self._config.user_timezone,
            global_quiet_start=self._hb.quiet_start,
            global_quiet_end=self._hb.quiet_end,
        )
        if is_quiet:
            logger.debug("Heartbeat skipped for %d: per-target quiet hours", chat_id)
        return is_quiet

    async def _run_for_chat(  # noqa: PLR0913
        self,
        chat_id: int,
        topic_id: int | None = None,
        *,
        prompt: str | None = None,
        ack_token: str | None = None,
        quiet_start: int | None = None,
        quiet_end: int | None = None,
    ) -> None:
        """Execute a single heartbeat for one chat."""
        set_log_context(operation="hb", chat_id=chat_id)

        if self._is_target_quiet(chat_id, quiet_start, quiet_end):
            return

        if self._is_chat_busy and self._is_chat_busy(chat_id):
            logger.debug("Heartbeat skipped: chat is busy")
            return

        if self._handle_heartbeat is None:
            return

        try:
            alert_text = await self._handle_heartbeat(chat_id, topic_id, prompt, ack_token)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Heartbeat execution error")
            return

        if alert_text is None:
            return

        if self._on_result:
            try:
                await self._on_result(chat_id, alert_text, topic_id)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Heartbeat result delivery error")
