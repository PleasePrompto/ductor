"""Slack-specific startup sequence."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING

from ductor_bot.i18n import t
from ductor_bot.infra.restart import consume_restart_marker, consume_restart_sentinel

if TYPE_CHECKING:
    from ductor_bot.messenger.slack.bot import SlackBot

logger = logging.getLogger(__name__)


async def run_slack_startup(bot: SlackBot) -> None:
    """Slack startup mirrors Matrix startup with Slack-specific logging."""
    primary = bot._orchestrator is None

    if primary:
        from ductor_bot.orchestrator.core import Orchestrator

        bot._orchestrator = await Orchestrator.create(bot._config, agent_name=bot._agent_name)
        bot._orchestrator.wire_observers_to_bus(bot._bus)

        sentinel = await _handle_restart_sentinel(bot)
        restart_reason = _consume_restart_marker(bot)
        if restart_reason and sentinel is None:
            await bot.notify_startup(f"**Bot restarted** ({restart_reason})")

        await _handle_startup_lifecycle(bot, sentinel)
        await _handle_recovery(bot)

        try:
            from ductor_bot.infra.install import is_upgradeable
            from ductor_bot.infra.updater import UpdateObserver
            from ductor_bot.infra.version import VersionInfo

            if is_upgradeable() and bot._config.update_check and bot._agent_name == "main":

                async def _on_update(info: VersionInfo) -> None:
                    await bot.notify_upgrade(
                        f"**Update available:** `{info.latest}`\nUse `/upgrade` to update."
                    )

                bot._update_observer = UpdateObserver(notify=_on_update)
                bot._update_observer.start()
        except ImportError:
            pass

    logger.info("Slack bot online: %s", bot.bot_name)

    for hook in bot._startup_hooks:
        await hook()


async def _handle_restart_sentinel(bot: SlackBot) -> dict[str, object] | None:
    if bot._orchestrator is None:
        return None
    sentinel_path = bot._orchestrator.paths.ductor_home / "restart-sentinel.json"
    sentinel = await asyncio.to_thread(consume_restart_sentinel, sentinel_path=sentinel_path)
    if sentinel:
        chat_id = int(sentinel.get("chat_id", 0))
        msg = str(sentinel.get("message", t("startup.restart_default")))
        if chat_id:
            await bot.notification_service.notify(chat_id, msg)
    return sentinel


def _consume_restart_marker(bot: SlackBot) -> str:
    paths_obj = bot._orchestrator.paths if bot._orchestrator else None
    if paths_obj is None:
        return ""
    marker_path = paths_obj.ductor_home / "restart-requested"
    if consume_restart_marker(marker_path=marker_path):
        return "restart marker"
    return ""


async def _handle_startup_lifecycle(bot: SlackBot, sentinel: dict[str, object] | None) -> None:
    from ductor_bot.infra.startup_state import detect_startup_kind, save_startup_state
    from ductor_bot.text.response_format import startup_notification_text

    if bot._orchestrator is None:
        return
    startup_state_path = bot._orchestrator.paths.startup_state_path
    startup_info = await asyncio.to_thread(detect_startup_kind, startup_state_path)
    await asyncio.to_thread(save_startup_state, startup_state_path, startup_info)
    if sentinel is None and startup_info.kind.value != "service_restart":
        note = startup_notification_text(startup_info.kind.value)
        if note:
            await bot.notify_startup(note)


async def _handle_recovery(bot: SlackBot) -> None:
    from ductor_bot.infra.recovery import RecoveryPlanner
    from ductor_bot.text.response_format import recovery_notification_text

    orch = bot._orchestrator
    if orch is None:
        return
    planner = RecoveryPlanner(
        inflight=orch.inflight_tracker,
        named_sessions=orch.named_sessions.pop_recovered_running(),
        max_age_seconds=bot._config.timeouts.normal * 2,
    )
    for action in planner.plan():
        note = recovery_notification_text(action.kind, action.prompt_preview, action.session_name)
        await bot.notification_service.notify(action.chat_id, note)
        if action.kind == "named_session" and action.session_name:
            with contextlib.suppress(Exception):
                orch.submit_named_followup_bg(
                    action.chat_id,
                    action.session_name,
                    action.prompt_preview,
                    message_id=0,
                    thread_id=None,
                )
    orch.inflight_tracker.clear()
