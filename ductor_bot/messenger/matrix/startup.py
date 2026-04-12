"""Matrix-specific startup sequence.

Reuses orchestrator creation from the core but skips Telegram-specific
parts (bot username lookup, command registration, group audit).
Startup lifecycle (sentinel, startup-kind notification, recovery) mirrors
the Telegram startup for parity.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING

from ductor_bot.i18n import t
from ductor_bot.infra.restart import consume_restart_marker, consume_restart_sentinel

if TYPE_CHECKING:
    from ductor_bot.messenger.matrix.bot import MatrixBot

logger = logging.getLogger(__name__)


async def run_matrix_startup(bot: MatrixBot) -> None:
    """Matrix-specific startup: orchestrator, observers, recovery.

    When ``bot._orchestrator`` is already set (secondary transport mode),
    orchestrator creation and all primary-only steps are skipped.
    """
    primary = bot._orchestrator is None

    if primary:
        from ductor_bot.orchestrator.core import Orchestrator

        bot._orchestrator = await Orchestrator.create(bot._config, agent_name=bot._agent_name)

        # Wire all observers + injector to bus in one call
        bot._orchestrator.wire_observers_to_bus(bot._bus)

        # Handle restart sentinel (explicit restart via /restart command)
        sentinel = await _handle_restart_sentinel(bot)

        # Handle restart marker (restart-requested file)
        restart_reason = _consume_restart_marker(bot)
        if restart_reason and sentinel is None:
            await bot.notification_service.notify_all(
                t("startup.matrix_restart", reason=restart_reason)
            )

        # Startup kind detection + notification (first_start, reboot)
        await _handle_startup_lifecycle(bot, sentinel)

        # Recovery of interrupted work
        await _handle_recovery(bot)

        # Update checker
        try:
            from ductor_bot.infra.install import is_upgradeable
            from ductor_bot.infra.updater import UpdateObserver
            from ductor_bot.infra.version import VersionInfo

            if is_upgradeable() and bot._config.update_check and bot._agent_name == "main":

                async def _on_update(info: VersionInfo) -> None:
                    await bot.notification_service.notify_all(
                        t("startup.matrix_update", version=info.latest)
                    )

                bot._update_observer = UpdateObserver(notify=_on_update)
                bot._update_observer.start()
        except ImportError:
            pass

    logger.info(
        "Matrix bot online: %s on %s",
        bot._config.matrix.user_id,
        bot._config.matrix.homeserver,
    )

    # Run registered startup hooks (supervisor injection)
    for hook in bot._startup_hooks:
        await hook()


async def _handle_restart_sentinel(bot: MatrixBot) -> dict[str, object] | None:
    """Consume and handle the restart sentinel file. Returns sentinel dict or None."""
    sentinel_path = bot._orchestrator.paths.ductor_home / "restart-sentinel.json"
    sentinel = await asyncio.to_thread(consume_restart_sentinel, sentinel_path=sentinel_path)
    if sentinel:
        chat_id = int(sentinel.get("chat_id", 0))
        msg = str(sentinel.get("message", t("startup.restart_default")))
        if chat_id:
            await bot.notification_service.notify(chat_id, msg)
    return sentinel


def _consume_restart_marker(bot: MatrixBot) -> str:
    """Check and consume restart marker."""
    paths_obj = bot._orchestrator.paths if bot._orchestrator else None
    if paths_obj is None:
        return ""
    marker_path = paths_obj.ductor_home / "restart-requested"
    if consume_restart_marker(marker_path=marker_path):
        return "restart marker"
    return ""


async def _handle_startup_lifecycle(
    bot: MatrixBot, sentinel: dict[str, object] | None
) -> None:
    """Detect startup kind and send notification for first_start/reboot."""
    from ductor_bot.infra.startup_state import detect_startup_kind, save_startup_state
    from ductor_bot.text.response_format import startup_notification_text

    startup_info = await asyncio.to_thread(
        detect_startup_kind, bot._orchestrator.paths.startup_state_path
    )
    await asyncio.to_thread(
        save_startup_state, bot._orchestrator.paths.startup_state_path, startup_info
    )
    if sentinel is None and startup_info.kind.value != "service_restart":
        note = startup_notification_text(startup_info.kind.value)
        if note:
            await bot.notification_service.notify_all(note)


async def _handle_recovery(bot: MatrixBot) -> None:
    """Recover interrupted work (in-flight turns, named sessions)."""
    from ductor_bot.infra.recovery import RecoveryPlanner
    from ductor_bot.text.response_format import recovery_notification_text

    planner = RecoveryPlanner(
        inflight=bot._orchestrator.inflight_tracker,
        named_sessions=bot._orchestrator.named_sessions.pop_recovered_running(),
        max_age_seconds=bot._config.timeouts.normal * 2,
    )
    for action in planner.plan():
        note = recovery_notification_text(action.kind, action.prompt_preview, action.session_name)
        await bot.notification_service.notify(action.chat_id, note)
        if action.kind == "named_session" and action.session_name:
            with contextlib.suppress(Exception):
                bot._orchestrator.submit_named_followup_bg(
                    action.chat_id,
                    action.session_name,
                    action.prompt_preview,
                    message_id=0,
                    thread_id=None,
                )
    bot._orchestrator.inflight_tracker.clear()
