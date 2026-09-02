"""Telegram bot: aiogram 3.x frontend for the orchestrator."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import shutil
import tempfile
from collections.abc import Awaitable, Callable
from functools import partial
from pathlib import Path
from typing import TYPE_CHECKING

from aiogram import Bot, Dispatcher, F, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import (
    BotCommand,
    ChatMemberUpdated,
    FSInputFile,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ReplyParameters,
)

from ductor_bot.bus.bus import MessageBus
from ductor_bot.bus.lock_pool import LockPool
from ductor_bot.commands import BOT_COMMANDS as _COMMAND_DEFS
from ductor_bot.commands import MULTIAGENT_SUB_COMMANDS as _MA_SUB_DEFS
from ductor_bot.commands import PICKER_COMMANDS as _PICKER_DEFS
from ductor_bot.config import AgentConfig
from ductor_bot.files.allowed_roots import resolve_allowed_roots
from ductor_bot.files.archive import (
    MAX_ENTRIES,
    MAX_TOTAL_BYTES,
    extract_archive,
    inspect_archive,
)
from ductor_bot.files.edits import ClipboardStore, EditStore
from ductor_bot.files.uploads import UploadSession, UploadStore
from ductor_bot.handoff.readiness import check_readiness
from ductor_bot.i18n import t
from ductor_bot.infra.restart import EXIT_RESTART, consume_restart_marker
from ductor_bot.infra.updater import UpdateObserver
from ductor_bot.infra.version import VersionInfo, get_current_version
from ductor_bot.log_context import set_log_context
from ductor_bot.messenger.notifications import NotificationService
from ductor_bot.messenger.telegram.callbacks import (
    button_grid_to_markup,
    edit_selector_response,
    mark_button_choice,
    parse_ns_callback,
)
from ductor_bot.messenger.telegram.chat_tracker import ChatRecord, ChatTracker
from ductor_bot.messenger.telegram.file_browser import (
    BrowserSession,
    build_name_confirmation,
    build_staging_view,
    file_browser_start,
    handle_file_browser_callback,
    is_file_browser_callback,
)
from ductor_bot.messenger.telegram.formatting import markdown_to_telegram_html
from ductor_bot.messenger.telegram.handlers import (
    build_reply_prompt,
    handle_abort,
    handle_abort_all,
    handle_command,
    handle_interrupt,
    prepend_reply_to_media,
    strip_mention,
)
from ductor_bot.messenger.telegram.media import (
    download_media,
    has_media,
    is_command_for_others,
    is_message_addressed,
    resolve_media_text,
    should_drop_in_group,
)
from ductor_bot.messenger.telegram.menu import (
    MENU_ITEMS,
    MNU_BACK,
    MNU_CLOSE,
    build_menu,
    build_toggle_panel,
    is_menu_callback,
    state_subtitle,
)
from ductor_bot.messenger.telegram.menu import (
    parse_callback as parse_menu_callback,
)
from ductor_bot.messenger.telegram.message_dispatch import (
    NonStreamingDispatch,
    StreamingDispatch,
    run_non_streaming_message,
    run_streaming_message,
)
from ductor_bot.messenger.telegram.middleware import (
    MQ_PREFIX,
    AuthMiddleware,
    SequentialMiddleware,
)
from ductor_bot.messenger.telegram.sender import SendRichOpts, send_rich
from ductor_bot.messenger.telegram.sender import (
    send_files_from_text as _send_files_from_text,
)
from ductor_bot.messenger.telegram.stop_button import is_stop_callback
from ductor_bot.messenger.telegram.topic import (
    TopicNameCache,
    get_session_key,
    get_thread_id,
    is_general_thread,
)
from ductor_bot.messenger.telegram.typing import TypingContext as _TypingContext
from ductor_bot.messenger.telegram.welcome import (
    build_welcome_keyboard,
    build_welcome_text,
    get_welcome_button_label,
    is_welcome_callback,
    resolve_welcome_callback,
)
from ductor_bot.multiagent.bus import AsyncInterAgentResult
from ductor_bot.session.key import SessionKey
from ductor_bot.text.response_format import SEP, fmt
from ductor_bot.workspace.paths import DuctorPaths

if TYPE_CHECKING:
    from aiogram.types import CallbackQuery, Message

    from ductor_bot.orchestrator.core import Orchestrator

logger = logging.getLogger(__name__)

_WELCOME_IMAGE = Path(__file__).resolve().parent / "ductor_images" / "welcome.png"
_CAPTION_LIMIT = 1024

# Backward-compatible patch points used by tests.
TypingContext = _TypingContext
send_files_from_text = _send_files_from_text

# Built at import as well as in _rebuild_commands(): _sync_commands() may run
# before a rebuild, and the initial value is what gets published then.
_BOT_COMMANDS: list[BotCommand] = [
    BotCommand(command=cmd, description=desc) for cmd, desc in _PICKER_DEFS
]

_CMD_DESC: dict[str, str] = {**dict(_COMMAND_DEFS), **dict(_MA_SUB_DEFS)}


def _rebuild_commands() -> None:
    """Rebuild module-level command lists from current translations."""
    global _BOT_COMMANDS  # noqa: PLW0603
    from ductor_bot.commands import (
        get_bot_commands,
        get_multiagent_sub_commands,
        get_picker_commands,
    )

    cmd_defs = get_bot_commands()
    ma_defs = get_multiagent_sub_commands()
    # The picker is trimmed; _CMD_DESC below stays complete so /help still
    # documents every command, including the ones the menu covers.
    _BOT_COMMANDS = [BotCommand(command=cmd, description=desc) for cmd, desc in get_picker_commands()]
    _CMD_DESC.clear()
    _CMD_DESC.update({**dict(cmd_defs), **dict(ma_defs)})


def _help_line(command: str) -> str:
    """Return one command line for the help panel."""
    description = _CMD_DESC.get(command, "")
    return f"/{command} -- {description}" if description else f"/{command}"


def _build_help_text() -> str:
    return fmt(
        t("help.header"),
        SEP,
        f"{t('help.cat_daily')}\n{_help_line('clear')}\n{_help_line('compact')}\n"
        f"{_help_line('handoff')}\n{_help_line('stop')}\n"
        f"{_help_line('interrupt')}\n{_help_line('stop_all')}\n"
        f"{_help_line('model')}\n{_help_line('effort')}\n{_help_line('account')}\n"
        f"{_help_line('persona')}\n{_help_line('folder')}\n{_help_line('consult')}\n"
        f"{_help_line('status')}\n{_help_line('memory')}",
        f"{t('help.cat_automation')}\n{_help_line('session')}\n{_help_line('cron')}",
        f"{t('help.cat_multiagent')}\n{_help_line('agent_commands')}",
        f"{t('help.cat_browse')}\n{_help_line('where')}\n{_help_line('leave')}\n"
        f"{_help_line('files')}\n{_help_line('menu')}\n{_help_line('skills')}\n"
        f"{_help_line('info')}\n{_help_line('help')}",
        f"{t('help.cat_maintenance')}\n{_help_line('diagnose')}\n{_help_line('upgrade')}\n{_help_line('restart')}",
        SEP,
        t("help.footer"),
    )


async def _cancel_task(task: asyncio.Task[None] | None) -> None:
    """Cancel an asyncio task and suppress CancelledError."""
    if task and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task



def _rendered_dir(paths, project_roots, target: Path, notice: str):  # noqa: ANN001, ANN202
    """Directory view plus a result line, for editing a message in place."""
    from ductor_bot.messenger.telegram.file_browser import render_dir_with_notice

    return render_dir_with_notice(paths, project_roots, target, notice)


def _place(src: Path, dest: Path) -> None:
    """Move *src* to *dest*, creating the parent. Blocking; call in a thread."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(src), str(dest))


def _short_path(target: Path, home: Path) -> str:
    """A path the user can place at a glance, without the absolute prefix."""
    try:
        return str(target.relative_to(home))
    except ValueError:
        return str(target)

class TelegramNotificationService:
    """NotificationService implementation for Telegram."""

    def __init__(self, bot: Bot, config: AgentConfig) -> None:
        self._bot = bot
        self._config = config

    async def notify(self, chat_id: int, text: str) -> None:
        await send_rich(self._bot, chat_id, text, None)

    async def notify_all(self, text: str) -> None:
        for uid in self._config.allowed_user_ids:
            try:
                await send_rich(self._bot, uid, text, None)
            except TelegramAPIError:
                # An unreachable recipient (e.g. never pressed /start -> "chat
                # not found") must not abort the notification fan-out — or, at
                # startup, the whole boot.
                logger.warning("Notification to user %d failed, skipping", uid)


#: Callback data for the readiness gate's Retry button. Short on purpose:
#: Telegram caps callback data at 64 bytes.
HANDOFF_RETRY = "hor"

#: Commands that consolidate the handoff first, which is a full model turn.
#: Without an acknowledgement the screen does not change and the button gets
#: pressed repeatedly, queueing several of them.
_SLOW_COMMANDS = {
    "/compact": "handoff.compacting",
    "/clear": "handoff.clearing",
}


def _retry_keyboard() -> InlineKeyboardMarkup:
    """A single Retry button for the readiness gate."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text=t("handoff.btn_retry"), callback_data=HANDOFF_RETRY)]
        ]
    )


class TelegramBot:
    """Telegram frontend. All logic lives in the Orchestrator."""

    def __init__(
        self,
        config: AgentConfig,
        *,
        agent_name: str = "main",
        bus: MessageBus | None = None,
        lock_pool: LockPool | None = None,
    ) -> None:
        self._config = config
        self._agent_name = agent_name
        self._orchestrator: Orchestrator | None = None
        self._abort_all_callback: Callable[[], Awaitable[int]] | None = None

        self._bot = Bot(
            token=config.telegram_token,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        self._notification_service: NotificationService = TelegramNotificationService(
            self._bot, config
        )
        self._bot_id: int | None = None
        self._bot_username: str | None = None

        self._dp = Dispatcher()
        self._router = Router(name="main")
        self._exit_code: int = 0
        self._restart_watcher: asyncio.Task[None] | None = None
        self._update_observer: UpdateObserver | None = None
        self._upgrade_lock = asyncio.Lock()
        self._group_audit_task: asyncio.Task[None] | None = None

        allowed = set(config.allowed_user_ids)
        allowed_groups = set(config.allowed_group_ids)
        allowed_channels = set(config.allowed_channel_ids)
        self._allowed_users = allowed
        self._allowed_groups = allowed_groups
        self._allowed_channels = allowed_channels
        self._chat_tracker: ChatTracker | None = None  # set in _on_startup
        self._topic_names = TopicNameCache()
        self._lock_pool = lock_pool or LockPool()
        self._upload_store: UploadStore | None = None
        self._edit_store = EditStore()
        self._clipboard = ClipboardStore()
        self._bus = bus or MessageBus(lock_pool=self._lock_pool)

        from ductor_bot.messenger.telegram.transport import TelegramTransport

        self._bus.register_transport(TelegramTransport(self))
        self._sequential = SequentialMiddleware(
            lock_pool=self._lock_pool,
            topic_names=self._topic_names,
            group_mention_only=config.group_mention_only,
        )
        self._sequential.set_bot(self._bot)
        self._sequential.set_interrupt_handler(self._on_interrupt)
        self._sequential.set_abort_handler(self._on_abort)
        self._sequential.set_abort_all_handler(self._on_abort_all)
        self._sequential.set_quick_command_handler(self._on_quick_command)
        on_rejected = self._on_group_rejected
        auth = AuthMiddleware(allowed, allowed_group_ids=allowed_groups, on_rejected=on_rejected)
        self._router.message.outer_middleware(auth)
        self._router.message.outer_middleware(self._sequential)
        self._router.callback_query.outer_middleware(
            AuthMiddleware(allowed, allowed_group_ids=allowed_groups, on_rejected=on_rejected)
        )

        self._register_handlers()
        self._register_member_handlers()
        self._dp.include_router(self._router)
        self._dp.startup.register(self._on_startup)

    @property
    def _orch(self) -> Orchestrator:
        if self._orchestrator is None:
            msg = "Orchestrator not initialized -- call after startup"
            raise RuntimeError(msg)
        return self._orchestrator

    @property
    def orchestrator(self) -> Orchestrator | None:
        """Public read-only access to the orchestrator (None before startup)."""
        return self._orchestrator

    def set_abort_all_callback(self, callback: Callable[[], Awaitable[int]]) -> None:
        """Set a callback that kills processes on ALL agents (set by supervisor)."""
        self._abort_all_callback = callback

    @property
    def dispatcher(self) -> Dispatcher:
        """Public read-only access to the aiogram Dispatcher."""
        return self._dp

    @property
    def bot_instance(self) -> Bot:
        """Public read-only access to the aiogram Bot instance."""
        return self._bot

    @property
    def config(self) -> AgentConfig:
        """Public read-only access to the agent configuration."""
        return self._config

    @property
    def notification_service(self) -> NotificationService:
        """Transport-agnostic notification interface."""
        return self._notification_service

    def register_startup_hook(self, hook: Callable[[], Awaitable[None]]) -> None:
        """Register a callback to run after bot startup (used by supervisor)."""
        self._dp.startup.register(hook)

    @property
    def sequential(self) -> SequentialMiddleware:
        """Public read-only access to the sequential middleware."""
        return self._sequential

    @property
    def lock_pool(self) -> LockPool:
        """Shared lock pool (used by middleware, bus, and API server)."""
        return self._lock_pool

    def _is_addressed(self, message: Message) -> bool:
        """True if the message is addressed to this bot instance."""
        if message.chat.type not in ("group", "supergroup"):
            return True
        return is_message_addressed(message, self._bot_id, self._bot_username)

    def _is_for_others(self, message: Message) -> bool:
        """True if the message is a command explicitly for another bot."""
        if message.chat.type not in ("group", "supergroup"):
            return False
        return is_command_for_others(message, self._bot_username)

    def file_roots(self, paths: DuctorPaths) -> list[Path] | None:
        """Allowed root directories for ``<file:...>`` tag sends."""
        return resolve_allowed_roots(self._config.file_access, paths.workspace)

    async def broadcast(self, text: str, opts: SendRichOpts | None = None) -> None:
        """Send a message to all allowed users."""
        for uid in self._config.allowed_user_ids:
            await send_rich(self._bot, uid, text, opts)

    async def notify_startup(self, text: str) -> None:
        """Route startup-lifecycle notifications (#64).

        Fallback policy:
          * ``startup_targets`` empty (default) -> broadcast via ``notify_all``.
          * ``startup_targets`` non-empty but every entry disabled -> explicit
            silence (no fallback). This is how users opt out of lifecycle
            notifications without losing upgrade routing.

        Per-target failures are swallowed at warning level so a single bad
        target does not mask the rest.
        """
        configured = self._config.notifications.startup_targets
        if not configured:
            await self._notification_service.notify_all(text)
            return
        targets = [t for t in configured if t.enabled and t.chat_id is not None]
        for target in targets:
            try:
                assert target.chat_id is not None
                await send_rich(
                    self._bot,
                    target.chat_id,
                    text,
                    SendRichOpts(thread_id=target.topic_id),
                )
            except Exception:
                logger.warning(
                    "notify_startup: delivery failed for chat_id=%s topic_id=%s",
                    target.chat_id,
                    target.topic_id,
                    exc_info=True,
                )

    async def notify_upgrade(self, text: str, opts: SendRichOpts | None = None) -> None:
        """Route upgrade-available notifications (#64).

        Fallback policy (mirrors ``notify_startup``):
          * ``upgrade_targets`` empty (default) -> ``broadcast``.
          * ``upgrade_targets`` non-empty but every entry disabled -> explicit
            silence (no fallback).
        ``opts`` (reply_markup, etc.) is preserved on each per-target send.
        """
        configured = self._config.notifications.upgrade_targets
        if not configured:
            await self.broadcast(text, opts)
            return
        targets = [t for t in configured if t.enabled and t.chat_id is not None]
        for target in targets:
            try:
                assert target.chat_id is not None
                target_opts = SendRichOpts(
                    reply_markup=opts.reply_markup if opts else None,
                    allowed_roots=opts.allowed_roots if opts else None,
                    thread_id=target.topic_id,
                    reply_to_message_id=opts.reply_to_message_id if opts else None,
                )
                await send_rich(self._bot, target.chat_id, text, target_opts)
            except Exception:
                logger.warning(
                    "notify_upgrade: delivery failed for chat_id=%s topic_id=%s",
                    target.chat_id,
                    target.topic_id,
                    exc_info=True,
                )

    async def _on_startup(self) -> None:
        from ductor_bot.messenger.telegram.startup import run_startup

        await run_startup(self)
        self._sequential.set_bot_username(self._bot_username)
        self._sequential.set_bot_id(self._bot_id)

    def _register_handlers(self) -> None:
        r = self._router
        r.message(CommandStart(ignore_case=True))(self._on_start)
        r.message(Command("help", ignore_case=True))(self._on_help)
        r.message(Command("info", ignore_case=True))(self._on_info)
        r.message(Command("stop_all", ignore_case=True))(self._on_stop_all)
        r.message(Command("stop", ignore_case=True))(self._on_stop)
        r.message(Command("restart", ignore_case=True))(self._on_restart)
        r.message(Command("session", ignore_case=True))(self._on_session)
        r.message(Command("named", ignore_case=True))(self._on_named)
        # "showfiles" stays as an unlisted alias: renaming a command people
        # already type should not break their muscle memory.
        r.message(Command("files", "showfiles", ignore_case=True))(self._on_files)
        r.message(Command("menu", ignore_case=True))(self._on_menu)
        r.message(Command("agent_commands", ignore_case=True))(self._on_agent_commands)
        base_cmds = [
            "status",
            "memory",
            "model",
            "effort",
            "account",
            "persona",
            "folder",
            "consult",
            "skills",
            "cron",
            "diagnose",
            "upgrade",
            "reset",
        ]
        if self._agent_name == "main":
            base_cmds += ["agents", "agent_start", "agent_stop", "agent_restart"]
        for cmd in base_cmds:
            r.message(Command(cmd, ignore_case=True))(self._on_command)
        r.message(F.forum_topic_created)(self._on_forum_topic_created)
        r.message(F.forum_topic_edited)(self._on_forum_topic_edited)
        r.message()(self._on_message)
        r.callback_query()(self._on_callback_query)

    def _register_member_handlers(self) -> None:
        """Register my_chat_member handlers on the dispatcher (not router).

        ``ChatMemberUpdated`` events bypass message middleware, so they go
        directly on the dispatcher.
        """
        from aiogram.filters import ChatMemberUpdatedFilter
        from aiogram.filters.chat_member_updated import (
            IS_MEMBER,
            IS_NOT_MEMBER,
        )

        self._dp.my_chat_member.register(
            self._on_bot_added,
            ChatMemberUpdatedFilter(IS_NOT_MEMBER >> IS_MEMBER),
        )
        self._dp.my_chat_member.register(
            self._on_bot_removed,
            ChatMemberUpdatedFilter(IS_MEMBER >> IS_NOT_MEMBER),
        )

    def _on_auth_hot_reload(self, config: AgentConfig, hot: dict[str, object]) -> None:
        """Update auth sets and language in-place when config is hot-reloaded."""
        if "allowed_user_ids" in hot:
            self._allowed_users.clear()
            self._allowed_users.update(config.allowed_user_ids)
            logger.info("Auth hot-reloaded: allowed_user_ids (%d)", len(self._allowed_users))
        if "allowed_group_ids" in hot:
            self._allowed_groups.clear()
            self._allowed_groups.update(config.allowed_group_ids)
            logger.info("Auth hot-reloaded: allowed_group_ids (%d)", len(self._allowed_groups))
            self._group_audit_task = asyncio.create_task(self._fire_audit())
        if "allowed_channel_ids" in hot:
            self._allowed_channels.clear()
            self._allowed_channels.update(config.allowed_channel_ids)
            logger.info("Auth hot-reloaded: allowed_channel_ids (%d)", len(self._allowed_channels))
        if "language" in hot:
            _rebuild_commands()
            self._lang_sync_task = asyncio.create_task(self._sync_commands())
            logger.info("Language hot-reloaded: commands re-synced")

    # -- Chat tracker (my_chat_member + /where + /leave) ------------------------

    def _on_group_rejected(self, chat_id: int, chat_type: str, title: str) -> None:
        """Callback from AuthMiddleware when a group message is rejected."""
        if self._chat_tracker:
            self._chat_tracker.record_rejected(chat_id, chat_type, title)

    async def _on_bot_added(self, event: ChatMemberUpdated) -> None:
        """Bot was added to a group or channel."""
        chat = event.chat
        is_channel = chat.type == "channel"
        if is_channel:
            allowed = chat.id in self._allowed_channels
            reject_key = "telegram.channel_not_whitelisted"
            chat_kind = "channel"
        else:
            allowed = chat.id in self._allowed_groups
            reject_key = "telegram.group_rejected"
            chat_kind = "group"
        if self._chat_tracker:
            self._chat_tracker.record_join(
                chat.id,
                chat.type,
                chat.title or "",
                allowed=allowed,
            )
        if not allowed:
            with contextlib.suppress(TelegramAPIError):
                await self._bot.send_message(
                    chat.id,
                    t(reject_key),
                )
            with contextlib.suppress(TelegramAPIError):
                await self._bot.leave_chat(chat.id)
            if self._chat_tracker:
                self._chat_tracker.record_leave(chat.id, "auto_left")
            logger.info(
                "Auto-left unauthorized %s chat_id=%d title=%s",
                chat_kind,
                chat.id,
                chat.title,
            )
            return
        await self._send_join_notification(chat.id)

    async def _on_bot_removed(self, event: ChatMemberUpdated) -> None:
        """Bot was removed from a group."""
        chat = event.chat
        status = "kicked" if event.new_chat_member.status == "kicked" else "left"
        if self._chat_tracker:
            self._chat_tracker.record_leave(chat.id, status)
        logger.info("Bot removed from group chat_id=%d status=%s", chat.id, status)

    async def _send_join_notification(self, chat_id: int) -> None:
        """Send JOIN_NOTIFICATION.md content and try to pin it."""
        if not self._orchestrator:
            return
        path = self._orch.paths.join_notification_path
        if not path.is_file():
            return
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            return
        from ductor_bot.messenger.telegram.sender import _send_text_chunks

        msg, _delivered = await _send_text_chunks(self._bot, chat_id, text)
        if msg:
            with contextlib.suppress(TelegramAPIError):
                await self._bot.pin_chat_message(chat_id, msg.message_id, disable_notification=True)

    _GROUP_AUDIT_INTERVAL = 86400  # 24 hours

    async def _fire_audit(self) -> None:
        """Fire-and-forget wrapper for ``audit_groups``."""
        await self.audit_groups()

    async def _run_group_audit_loop(self) -> None:
        """Run ``audit_groups`` every 24 hours."""
        while True:
            await asyncio.sleep(self._GROUP_AUDIT_INTERVAL)
            try:
                left = await self.audit_groups()
                if left:
                    logger.info("Periodic group audit: left %d group(s)", left)
            except Exception:
                logger.debug("Periodic group audit error", exc_info=True)

    async def audit_groups(self) -> int:
        """Leave groups where the bot is still a member but no longer allowed.

        Checks tracked active groups against ``allowed_group_ids`` and calls
        ``leave_chat`` for any that lost authorization.  Returns the number
        of groups left.
        """
        if not self._chat_tracker:
            return 0
        left = 0
        for rec in self._chat_tracker.get_all():
            if rec.status != "active":
                continue
            if rec.chat_type == "channel":
                if rec.chat_id in self._allowed_channels:
                    continue
            elif rec.chat_id in self._allowed_groups:
                continue
            # Not allowed — try to leave.
            try:
                await self._bot.leave_chat(rec.chat_id)
            except TelegramAPIError:
                logger.debug("audit_groups: leave_chat failed for %d", rec.chat_id, exc_info=True)
            self._chat_tracker.record_leave(rec.chat_id, "auto_left")
            logger.info("Audit: auto-left group %d (%s)", rec.chat_id, rec.title)
            left += 1
        return left

    @staticmethod
    def _where_line(r: ChatRecord) -> str:
        """Format a single chat record for /where output."""
        title = r.title or "untitled"
        return f"`{r.chat_id}` — {title} ({r.chat_type})"

    def _format_where(self) -> str:
        """Build the /where response text."""
        if not self._chat_tracker:
            return fmt(t("telegram.where_header"), SEP, t("telegram.where_no_tracker"))
        records = self._chat_tracker.get_all()
        if not records:
            return fmt(t("telegram.where_header"), SEP, t("telegram.where_empty"))

        sections: list[str] = []
        active = [r for r in records if r.status == "active" and r.allowed]
        rejected = [r for r in records if not r.allowed or r.status == "rejected"]
        left = [r for r in records if r.status in ("left", "kicked", "auto_left")]

        if active:
            lines = [self._where_line(r) for r in active]
            sections.append("**Active**\n" + "\n".join(lines))
        if rejected:
            lines = []
            for r in rejected:
                extra = f" — {r.rejected_count}x rejected" if r.rejected_count else ""
                lines.append(f"{self._where_line(r)}{extra}")
            sections.append("**Rejected**\n" + "\n".join(lines))
        if left:
            lines = [f"{self._where_line(r)} [{r.status}]" for r in left]
            sections.append("**Left**\n" + "\n".join(lines))

        return fmt(t("telegram.where_header"), SEP, *sections)

    async def _handle_where(self, chat_id: int, message: Message) -> None:
        """Handle /where: show all tracked chats/groups."""
        await send_rich(
            self._bot,
            chat_id,
            self._format_where(),
            SendRichOpts(
                reply_to_message_id=message.message_id,
                thread_id=get_thread_id(message),
            ),
        )

    async def _handle_leave(self, chat_id: int, message: Message) -> None:
        """Handle /leave <group_id>: manually leave a group."""
        thread_id = get_thread_id(message)
        parts = (message.text or "").strip().split(None, 1)
        if len(parts) < 2:
            await send_rich(
                self._bot,
                chat_id,
                fmt(t("telegram.leave_usage_header"), SEP, t("telegram.leave_usage")),
                SendRichOpts(reply_to_message_id=message.message_id, thread_id=thread_id),
            )
            return

        try:
            group_id = int(parts[1].strip())
        except ValueError:
            await send_rich(
                self._bot,
                chat_id,
                t("telegram.leave_invalid_id"),
                SendRichOpts(reply_to_message_id=message.message_id, thread_id=thread_id),
            )
            return

        try:
            await self._bot.leave_chat(group_id)
        except TelegramAPIError as exc:
            await send_rich(
                self._bot,
                chat_id,
                t("telegram.leave_failed", error=exc),
                SendRichOpts(reply_to_message_id=message.message_id, thread_id=thread_id),
            )
            return

        if self._chat_tracker:
            self._chat_tracker.record_leave(group_id, "left")

        await send_rich(
            self._bot,
            chat_id,
            t("telegram.left_group", group_id=group_id),
            SendRichOpts(reply_to_message_id=message.message_id, thread_id=thread_id),
        )

    # -- Welcome & help ---------------------------------------------------------

    async def _show_welcome(self, message: Message) -> None:
        """Send the welcome screen with auth status and quick-start buttons."""
        from ductor_bot.cli.auth import check_all_auth

        chat_id = message.chat.id
        thread_id = get_thread_id(message)
        user_name = message.from_user.first_name if message.from_user else ""

        from ductor_bot.cli.claude_accounts import active_claude_account_dir

        auth_results = await asyncio.to_thread(
            check_all_auth, active_claude_account_dir(self._config)
        )
        text = build_welcome_text(user_name, auth_results, self._config)
        keyboard = build_welcome_keyboard()

        sent_with_image = await self._send_welcome_image(
            chat_id, text, keyboard, message, thread_id=thread_id
        )
        if not sent_with_image:
            await send_rich(
                self._bot,
                chat_id,
                text,
                SendRichOpts(
                    reply_to_message_id=message.message_id,
                    reply_markup=keyboard,
                    thread_id=thread_id,
                ),
            )

    async def _send_welcome_image(
        self,
        chat_id: int,
        text: str,
        keyboard: InlineKeyboardMarkup,
        reply_to: Message,
        *,
        thread_id: int | None = None,
    ) -> bool:
        """Try to send welcome.png with caption. Returns True if caption was attached."""
        if not _WELCOME_IMAGE.is_file():
            return False

        html_caption: str | None = None
        if len(text) <= _CAPTION_LIMIT:
            html_caption = markdown_to_telegram_html(text)

        try:
            await self._bot.send_photo(
                chat_id=chat_id,
                photo=FSInputFile(_WELCOME_IMAGE),
                caption=html_caption,
                parse_mode=ParseMode.HTML if html_caption else None,
                reply_markup=keyboard if html_caption else None,
                reply_parameters=ReplyParameters(message_id=reply_to.message_id),
                message_thread_id=thread_id,
            )
        except TelegramBadRequest:
            logger.warning("Welcome image caption failed, retrying without")
            try:
                await self._bot.send_photo(
                    chat_id=chat_id,
                    photo=FSInputFile(_WELCOME_IMAGE),
                    reply_parameters=ReplyParameters(message_id=reply_to.message_id),
                    message_thread_id=thread_id,
                )
            except (TelegramAPIError, OSError):
                logger.exception("Failed to send welcome image")
                return False
            return False
        except (TelegramAPIError, OSError):
            logger.exception("Failed to send welcome image")
            return False
        return html_caption is not None

    async def _on_start(self, message: Message) -> None:
        """Handle /start: always show welcome screen."""
        if self._is_for_others(message):
            return
        if self._config.group_mention_only and not self._is_addressed(message):
            return
        await self._show_welcome(message)
        await self._send_join_notification(message.chat.id)

    async def _on_help(self, message: Message) -> None:
        """Handle /help: show command reference."""
        if self._is_for_others(message):
            return
        if self._config.group_mention_only and not self._is_addressed(message):
            return
        await send_rich(
            self._bot,
            message.chat.id,
            _build_help_text(),
            SendRichOpts(reply_to_message_id=message.message_id, thread_id=get_thread_id(message)),
        )

    async def _on_agent_commands(self, message: Message) -> None:
        """Handle /agent_commands: explain multi-agent system + list commands."""
        if self._is_for_others(message):
            return
        if self._config.group_mention_only and not self._is_addressed(message):
            return
        chat_id = message.chat.id
        thread_id = get_thread_id(message)

        lines = [
            t("agents.telegram_explanation"),
            "",
            t("agents.commands_header"),
            "`/agents` — list all agents and their status",
            "`/agent_start <name>` — start a sub-agent",
            "`/agent_stop <name>` — stop a sub-agent",
            "`/agent_restart <name>` — restart a sub-agent",
            "",
            t("agents.setup_header"),
            t("agents.setup_instruction"),
        ]
        text = fmt(t("agents.system_header"), SEP, "\n".join(lines))
        await send_rich(
            self._bot,
            chat_id,
            text,
            SendRichOpts(reply_to_message_id=message.message_id, thread_id=thread_id),
        )

    async def _on_info(self, message: Message) -> None:
        """Handle /info: show project links and version."""
        if self._is_for_others(message):
            return
        if self._config.group_mention_only and not self._is_addressed(message):
            return
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        version = get_current_version()
        text = fmt(
            t("info.header"),
            t("info.version", version=version),
            SEP,
            t("info.telegram_description"),
        )
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="GitHub", url="https://github.com/PleasePrompto/ductor"
                    ),
                    InlineKeyboardButton(
                        text="Changelog",
                        url="https://github.com/PleasePrompto/ductor/releases",
                    ),
                ],
                [InlineKeyboardButton(text="PyPI", url="https://pypi.org/project/ductor/")],
            ],
        )
        await send_rich(
            self._bot,
            message.chat.id,
            text,
            SendRichOpts(
                reply_to_message_id=message.message_id,
                reply_markup=keyboard,
                thread_id=get_thread_id(message),
            ),
        )

    async def _on_menu(self, message: Message) -> None:
        """Handle /menu: open the inline menu, and install the toggle panel.

        The command message is deleted straight away. It is the only text this
        feature ever sends, and leaving a "/menu" in the topic every time the
        toggle is tapped is the clutter the inline menu exists to avoid.
        """
        chat_id = message.chat.id
        thread_id = get_thread_id(message)

        with contextlib.suppress(TelegramAPIError):
            await self._bot.delete_message(chat_id=chat_id, message_id=message.message_id)

        # Sent with the panel attached so the toggle exists from now on; after
        # this the button sends /menu and the user never types it again.
        with contextlib.suppress(TelegramAPIError):
            await self._bot.send_message(
                chat_id,
                markdown_to_telegram_html(t("menu.panel_ready")),
                reply_markup=build_toggle_panel(),
                message_thread_id=thread_id,
                parse_mode=ParseMode.HTML,
            )

        await self._send_menu(get_session_key(message), thread_id)

    async def _send_menu(self, key: SessionKey, thread_id: int | None) -> None:
        text, keyboard = build_menu(self._menu_subtitle(key))
        with contextlib.suppress(TelegramAPIError):
            await self._bot.send_message(
                key.chat_id,
                markdown_to_telegram_html(text),
                reply_markup=keyboard,
                message_thread_id=thread_id,
                parse_mode=ParseMode.HTML,
            )

    async def _edit_to_menu(self, key: SessionKey, message_id: int) -> None:
        """Turn a submenu back into the menu, in place.

        Editing rather than sending: the screen the user is looking at is the
        one that should become the menu, and a second menu message left behind
        is the clutter closing exists to avoid.
        """
        text, keyboard = build_menu(self._menu_subtitle(key))
        with contextlib.suppress(TelegramAPIError):
            await self._bot.edit_message_text(
                text=markdown_to_telegram_html(text),
                chat_id=key.chat_id,
                message_id=message_id,
                reply_markup=keyboard,
                parse_mode=ParseMode.HTML,
            )

    def _menu_subtitle(self, key: SessionKey) -> str:
        """Current bindings, shown in the header instead of hiding buttons."""
        bound = self._orch.bindings.resolve(key.storage_key)
        folder = bound.name if bound else ""
        persona = self._orch.personas.get(key.storage_key) or ""
        model = ""
        with contextlib.suppress(AttributeError, KeyError, TypeError):
            model = self._orch.resolve_session_directive(key).model or ""
        return state_subtitle(folder, persona, model)

    async def _handle_menu_callback(
        self, key: SessionKey, message_id: int, data: str, *, thread_id: int | None = None
    ) -> None:
        """Run the command a menu button stands for.

        Routed through the orchestrator's own registry rather than
        re-implemented, so a menu item behaves exactly as typing the command
        does and cannot drift from it.
        """
        if data == MNU_CLOSE:
            with contextlib.suppress(TelegramAPIError):
                await self._bot.delete_message(chat_id=key.chat_id, message_id=message_id)
            return

        if data == MNU_BACK:
            await self._edit_to_menu(key, message_id)
            return

        index = parse_menu_callback(data)
        if index is None or not 0 <= index < len(MENU_ITEMS):
            return
        command = MENU_ITEMS[index].command

        # Some menu entries are transport screens rather than orchestrator
        # commands. Sending those through handle_message routes them to the
        # agent, which answers "that is not available in this environment" —
        # a menu button producing a chat reply, with nothing to show it failed.
        transport = self._transport_menu_actions()
        if command in transport:
            await transport[command](key, thread_id)
            return

        # Commands that run a model turn before answering leave the user
        # staring at an unchanged screen for a minute, so they press the button
        # again — and again. Say something first.
        if command in _SLOW_COMMANDS:
            with contextlib.suppress(TelegramAPIError):
                await self._bot.send_message(
                    key.chat_id,
                    markdown_to_telegram_html(t(_SLOW_COMMANDS[command])),
                    message_thread_id=thread_id,
                    parse_mode=ParseMode.HTML,
                )

        # handle_message routes a leading slash through the same registry the
        # typed command uses, so a menu item cannot drift from its command.
        result = await self._orch.handle_message(key, command)
        if result is None or not result.text:
            return
        await send_rich(
            self._bot,
            key.chat_id,
            result.text,
            SendRichOpts(
                reply_markup=button_grid_to_markup(result.buttons) if result.buttons else None,
                thread_id=thread_id,
            ),
        )

    def _transport_menu_actions(self):  # noqa: ANN202
        """Menu entries handled here rather than by the orchestrator registry."""
        return {
            "/files": self._send_files_view,
            "/help": self._send_help_view,
        }

    async def _send_help_view(self, key: SessionKey, thread_id: int | None) -> None:
        await send_rich(
            self._bot,
            key.chat_id,
            _build_help_text(),
            SendRichOpts(thread_id=thread_id),
        )

    def _is_consult(self, key: SessionKey) -> bool:
        """True in the Consult topic this bot created for *key*'s chat."""
        if key.topic_id is None or not self._config.managed_topics:
            return False
        from ductor_bot.messenger.telegram.managed_topics import CONSULT, ManagedTopicStore

        store = ManagedTopicStore(self._orch.paths.managed_topics_path)
        return store.get(key.chat_id, CONSULT).topic_id == key.topic_id

    def _roots_for(self, key: SessionKey) -> dict[str, str]:
        """The directories this conversation may see.

        Everywhere else this is the configured catalogue. In Consult it is that
        topic's own directory and nothing else — the file manager is our code,
        so unlike the instruction in its CLAUDE.md this is actually enforced.
        """
        if self._is_consult(key):
            from ductor_bot.messenger.telegram.file_browser import RESTRICTED

            return {
                "Consult": str(self._orch.paths.consult_dir),
                # Without this the browser adds ~/.ductor, which contains
                # Consult — and the nested-root rule would keep the ancestor,
                # exposing everything the narrowing was for.
                RESTRICTED: "1",
            }
        return dict(self._config.project_roots)

    async def _send_files_view(self, key: SessionKey, thread_id: int | None) -> None:
        text, keyboard = await file_browser_start(
            self._orch.paths,
            self._roots_for(key),
            self._orch.bindings.resolve(key.storage_key),
        )
        await send_rich(
            self._bot,
            key.chat_id,
            text,
            SendRichOpts(reply_markup=keyboard, thread_id=thread_id),
        )

    async def _on_files(self, message: Message) -> None:
        """Handle /files: browse, transfer and manage files."""
        await self._send_files_view(get_session_key(message), get_thread_id(message))

    # -- Interrupt, abort, commands, sessions ----------------------------------

    async def _on_interrupt(self, chat_id: int, message: Message) -> bool:
        return await handle_interrupt(
            self._orchestrator,
            self._bot,
            chat_id=chat_id,
            message=message,
        )

    async def _on_abort_all(self, chat_id: int, message: Message) -> bool:
        return await handle_abort_all(
            self._orchestrator,
            self._bot,
            chat_id=chat_id,
            message=message,
            abort_all_callback=self._abort_all_callback,
        )

    async def _on_abort(self, chat_id: int, message: Message) -> bool:
        return await handle_abort(
            self._orchestrator,
            self._bot,
            chat_id=chat_id,
            message=message,
        )

    async def _dispatch_direct_command(
        self,
        chat_id: int,
        message: Message,
        text_lower: str,
    ) -> bool | None:
        """Handle commands that don't need the orchestrator. Returns True/None."""
        if text_lower.startswith("/where"):
            await self._handle_where(chat_id, message)
            return True
        if text_lower.startswith("/leave"):
            await self._handle_leave(chat_id, message)
            return True
        if text_lower.startswith(("/files", "/showfiles")) and self._orchestrator is not None:
            await self._on_files(message)
            return True
        if text_lower.startswith("/menu"):
            await self._on_menu(message)
            return True
        return None

    async def _on_quick_command(self, chat_id: int, message: Message) -> bool:
        """Handle a read-only command without the sequential lock.

        ``/model`` is special: when the chat is busy it returns an immediate
        "agent is working" message; otherwise it acquires the lock for an
        atomic model switch.
        """
        if self._is_for_others(message) or (
            self._config.group_mention_only and not self._is_addressed(message)
        ):
            return False

        text_lower = (message.text or "").strip().lower()

        direct = await self._dispatch_direct_command(chat_id, message, text_lower)
        if direct is not None or self._orchestrator is None:
            return direct or False

        if text_lower.startswith("/named"):
            await handle_command(self._orchestrator, self._bot, message)
            return True

        if text_lower.startswith("/model"):
            await handle_command(self._orchestrator, self._bot, message)
            return True

        await handle_command(self._orchestrator, self._bot, message)
        return True

    async def _on_stop_all(self, message: Message) -> None:
        if self._is_for_others(message):
            return
        if self._config.group_mention_only and not self._is_addressed(message):
            return
        await handle_abort_all(
            self._orchestrator,
            self._bot,
            chat_id=message.chat.id,
            message=message,
            abort_all_callback=self._abort_all_callback,
        )

    async def _on_stop(self, message: Message) -> None:
        if self._is_for_others(message):
            return
        if self._config.group_mention_only and not self._is_addressed(message):
            return
        await handle_abort(
            self._orchestrator,
            self._bot,
            chat_id=message.chat.id,
            message=message,
        )

    async def _on_command(self, message: Message) -> None:
        if self._is_for_others(message):
            return
        if self._config.group_mention_only and not self._is_addressed(message):
            return
        await handle_command(self._orch, self._bot, message)

    async def _on_forum_topic_created(self, message: Message) -> None:
        """Cache the name when a forum topic is created."""
        from ductor_bot.messenger.telegram.topic import get_topic_name_from_message

        name = get_topic_name_from_message(message)
        if name and message.message_thread_id is not None:
            self._topic_names.set(message.chat.id, message.message_thread_id, name)
            logger.debug(
                "Topic name cached: %d/%d = %s", message.chat.id, message.message_thread_id, name
            )

    async def _on_forum_topic_edited(self, message: Message) -> None:
        """Update the cache when a forum topic is renamed."""
        from ductor_bot.messenger.telegram.topic import get_topic_name_from_message

        name = get_topic_name_from_message(message)
        if name and message.message_thread_id is not None:
            self._topic_names.set(message.chat.id, message.message_thread_id, name)
            logger.debug(
                "Topic name updated: %d/%d = %s", message.chat.id, message.message_thread_id, name
            )

    def _build_session_help(self) -> str:
        """Build the /session hub: explain the system + show commands."""
        providers = self._orch.available_providers
        lines: list[str] = [
            t("session_help.telegram_explanation"),
            "",
            t("session_help.usage_header"),
        ]

        if len(providers) == 1:
            p = next(iter(providers))
            if p == "claude":
                lines.append(t("session_help.claude_single"))
                lines.append(t("session_help.claude_model"))
            elif p == "codex":
                lines.append(t("session_help.codex_single"))
            elif p == "grok":
                lines.append(t("session_help.grok_single"))
                lines.append(t("session_help.grok_model"))
            else:
                lines.append(t("session_help.gemini_single"))
                lines.append(t("session_help.gemini_model"))
        else:
            lines.append(t("session_help.default_provider"))
            if "claude" in providers:
                lines.append(t("session_help.claude_multi"))
            if "codex" in providers:
                lines.append(t("session_help.codex_multi"))
            if "gemini" in providers:
                lines.append(t("session_help.gemini_multi"))
            if "grok" in providers:
                lines.append(t("session_help.grok_multi"))
            lines.append(t("session_help.explicit"))

        lines += [
            "",
            t("session_help.followup_header"),
            t("session_help.followup_line"),
            "",
            t("session_help.commands_header"),
            t("session_help.telegram_sessions_cmd"),
            t("session_help.telegram_stop_cmd"),
        ]

        return fmt(t("session_help.header"), SEP, "\n".join(lines))

    async def _on_session(self, message: Message) -> None:
        """Handle /session: submit a named background session."""
        import re

        text = (message.text or "").strip()
        parts = text.split(None, 1)
        chat_id = message.chat.id
        thread_id = get_thread_id(message)

        if len(parts) < 2 or not parts[1].strip():
            await send_rich(
                self._bot,
                chat_id,
                self._build_session_help(),
                SendRichOpts(reply_to_message_id=message.message_id, thread_id=thread_id),
            )
            return

        prompt = parts[1].strip()

        # Parse optional @directive prefix:
        #   @provider [model] <prompt>    — e.g. @codex, @claude opus
        #   @model <prompt>               — e.g. @opus (infers provider)
        #   @session-name <prompt>        — follow-up to existing session
        provider_override: str | None = None
        model_override: str | None = None
        session_followup: str | None = None
        directive_match = re.match(r"@([a-zA-Z][a-zA-Z0-9_.-]*)\s+", prompt)
        if directive_match:
            key = directive_match.group(1).lower()
            rest = prompt[directive_match.end() :]

            resolved = self._orch.resolve_session_directive(key)
            if resolved:
                provider_override, model_override = resolved[0], resolved[1] or None
                prompt = rest
                # If key was a provider name, check for optional model after it
                if key in ("claude", "codex", "gemini", "antigravity", "grok"):
                    model_match = re.match(r"([a-zA-Z][a-zA-Z0-9_.-]*)\s+", prompt)
                    if model_match:
                        candidate = model_match.group(1).lower()
                        if self._orch.is_known_model(candidate):
                            model_override = candidate
                            prompt = prompt[model_match.end() :]
            elif self._orch.get_named_session(chat_id, key):
                session_followup = key
                prompt = rest

        try:
            if session_followup:
                task_id = self._orch.submit_named_followup_bg(
                    chat_id, session_followup, prompt, message.message_id, thread_id
                )
                await send_rich(
                    self._bot,
                    chat_id,
                    fmt(
                        f"**[{session_followup}] Follow-up sent**",
                        SEP,
                        f"Task `{task_id}` queued.",
                    ),
                    SendRichOpts(reply_to_message_id=message.message_id, thread_id=thread_id),
                )
            else:
                from ductor_bot.orchestrator.core import NamedSessionRequest

                ns_request = NamedSessionRequest(
                    message_id=message.message_id,
                    thread_id=thread_id,
                    provider_override=provider_override,
                    model_override=model_override,
                )
                task_id, session_name = self._orch.submit_named_session(
                    chat_id,
                    prompt,
                    ns_request,
                )
                ns = self._orch.get_named_session(chat_id, session_name)
                provider = ns.provider if ns else (provider_override or self._orch.config.provider)
                model = ns.model if ns else ""
                provider_label = {
                    "claude": "Claude",
                    "codex": "Codex",
                    "gemini": "Gemini",
                    "antigravity": "Antigravity",
                    "grok": "Grok Build",
                }.get(provider, provider)
                model_info = f" ({model})" if model else ""
                await send_rich(
                    self._bot,
                    chat_id,
                    fmt(
                        f"**Session `{session_name}` started**",
                        SEP,
                        f"Running on {provider_label}{model_info}.\n"
                        f"Follow up: `@{session_name} <message>`",
                    ),
                    SendRichOpts(reply_to_message_id=message.message_id, thread_id=thread_id),
                )
        except ValueError as exc:
            await send_rich(
                self._bot,
                chat_id,
                str(exc),
                SendRichOpts(reply_to_message_id=message.message_id, thread_id=thread_id),
            )

    async def _on_named(self, message: Message) -> None:
        """Handle /named: show named-session management UI."""
        if self._config.group_mention_only and not self._is_addressed(message):
            return
        await handle_command(self._orch, self._bot, message)

    async def _on_restart(self, message: Message) -> None:
        if self._config.group_mention_only and not self._is_addressed(message):
            return
        from ductor_bot.infra.restart import write_restart_sentinel

        chat_id = message.chat.id
        paths = self._orch.paths
        sentinel = paths.ductor_home / "restart-sentinel.json"
        await asyncio.to_thread(
            write_restart_sentinel, chat_id, t("startup.restart_default"), sentinel_path=sentinel
        )
        text = fmt(t("startup.restart_header"), SEP, t("startup.restart_body"))
        await send_rich(
            self._bot,
            message.chat.id,
            text,
            SendRichOpts(reply_to_message_id=message.message_id, thread_id=get_thread_id(message)),
        )
        self._exit_code = EXIT_RESTART
        await self._dp.stop_polling()

    # -- Callbacks -------------------------------------------------------------

    async def _on_callback_query(self, callback: CallbackQuery) -> None:
        """Handle inline keyboard button presses.

        Welcome quick-start (``w:`` prefix), model selector (``ms:`` prefix),
        and generic button callbacks are each routed to their own handler.

        All orchestrator interactions acquire the per-chat lock to prevent
        race conditions with concurrent webhook wake dispatch or model switches.
        """
        from aiogram.types import InaccessibleMessage

        await callback.answer()
        data = callback.data
        msg = callback.message
        if not data or msg is None or isinstance(msg, InaccessibleMessage):
            return

        chat_id = msg.chat.id
        key = get_session_key(msg)
        self._protect_general(msg, key)
        thread_id = get_thread_id(msg)
        set_log_context(operation="cb", chat_id=chat_id)
        logger.info("Callback data=%s", data[:40])

        # Resolve display label before data gets rewritten
        display_label: str = data
        if is_welcome_callback(data):
            display_label = get_welcome_button_label(data) or data
            resolved = resolve_welcome_callback(data)
            if not resolved:
                return
            data = resolved

        if await self._route_special_callback(key, msg.message_id, data, thread_id=thread_id):
            return

        await self._mark_button_choice(chat_id, msg, display_label)

        async with self._sequential.get_lock(key.lock_key):
            if self._config.streaming.enabled:
                await self._handle_streaming(msg, key, data, thread_id=thread_id)
            else:
                await self._handle_non_streaming(msg, key, data, thread_id=thread_id)

    async def _route_special_callback(  # noqa: PLR0911
        self, key: SessionKey, message_id: int, data: str, *, thread_id: int | None = None
    ) -> bool:
        """Handle known callback namespaces. Returns True when handled."""
        if await self._route_prefix_callback(key, message_id, data, thread_id=thread_id):
            return True

        from ductor_bot.orchestrator.selectors.consult_selector import (
            is_consult_selector_callback,
        )

        if is_consult_selector_callback(data):
            await self._handle_consult_selector(key, message_id, data)
            return True

        from ductor_bot.orchestrator.selectors.folder_selector import (
            is_folder_selector_callback,
        )

        if is_folder_selector_callback(data):
            await self._handle_folder_selector(key, message_id, data, thread_id=thread_id)
            return True

        from ductor_bot.orchestrator.selectors.persona_selector import (
            is_persona_selector_callback,
        )

        if is_persona_selector_callback(data):
            await self._handle_persona_selector(key, message_id, data, thread_id=thread_id)
            return True

        from ductor_bot.orchestrator.selectors.skills_selector import (
            is_skills_selector_callback,
        )

        if is_skills_selector_callback(data):
            await self._handle_skills_selector(key, message_id, data)
            return True

        from ductor_bot.orchestrator.selectors.account_selector import (
            is_account_selector_callback,
        )

        if is_account_selector_callback(data):
            await self._handle_account_selector(key, message_id, data)
            return True

        from ductor_bot.orchestrator.selectors.model_selector import is_model_selector_callback

        if is_model_selector_callback(data):
            await self._handle_model_selector(key, message_id, data)
            return True

        from ductor_bot.orchestrator.selectors.cron_selector import is_cron_selector_callback

        if is_cron_selector_callback(data):
            await self._handle_cron_selector(key.chat_id, message_id, data)
            return True

        if is_file_browser_callback(data):
            await self._handle_file_browser(key, message_id, data, thread_id=thread_id)
            return True

        return False

    async def _route_prefix_callback(  # noqa: PLR0911
        self, key: SessionKey, message_id: int, data: str, *, thread_id: int | None = None
    ) -> bool:
        """Handle prefix-based callback namespaces. Returns True when handled."""
        from ductor_bot.messenger.telegram.file_browser import SF_EDIT_APPLY_PREFIX

        chat_id = key.chat_id
        if is_menu_callback(data):
            await self._handle_menu_callback(key, message_id, data, thread_id=thread_id)
            return True

        if data == SF_EDIT_APPLY_PREFIX:
            await self._apply_pending_edit(key, message_id)
            return True
        if is_stop_callback(data):
            await self._handle_stop(key, message_id, thread_id=thread_id)
            return True

        if data.startswith(MQ_PREFIX):
            await self._handle_queue_cancel(chat_id, data, thread_id=thread_id)
            return True

        if data.startswith("upg:"):
            await self._handle_upgrade_callback(chat_id, message_id, data, thread_id=thread_id)
            return True

        if data == HANDOFF_RETRY:
            await self._handle_handoff_retry(key, message_id)
            return True

        from ductor_bot.orchestrator.selectors.session_selector import is_session_selector_callback

        if is_session_selector_callback(data):
            await self._handle_session_selector(chat_id, message_id, data)
            return True

        if data.startswith("ns:"):
            await self._handle_ns_callback(key, data, thread_id=thread_id)
            return True

        return False

    async def _handle_model_selector(self, key: SessionKey, message_id: int, data: str) -> None:
        """Handle model selector wizard by editing the message in-place."""
        from ductor_bot.orchestrator.selectors.model_selector import handle_model_callback

        async with self._sequential.get_lock(key.lock_key):
            resp = await handle_model_callback(self._orch, key, data)
        await edit_selector_response(self._bot, key.chat_id, message_id, resp)

    async def _handle_skills_selector(self, key: SessionKey, message_id: int, data: str) -> None:
        """Handle the skills browser by editing the message in-place."""
        from ductor_bot.orchestrator.selectors.skills_selector import handle_skills_callback

        resp = handle_skills_callback(self._orch, data)
        await edit_selector_response(self._bot, key.chat_id, message_id, resp)

    async def _handle_account_selector(self, key: SessionKey, message_id: int, data: str) -> None:
        """Handle the Claude account selector by editing the message in-place."""
        from ductor_bot.orchestrator.selectors.account_selector import handle_account_callback

        async with self._sequential.get_lock(key.lock_key):
            resp = await handle_account_callback(self._orch, data)
        await edit_selector_response(self._bot, key.chat_id, message_id, resp)

    async def _handle_cron_selector(self, chat_id: int, message_id: int, data: str) -> None:
        """Handle cron selector wizard by editing the message in-place."""
        from ductor_bot.orchestrator.selectors.cron_selector import handle_cron_callback

        async with self._sequential.get_lock(chat_id):
            resp = await handle_cron_callback(self._orch, data)
        await edit_selector_response(self._bot, chat_id, message_id, resp)

    async def _handle_session_selector(self, chat_id: int, message_id: int, data: str) -> None:
        """Handle session selector wizard by editing the message in-place."""
        from ductor_bot.orchestrator.selectors.session_selector import handle_session_callback

        async with self._sequential.get_lock(chat_id):
            resp = await handle_session_callback(self._orch, chat_id, data)
        await edit_selector_response(self._bot, chat_id, message_id, resp)

    async def _handle_ns_callback(
        self, key: SessionKey, data: str, *, thread_id: int | None = None
    ) -> None:
        """Handle ``ns:<session_name>:<label>`` button callbacks from session results."""
        parsed = parse_ns_callback(data)
        if parsed is None:
            return
        session_name, label = parsed

        async with self._sequential.get_lock(key.lock_key):
            if self._config.streaming.enabled:
                from ductor_bot.orchestrator.flows import named_session_streaming

                result = await named_session_streaming(self._orch, key, session_name, label)
            else:
                from ductor_bot.orchestrator.flows import named_session_flow

                result = await named_session_flow(self._orch, key, session_name, label)

            if result.text:
                await send_rich(
                    self._bot,
                    key.chat_id,
                    result.text,
                    SendRichOpts(
                        allowed_roots=self.file_roots(self._orch.paths),
                        thread_id=thread_id,
                    ),
                )

    async def _send_browser_file(
        self, chat_id: int, path: Path, *, thread_id: int | None = None
    ) -> None:
        """Send a file the user tapped in the browser."""
        from aiogram.types import FSInputFile

        try:
            await self._bot.send_document(
                chat_id=chat_id,
                document=FSInputFile(path),
                message_thread_id=thread_id,
            )
        except TelegramAPIError:
            logger.exception("Failed to send browsed file %s", path)
            await self._bot.send_message(
                chat_id, t("file_browser.send_failed", name=path.name), message_thread_id=thread_id
            )

    async def _send_browser_zip(
        self, chat_id: int, directory: Path, *, thread_id: int | None = None
    ) -> None:
        """Zip a directory and send it, or explain why it cannot be sent."""
        import shutil
        from functools import partial

        from aiogram.types import FSInputFile

        from ductor_bot.messenger.telegram.file_browser import build_zip

        archive, error_key = await asyncio.to_thread(build_zip, directory)
        if archive is None:
            await self._bot.send_message(chat_id, t(error_key), message_thread_id=thread_id)
            return
        try:
            await self._bot.send_document(
                chat_id=chat_id,
                document=FSInputFile(archive),
                message_thread_id=thread_id,
            )
        except TelegramAPIError:
            logger.exception("Failed to send archive of %s", directory)
            await self._bot.send_message(
                chat_id,
                t("file_browser.send_failed", name=archive.name),
                message_thread_id=thread_id,
            )
        finally:
            await asyncio.to_thread(partial(shutil.rmtree, archive.parent, ignore_errors=True))

    async def _handle_file_browser(
        self, key: SessionKey, message_id: int, data: str, *, thread_id: int | None = None
    ) -> None:
        """Handle file browser navigation or file request."""
        chat_id = key.chat_id
        action = await handle_file_browser_callback(
            self._orch.paths,
            self._roots_for(key),
            data,
            session=BrowserSession(
                uploads=self._uploads,
                key=key.storage_key,
                current_binding=self._orch.bindings.get(key.storage_key) or None,
                edits=self._edit_store,
                clipboard=self._clipboard,
            ),
        )

        if action.bind_dir is not None and not self._orch.bindings.set(
            key.storage_key, str(action.bind_dir)
        ):
            await self._bot.send_message(
                chat_id,
                markdown_to_telegram_html(t("folder.general_locked")),
                message_thread_id=thread_id,
                parse_mode=ParseMode.HTML,
            )
            return

        if action.edit_message:
            edit = self._edit_store.get(key.storage_key)
            if edit is not None:
                edit.message_id = message_id

        if action.upload_message:
            # Staged files are reported by editing this message, so the upload
            # has to know which one it owns.
            session = self._uploads.get(key.storage_key)
            if session is not None:
                session.message_id = message_id

        if action.send_path is not None:
            await self._send_browser_file(chat_id, action.send_path, thread_id=thread_id)
            return

        if action.zip_dir is not None:
            await self._send_browser_zip(chat_id, action.zip_dir, thread_id=thread_id)
            return

        text, keyboard = action.text, action.keyboard

        # Directory navigation: edit message in-place
        with contextlib.suppress(TelegramBadRequest):
            await self._bot.edit_message_text(
                text=markdown_to_telegram_html(text),
                chat_id=chat_id,
                message_id=message_id,
                reply_markup=keyboard,
                parse_mode=ParseMode.HTML,
            )

    async def _handle_stop(
        self, key: SessionKey, message_id: int, *, thread_id: int | None = None
    ) -> None:
        """Ctrl+C for this topic: interrupt its run, leave every other alone.

        The CLI records the interruption itself and the session stays
        resumable, so anything queued behind this turn simply runs next —
        which is what Ctrl+C does in a terminal.
        """
        stopped = self._orch.interrupt(key.chat_id, key.topic_id)
        with contextlib.suppress(TelegramAPIError):
            await self._bot.edit_message_reply_markup(
                chat_id=key.chat_id, message_id=message_id, reply_markup=None
            )
        if not stopped:
            return
        with contextlib.suppress(TelegramAPIError):
            await self._bot.send_message(
                key.chat_id,
                markdown_to_telegram_html(t("turn.stopped")),
                message_thread_id=thread_id,
                parse_mode=ParseMode.HTML,
            )

    async def _handle_queue_cancel(
        self, chat_id: int, data: str, *, thread_id: int | None = None
    ) -> None:
        """Handle a ``mq:<entry_id>`` callback to cancel a queued message."""
        try:
            entry_id = int(data[len(MQ_PREFIX) :])
        except (ValueError, IndexError):
            return
        await self._sequential.cancel_entry((chat_id, thread_id), entry_id)

    async def _mark_button_choice(self, chat_id: int, msg: Message, label: str) -> None:
        """Edit the bot message to append ``[USER ANSWER] label`` and remove the keyboard."""
        await mark_button_choice(self._bot, chat_id, msg, label)

    # -- Messages --------------------------------------------------------------

    async def _on_message(self, message: Message) -> None:
        text = await self._resolve_text(message)
        if text is None:
            return

        key = get_session_key(message)
        self._protect_general(message, key)
        thread_id = get_thread_id(message)
        logger.debug("Message text=%s", text[:80])

        # A pending rename or new folder is waiting for its name. Consume the
        # message rather than sending it to the agent, which would otherwise
        # act on "docs" as though it were an instruction.
        if await self._collect_edit_name(message, key):
            return

        # Folder first, then persona: one decides where the work happens, the
        # other how. Each holds the message rather than discarding it, so
        # answering does not cost the user their prompt.
        if await self._ask_folder_if_needed(key, text, thread_id=thread_id):
            return

        # Before the persona gate and before any token is spent: a workspace
        # that cannot hold a protected handoff should stop the turn, not divert
        # the file somewhere nobody is looking.
        if await self._block_if_not_ready(key, thread_id=thread_id):
            return

        if await self._ask_persona_if_needed(key, text, thread_id=thread_id):
            return

        # #63: status_reaction (stage-based) wins over seen_reaction (one-shot).
        # Both enabled would fight over the same Telegram emoji slot.
        if self._config.scene.seen_reaction and not self._config.scene.status_reaction:
            await self._set_seen_reaction(message)

        if self._config.streaming.enabled:
            await self._handle_streaming(message, key, text, thread_id=thread_id)
        else:
            await self._handle_non_streaming(message, key, text, thread_id=thread_id)

    async def _handle_consult_selector(self, key: SessionKey, message_id: int, data: str) -> None:
        """Record a new wipe schedule and refresh the notice that states it."""
        from ductor_bot.config import update_config_file_async
        from ductor_bot.orchestrator.selectors.consult_selector import (
            consult_selector,
            parse_callback,
            resolve_choice,
        )

        index = parse_callback(data)
        chosen = resolve_choice(index) if index is not None else None
        if chosen is None:
            await edit_selector_response(
                self._bot, key.chat_id, message_id, consult_selector(self._orch)
            )
            return

        self._config.consult_wipe = chosen
        await update_config_file_async(self._orch.paths.config_path, consult_wipe=chosen)
        # The pinned notice names the schedule, so it is now out of date. It is
        # rewritten here rather than at the next restart: the sentence people
        # rely on must not describe the old plan.
        await self._refresh_managed_notices()
        await edit_selector_response(
            self._bot, key.chat_id, message_id, consult_selector(self._orch)
        )

    async def _refresh_managed_notices(self) -> None:
        """Re-run the bootstrap so pinned notices match the current config."""
        if not self._config.managed_topics:
            return
        from ductor_bot.messenger.telegram.managed_topics import (
            ManagedTopicStore,
            ensure_managed_topics,
        )

        paths = self._orch.paths
        store = ManagedTopicStore(paths.managed_topics_path)
        schedule = (self._config.consult_wipe, self._config.consult_wipe_hour)
        for chat_id in self._config.allowed_group_ids:
            await ensure_managed_topics(
                self._bot, chat_id, store, paths.consult_dir, schedule
            )

    def _protect_general(self, message: Message, key: SessionKey) -> None:
        """Mark a forum's General thread as unbindable, before anything reads it.

        Called at both front doors — messages and callbacks — because every path
        that could bind a folder is reached by an update from the same chat, so
        marking here is enough for the store to refuse all of them.
        """
        if is_general_thread(message):
            self._orch.bindings.protect(key.storage_key)

    async def _ask_folder_if_needed(
        self, key: SessionKey, text: str, *, thread_id: int | None = None
    ) -> bool:
        """Ask which folder this conversation works in, before any work happens.

        Returns True when the message was held and the question asked, in which
        case the caller must not process it yet.

        Nothing is inferred. A folder is never guessed from a topic's name — the
        mechanism that did guess failed silently after every restart, which is
        how a file ended up in a directory nobody was told about.
        """
        from ductor_bot.orchestrator.selectors.folder_selector import folder_selector

        if self._orch.bindings.has_choice(key.storage_key):
            return False
        # Nothing configured means one possible answer, so there is nothing to
        # consent to; blocking here would lock out an installation that has
        # never defined a project.
        catalogue = self._roots_for(key)
        if not catalogue:
            return False

        self._orch.bindings.hold(key.storage_key, text)
        resp = folder_selector(self._orch, key, asking=True, catalogue=catalogue)
        await self._bot.send_message(
            key.chat_id,
            markdown_to_telegram_html(resp.text),
            reply_markup=button_grid_to_markup(resp.buttons),
            message_thread_id=thread_id,
            parse_mode=ParseMode.HTML,
        )
        return True

    async def _handle_folder_selector(
        self, key: SessionKey, message_id: int, data: str, *, thread_id: int | None = None
    ) -> None:
        """Record a folder choice and run whatever message was waiting on it."""
        from ductor_bot.orchestrator.selectors.folder_selector import (
            folder_selector,
            parse_callback,
            resolve_choice,
        )

        # A panel posted before this conversation was recognised as General is
        # still on screen and still pressable, so the refusal lives here too and
        # not only where the buttons are offered.
        if self._orch.bindings.is_protected(key.storage_key):
            with contextlib.suppress(TelegramBadRequest):
                await self._bot.edit_message_text(
                    chat_id=key.chat_id,
                    message_id=message_id,
                    text=markdown_to_telegram_html(t("folder.general_locked")),
                    parse_mode=ParseMode.HTML,
                )
            return

        catalogue = self._roots_for(key)
        index = parse_callback(data)
        chosen = resolve_choice(catalogue, index) if index is not None else None
        if chosen is None:
            resp = folder_selector(self._orch, key, catalogue=catalogue)
            await edit_selector_response(self._bot, key.chat_id, message_id, resp)
            return

        self._orch.bindings.set(key.storage_key, chosen)
        label = chosen or t("folder.shared_label")
        with contextlib.suppress(TelegramBadRequest):
            await self._bot.edit_message_text(
                chat_id=key.chat_id,
                message_id=message_id,
                text=markdown_to_telegram_html(t("folder.selected", dir=label)),
                parse_mode=ParseMode.HTML,
            )

        held = self._orch.bindings.take(key.storage_key)
        if not held:
            return

        # The folder was only the first gate. Hand the held message back to the
        # persona gate rather than running it: skipping straight to execution
        # here would let a conversation start with no persona chosen.
        if await self._ask_persona_if_needed(key, held, thread_id=thread_id):
            return

        async with self._sequential.get_lock(key.lock_key):
            if self._config.streaming.enabled:
                sent = await self._bot.send_message(
                    key.chat_id, held, parse_mode=None, message_thread_id=thread_id
                )
                await self._handle_streaming(sent, key, held, thread_id=thread_id)
            else:
                await self._handle_non_streaming(None, key, held, thread_id=thread_id)

    async def _block_if_not_ready(
        self, key: SessionKey, *, thread_id: int | None = None
    ) -> bool:
        """Stop the turn when a protected handoff cannot be written here.

        Returns True when the message was refused, in which case the caller must
        not process it. Nothing is held: a queued message replayed later, after
        the user has fixed something and moved on, is its own surprise.
        """
        folder = self._orch.bindings.resolve(key.storage_key)
        result = check_readiness(folder, self._orch.paths)
        if result.ok:
            return False

        logger.warning("Handoff not ready: %s (%s)", result.key, result.detail)
        await self._bot.send_message(
            key.chat_id,
            markdown_to_telegram_html(t(result.key, detail=result.detail)),
            reply_markup=_retry_keyboard(),
            message_thread_id=thread_id,
            parse_mode=ParseMode.HTML,
        )
        return True

    async def _handle_handoff_retry(self, key: SessionKey, message_id: int) -> None:
        """Re-run the readiness check and say what it found now."""
        folder = self._orch.bindings.resolve(key.storage_key)
        result = check_readiness(folder, self._orch.paths)
        text = (
            t("handoff.ready_now")
            if result.ok
            else t(result.key, detail=result.detail)
        )
        with contextlib.suppress(TelegramBadRequest):
            await self._bot.edit_message_text(
                chat_id=key.chat_id,
                message_id=message_id,
                text=markdown_to_telegram_html(text),
                parse_mode=ParseMode.HTML,
                reply_markup=None if result.ok else _retry_keyboard(),
            )

    async def _ask_persona_if_needed(
        self, key: SessionKey, text: str, *, thread_id: int | None = None
    ) -> bool:
        """Ask which persona should govern a new conversation.

        Returns True when the message was held and the question asked, in which
        case the caller must not process it yet.
        """
        from ductor_bot.orchestrator.selectors.persona_selector import persona_selector

        if not self._config.persona_prompt:
            return False
        # Having answered is the only thing that stops the question. Gating on
        # "no active session" as well left every conversation that predates the
        # feature unable to ever be asked, and /new already clears the choice.
        if self._orch.personas.has_choice(key.storage_key):
            return False

        self._orch.personas.hold(key.storage_key, text)
        resp = persona_selector(self._orch, key, asking=True)
        await self._bot.send_message(
            key.chat_id,
            markdown_to_telegram_html(resp.text),
            reply_markup=button_grid_to_markup(resp.buttons),
            message_thread_id=thread_id,
            parse_mode=ParseMode.HTML,
        )
        return True

    async def _handle_persona_selector(
        self, key: SessionKey, message_id: int, data: str, *, thread_id: int | None = None
    ) -> None:
        """Apply a persona choice and run whatever message was waiting on it."""
        from ductor_bot.orchestrator.selectors.persona_selector import (
            parse_callback,
            persona_selector,
            resolve_choice,
        )

        index = parse_callback(data)
        chosen = resolve_choice(index) if index is not None else None
        if chosen is None:
            resp = persona_selector(self._orch, key)
            await edit_selector_response(self._bot, key.chat_id, message_id, resp)
            return

        self._orch.personas.set(key.storage_key, chosen)
        label = chosen or t("persona.default_label")
        with contextlib.suppress(TelegramBadRequest):
            await self._bot.edit_message_text(
                chat_id=key.chat_id,
                message_id=message_id,
                text=markdown_to_telegram_html(t("persona.selected", persona=label)),
                parse_mode=ParseMode.HTML,
            )

        held = self._orch.personas.take(key.storage_key)
        if not held:
            return
        async with self._sequential.get_lock(key.lock_key):
            if self._config.streaming.enabled:
                sent = await self._bot.send_message(
                    key.chat_id, held, parse_mode=None, message_thread_id=thread_id
                )
                await self._handle_streaming(sent, key, held, thread_id=thread_id)
            else:
                await self._handle_non_streaming(None, key, held, thread_id=thread_id)
    async def _apply_pending_edit(self, key: SessionKey, message_id: int) -> bool:
        """Carry out a confirmed rename or folder creation. True when handled."""
        from ductor_bot.files.edits import apply_newdir, apply_rename, validate_name

        edit = self._edit_store.get(key.storage_key)
        if edit is None or not edit.name or validate_name(edit.name):
            return False

        name = edit.name.strip()
        target = edit.target
        parent = target.parent if edit.kind == "rename" else target
        try:
            if edit.kind == "rename":
                apply_rename(target, name)
                notice = t("edits.renamed", old=target.name, new=name)
            else:
                apply_newdir(target, name)
                notice = t("edits.created", name=name)
        except FileExistsError:
            notice = t("edits.exists", name=name)
        except OSError:
            logger.exception("Edit failed for %s", target)
            notice = t("edits.failed", name=name)
        finally:
            self._edit_store.end(key.storage_key)

        text, keyboard = await asyncio.to_thread(
            _rendered_dir, self._orch.paths, self._config.project_roots, parent, notice
        )
        with contextlib.suppress(TelegramBadRequest):
            await self._bot.edit_message_text(
                text=markdown_to_telegram_html(text),
                chat_id=key.chat_id,
                message_id=message_id,
                reply_markup=keyboard,
                parse_mode=ParseMode.HTML,
            )
        return True

    async def _collect_edit_name(self, message: Message, key: SessionKey) -> bool:
        """Take a typed name into the pending edit and show it back for approval.

        Returns True when the message was consumed. Nothing is written here —
        the user still has to confirm, which is what proves the bot read the
        name they meant.
        """
        edit = self._edit_store.get(key.storage_key)
        if edit is None or not message.text:
            return False

        edit.name = message.text.strip()

        # The name was an answer to a question, not a message in the
        # conversation. Leaving it below the menu makes the exchange read
        # backwards — the menu updates above while the input sits at the
        # bottom — and it leaves folder names in the topic afterwards.
        # Best effort: deletion needs can_delete_messages, which is a group
        # setting the bot does not control.
        with contextlib.suppress(TelegramAPIError):
            await self._bot.delete_message(
                chat_id=message.chat.id, message_id=message.message_id
            )

        text, keyboard = await asyncio.to_thread(
            build_name_confirmation,
            self._orch.paths,
            self._config.project_roots,
            edit,
            edit.name,
        )
        html = markdown_to_telegram_html(text)
        thread_id = get_thread_id(message)
        if edit.message_id is not None:
            try:
                await self._bot.edit_message_text(
                    text=html,
                    chat_id=message.chat.id,
                    message_id=edit.message_id,
                    reply_markup=keyboard,
                    parse_mode=ParseMode.HTML,
                )
            except TelegramBadRequest:
                edit.message_id = None
        if edit.message_id is None:
            with contextlib.suppress(TelegramAPIError):
                sent = await self._bot.send_message(
                    message.chat.id,
                    html,
                    reply_markup=keyboard,
                    message_thread_id=thread_id,
                    parse_mode=ParseMode.HTML,
                )
                edit.message_id = sent.message_id
        return True

    async def _report_landing(self, message: Message, dest: Path) -> None:
        """Say where an attachment was saved.

        Without this the file simply vanishes from the user's point of view:
        the agent is told the path, the person who sent it never is.
        """
        with contextlib.suppress(TelegramAPIError):
            await self._bot.send_message(
                message.chat.id,
                markdown_to_telegram_html(
                    t("upload.landed", dir=_short_path(dest, self._orch.paths.ductor_home))
                ),
                message_thread_id=get_thread_id(message),
                parse_mode=ParseMode.HTML,
            )

    async def _stage_upload(self, message: Message, key: SessionKey) -> None:
        """Take an attachment into the open upload for *key* and re-render it."""
        session = self._uploads.get(key.storage_key)
        if session is None:
            return
        session.errors.clear()

        if session.mode == "folder":
            await self._stage_archive(message, session)
        else:
            received = await self._receive_into(message, session.staging)
            if received is None:
                session.errors.append(t("upload.download_failed"))

        await self._refresh_staging(message, key, session)

    async def _stage_archive(self, message: Message, session: UploadSession) -> None:
        """Unpack one archive into staging, refusing a second while one waits."""
        if session.archive is not None:
            session.errors.append(t("upload.zip_busy", name=session.archive))
            return

        scratch = Path(tempfile.mkdtemp(prefix="ductor_zip_in_"))
        try:
            received = await self._receive_into(message, scratch)
            if received is None:
                session.errors.append(t("upload.download_failed"))
                return
            if received.suffix.lower() != ".zip":
                session.errors.append(t("upload.expect_zip", name=received.name))
                return

            entries, error_key = await asyncio.to_thread(inspect_archive, received)
            if entries is None:
                session.errors.append(t(error_key, count=MAX_ENTRIES, mb=MAX_TOTAL_BYTES // 1048576))
                return

            await asyncio.to_thread(extract_archive, received, session.staging)
            session.archive = received.name
        except (OSError, ValueError):
            logger.exception("Failed to stage archive for %s", session.dest)
            session.errors.append(t("upload.download_failed"))
        finally:
            await asyncio.to_thread(partial(shutil.rmtree, scratch, ignore_errors=True))

    async def _receive_into(self, message: Message, target: Path) -> Path | None:
        """Download the attachment and place it flat in *target*.

        ``download_media`` files things under a dated subdirectory, which is
        right for the shared media folder and wrong for a staging area whose
        listing is shown to the user.
        """
        scratch = Path(tempfile.mkdtemp(prefix="ductor_recv_"))
        try:
            info = await download_media(self._bot, message, scratch)
            if info is None:
                return None
            out = target / info.file_name
            await asyncio.to_thread(_place, info.path, out)
        except (TelegramAPIError, OSError):
            logger.exception("Failed to receive attachment into %s", target)
            return None
        else:
            return out
        finally:
            await asyncio.to_thread(partial(shutil.rmtree, scratch, ignore_errors=True))

    async def _refresh_staging(
        self, message: Message, key: SessionKey, session: UploadSession
    ) -> None:
        """Update the one message that reports what is staged."""
        text, keyboard = await asyncio.to_thread(
            build_staging_view, self._orch.paths, self._config.project_roots, session
        )
        html = markdown_to_telegram_html(text)
        thread_id = get_thread_id(message)

        if session.message_id is not None:
            try:
                await self._bot.edit_message_text(
                    text=html,
                    chat_id=message.chat.id,
                    message_id=session.message_id,
                    reply_markup=keyboard,
                    parse_mode=ParseMode.HTML,
                )
            except TelegramBadRequest:
                # Editing fails if the text is unchanged or the message is gone;
                # a fresh one keeps the upload usable either way.
                session.message_id = None
            except TelegramAPIError:
                logger.exception("Failed to update staging message")
                return

        if session.message_id is None:
            with contextlib.suppress(TelegramAPIError):
                sent = await self._bot.send_message(
                    message.chat.id,
                    html,
                    reply_markup=keyboard,
                    message_thread_id=thread_id,
                    parse_mode=ParseMode.HTML,
                )
                session.message_id = sent.message_id

    @property
    def _uploads(self) -> UploadStore:
        """Staging registry, built on first use.

        The orchestrator — and therefore the paths — is not attached until
        startup, so this cannot be constructed in ``__init__``.
        """
        if self._upload_store is None:
            self._upload_store = UploadStore(self._orch.paths.uploads_staging_dir)
        return self._upload_store

    async def _set_seen_reaction(self, message: Message) -> None:
        """Set a seen reaction on the user message. Graceful degradation on failure."""
        try:
            from aiogram.types import ReactionTypeEmoji

            await self._bot.set_message_reaction(
                chat_id=message.chat.id,
                message_id=message.message_id,
                reaction=[ReactionTypeEmoji(emoji="\U0001f440")],
            )
        except Exception:
            logger.debug("Failed to set seen reaction", exc_info=True)

    async def _resolve_text(self, message: Message) -> str | None:
        """Extract processable text from *message* (plain text or media prompt)."""
        if should_drop_in_group(
            message,
            bot_id=self._bot_id,
            bot_username=self._bot_username,
            group_mention_only=self._config.group_mention_only,
        ):
            return None

        if has_media(message):
            paths = self._orch.paths
            key = get_session_key(message)

            # An open upload claims the attachment: it goes to staging and is
            # reported back, and the agent is not involved until the user has
            # confirmed where the file belongs.
            if self._uploads.get(key.storage_key) is not None:
                await self._stage_upload(message, key)
                return None

            # Land the upload in the topic's project directory when one is
            # configured, so "send a file to this topic" puts it where the agent
            # is working instead of a shared media folder it then has to copy from.
            dest = self._orch.resolve_topic_media_dir(message) or paths.telegram_files_dir
            media_prompt = await resolve_media_text(self._bot, message, dest, paths.workspace)
            if media_prompt is None:
                return None
            await self._report_landing(message, dest)
            return prepend_reply_to_media(message, media_prompt)
        if not message.text:
            return None
        text = strip_mention(message.text, self._bot_username)
        return build_reply_prompt(message, text)

    async def _handle_streaming(
        self, message: Message, key: SessionKey, text: str, *, thread_id: int | None = None
    ) -> None:
        """Streaming flow: coalescer -> stream editor -> Telegram."""
        await run_streaming_message(
            StreamingDispatch(
                bot=self._bot,
                orchestrator=self._orch,
                message=message,
                key=key,
                text=text,
                streaming_cfg=self._config.streaming,
                allowed_roots=self.file_roots(self._orch.paths),
                thread_id=thread_id,
                scene_config=self._config.scene,
            ),
        )

    async def _handle_non_streaming(
        self,
        reply_to: Message | None,
        key: SessionKey,
        text: str,
        *,
        thread_id: int | None = None,
    ) -> None:
        """Non-streaming flow: one-shot orchestrator call -> Telegram delivery.

        ``reply_to`` doubles as the user's trigger message (passed as
        ``message`` so the reaction tracker anchors consistently with the
        streaming path — MED #10).
        """
        await run_non_streaming_message(
            NonStreamingDispatch(
                bot=self._bot,
                orchestrator=self._orch,
                key=key,
                text=text,
                allowed_roots=self.file_roots(self._orch.paths),
                message=reply_to,
                reply_to=reply_to,
                thread_id=thread_id,
                scene_config=self._config.scene,
            ),
        )

    # -- Background handlers ---------------------------------------------------

    async def on_async_interagent_result(self, result: AsyncInterAgentResult) -> None:
        """Handle async inter-agent result via the message bus."""
        from ductor_bot.bus.adapters import (
            build_interagent_injection_prompt,
            from_interagent_result,
        )

        if result.transport and result.transport != "tg":
            logger.debug(
                "Skipping async interagent result for transport=%s in Telegram handler",
                result.transport,
            )
            return

        # Prefer the originating chat context carried by the result;
        # fall back to the sender agent's default DM.
        chat_id = result.chat_id or (
            self._config.allowed_user_ids[0] if self._config.allowed_user_ids else 0
        )
        if not chat_id:
            logger.warning("No chat_id available for async interagent result delivery")
            return
        set_log_context(operation="ia-async", chat_id=chat_id)

        injection_prompt = build_interagent_injection_prompt(
            result,
            agent_name=self._agent_name,
            transport_label="Telegram chat",
        )
        if injection_prompt:
            logger.info(
                "ia-async inject: task=%s from=%s prompt_len=%d",
                result.task_id,
                result.recipient or result.sender,
                len(injection_prompt),
            )

        await self._bus.submit(
            from_interagent_result(
                result,
                chat_id,
                injection_prompt=injection_prompt,
                transport="tg",
            )
        )

    async def _handle_webhook_wake(self, chat_id: int, prompt: str) -> str | None:
        """Process webhook wake prompt via the message bus."""
        from ductor_bot.bus.envelope import LockMode

        set_log_context(operation="wh", chat_id=chat_id)
        key = SessionKey(chat_id=chat_id)
        lock = self._lock_pool.get(key.lock_key)
        async with lock:
            result = await self._orch.handle_message(key, prompt)

        # Deliver result — lock already released, skip bus lock
        from ductor_bot.bus.adapters import from_webhook_wake

        env = from_webhook_wake(chat_id, prompt)
        env.result_text = result.text
        env.lock_mode = LockMode.NONE  # Lock already held above
        await self._bus.submit(env)
        return result.text

    # -- Update notifications --------------------------------------------------

    async def _on_update_available(self, info: VersionInfo) -> None:
        """Notify all users about a new version via Telegram."""
        from ductor_bot.messenger.telegram.upgrade_handler import on_update_available

        await on_update_available(self, info)

    async def _handle_upgrade_callback(
        self, chat_id: int, message_id: int, data: str, *, thread_id: int | None = None
    ) -> None:
        """Handle ``upg:yes:<version>``, ``upg:no``, and ``upg:cl:<version>`` callbacks."""
        from ductor_bot.messenger.telegram.upgrade_handler import handle_upgrade_callback

        await handle_upgrade_callback(self, chat_id, message_id, data, thread_id=thread_id)

    async def _sync_commands(self) -> None:
        from aiogram.types import BotCommandScopeAllGroupChats, BotCommandScopeAllPrivateChats

        desired = _BOT_COMMANDS

        # Clear legacy scoped commands (previous versions set per-scope lists).
        # Telegram keeps scoped commands independently — they must be deleted
        # explicitly or they shadow the default-scope list.
        for scope in (BotCommandScopeAllPrivateChats(), BotCommandScopeAllGroupChats()):
            try:
                scoped = await self._bot.get_my_commands(scope=scope)
                if scoped:
                    await self._bot.delete_my_commands(scope=scope)
                    logger.info("Cleared legacy %s commands", type(scope).__name__)
            except TelegramAPIError:
                pass  # scope not set — nothing to clear

        # Set default-scope commands (shown everywhere).
        # Compare as ordered list so reordering triggers an update.
        current = await self._bot.get_my_commands()
        current_tuples = [(c.command, c.description) for c in current]
        desired_tuples = [(c.command, c.description) for c in desired]
        if current_tuples != desired_tuples:
            await self._bot.set_my_commands(desired)
            logger.info("Updated %d bot commands", len(desired))

    async def _watch_restart_marker(self) -> None:
        """Poll for restart-requested marker file."""
        paths = self._orch.paths
        marker = paths.ductor_home / "restart-requested"
        try:
            while True:
                await asyncio.sleep(2.0)
                if await asyncio.to_thread(consume_restart_marker, marker_path=marker):
                    logger.info("Restart marker detected, stopping polling")
                    self._exit_code = EXIT_RESTART
                    await self._dp.stop_polling()
        except asyncio.CancelledError:
            logger.debug("Restart watcher cancelled")

    async def run(self) -> int:
        """Start polling. Returns exit code (0 = normal, 42 = restart)."""
        logger.info("Starting Telegram bot (aiogram, long-polling)...")
        await self._bot.delete_webhook(drop_pending_updates=True)
        # Flush any lingering polling session from a previous instance (e.g.
        # after /agent_restart).  offset=-1 confirms all pending updates and
        # immediately takes over the polling slot on Telegram's servers,
        # preventing TelegramConflictError on the first real getUpdates call.
        with contextlib.suppress(Exception):
            from aiogram.methods import GetUpdates

            await self._bot(GetUpdates(offset=-1, timeout=0))
        allowed_updates = self._dp.resolve_used_update_types()
        logger.info("Polling allowed_updates=%s", ",".join(allowed_updates))
        await self._dp.start_polling(
            self._bot,
            allowed_updates=allowed_updates,
            close_bot_session=True,
            handle_signals=False,
        )
        return self._exit_code

    async def shutdown(self) -> None:
        await _cancel_task(self._restart_watcher)
        await _cancel_task(self._group_audit_task)
        if self._update_observer:
            await self._update_observer.stop()
        if self._orchestrator:
            await self._orchestrator.shutdown()

        # Release the Telegram polling session so a new bot instance can start.
        # Without this, Telegram rejects the next getUpdates call with
        # TelegramConflictError ("terminated by other getUpdates request").
        with contextlib.suppress(Exception):
            await self._dp.stop_polling()
        with contextlib.suppress(Exception):
            await self._bot.delete_webhook(drop_pending_updates=False)
        with contextlib.suppress(Exception):
            await self._bot.session.close()

        logger.info("Telegram bot shut down")
