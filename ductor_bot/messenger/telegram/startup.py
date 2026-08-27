"""Bot startup lifecycle: orchestrator creation, recovery, sentinel handling."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import TYPE_CHECKING

from ductor_bot.i18n import t
from ductor_bot.infra.restart import consume_restart_sentinel
from ductor_bot.infra.updater import UpdateObserver, consume_upgrade_sentinel
from ductor_bot.infra.version import get_current_version

if TYPE_CHECKING:
    from ductor_bot.messenger.telegram.app import TelegramBot

logger = logging.getLogger(__name__)


async def _handle_restart_sentinel(bot: TelegramBot) -> dict[str, object] | None:
    """Consume and handle the restart sentinel file. Returns sentinel dict or None."""
    sentinel_path = bot._orch.paths.ductor_home / "restart-sentinel.json"
    sentinel = await asyncio.to_thread(consume_restart_sentinel, sentinel_path=sentinel_path)
    if sentinel:
        chat_id = int(sentinel.get("chat_id", 0))
        msg = str(sentinel.get("message", t("startup.restart_default")))
        if chat_id:
            await bot.notification_service.notify(chat_id, msg)
    return sentinel


async def _handle_recovery(bot: TelegramBot, sentinel: dict[str, object] | None) -> None:
    """Handle upgrade sentinel, startup lifecycle, and auto-recovery of interrupted work."""
    upgrade = await asyncio.to_thread(consume_upgrade_sentinel, bot._orch.paths.ductor_home)
    if upgrade:
        uid = int(upgrade.get("chat_id", 0))
        old_v = upgrade.get("old_version", "?")
        new_v = upgrade.get("new_version", get_current_version())
        if uid:
            await bot.notification_service.notify(
                uid, t("startup.upgrade_complete", old=old_v, new=new_v)
            )

    from ductor_bot.infra.startup_state import detect_startup_kind, save_startup_state
    from ductor_bot.text.response_format import startup_notification_text

    startup_info = await asyncio.to_thread(detect_startup_kind, bot._orch.paths.startup_state_path)
    await asyncio.to_thread(save_startup_state, bot._orch.paths.startup_state_path, startup_info)
    if sentinel is None and startup_info.kind.value != "service_restart":
        note = startup_notification_text(startup_info.kind.value)
        if note:
            await bot.notify_startup(note)

    from ductor_bot.infra.recovery import RecoveryPlanner
    from ductor_bot.text.response_format import recovery_notification_text

    planner = RecoveryPlanner(
        inflight=bot._orch.inflight_tracker,
        named_sessions=bot._orch.named_sessions.pop_recovered_running(),
        max_age_seconds=bot.config.timeouts.normal * 2,
    )
    for action in planner.plan():
        note = recovery_notification_text(action.kind, action.prompt_preview, action.session_name)
        await bot.notification_service.notify(action.chat_id, note)
        if action.kind == "named_session" and action.session_name:
            with contextlib.suppress(Exception):
                bot._orch.submit_named_followup_bg(
                    action.chat_id,
                    action.session_name,
                    action.prompt_preview,
                    message_id=0,
                    thread_id=None,
                )
    bot._orch.inflight_tracker.clear()


async def _run_primary_startup(bot: TelegramBot) -> None:
    """Create orchestrator, handle sentinels, recovery, and update observer.

    Called only when the bot is the primary transport (owns the orchestrator).
    """
    from ductor_bot.orchestrator.core import Orchestrator

    bot._orchestrator = await Orchestrator.create(
        bot.config,
        agent_name=bot._agent_name,
    )

    from ductor_bot.messenger.telegram.chat_tracker import ChatTracker

    bot._chat_tracker = ChatTracker(bot._orch.paths.chat_activity_path)

    # Seed topic name cache from persisted sessions and wire the resolver.
    all_sessions = await bot._orch._sessions.list_all()
    seeded = bot._topic_names.seed_from_sessions(all_sessions)
    if seeded:
        logger.info("Topic name cache seeded with %d name(s)", seeded)
    bot._orch._sessions.set_topic_name_resolver(bot._topic_names.resolve)

    sentinel = await _handle_restart_sentinel(bot)

    bot._orch.wire_observers_to_bus(bot._bus, wake_handler=bot._handle_webhook_wake)
    bot._orchestrator.set_config_hot_reload_handler(bot._on_auth_hot_reload)

    async def _validate_chat(chat_id: int) -> bool:
        try:
            await bot.bot_instance.get_chat(chat_id)
        except Exception:
            return False
        else:
            return True

    bot._orch._observers.heartbeat.set_chat_validator(_validate_chat)

    await _handle_recovery(bot, sentinel)

    # Start background version checker (skip for dev/source installs)
    from ductor_bot.infra.install import is_upgradeable

    if is_upgradeable() and bot.config.update_check and bot._agent_name == "main":
        bot._update_observer = UpdateObserver(notify=bot._on_update_available)
        bot._update_observer.start()


async def run_startup(bot: TelegramBot) -> None:
    """Execute full startup sequence: orchestrator, sentinels, recovery, update observer.

    When ``bot._orchestrator`` is already set (secondary transport mode),
    orchestrator creation and all primary-only steps are skipped.
    """
    if bot._orchestrator is None:
        await _run_primary_startup(bot)

    me = await bot.bot_instance.get_me()
    bot._bot_id = me.id
    bot._bot_username = (me.username or "").lower()
    logger.info("Bot online: @%s (id=%d)", me.username, me.id)

    await bot._sync_commands()
    bot._restart_watcher = asyncio.create_task(bot._watch_restart_marker())

    await _ensure_managed_topics(bot)

    # Audit groups on startup and start periodic 24h check
    await bot.audit_groups()
    bot._group_audit_task = asyncio.create_task(bot._run_group_audit_loop())


async def _ensure_managed_topics(bot: TelegramBot) -> None:
    """Create or verify the Consult topic and pinned notices in each group.

    Runs on every start, not only the first: the state file records what was
    created, and an edit to the pinned notice is what proves it still exists.
    """
    if not bot.config.managed_topics or bot._agent_name != "main":
        return

    from ductor_bot.messenger.telegram.managed_topics import (
        ManagedTopicStore,
        ensure_managed_topics,
    )

    paths = bot._orch.paths
    store = ManagedTopicStore(paths.managed_topics_path)
    for chat_id in bot.config.allowed_group_ids:
        await ensure_managed_topics(bot.bot_instance, chat_id, store, paths.consult_dir)
