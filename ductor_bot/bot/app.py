"""Telegram bot: aiogram 3.x frontend for the orchestrator."""

from __future__ import annotations

import asyncio
import contextlib
import html as html_mod
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import BotCommand, FSInputFile, ReplyParameters

from ductor_bot.bot.formatting import markdown_to_telegram_html
from ductor_bot.bot.handlers import (
    handle_abort,
    handle_command,
    handle_new_session,
    strip_mention,
)
from ductor_bot.bot.media import has_media, is_media_addressed, resolve_media_text
from ductor_bot.bot.middleware import AuthMiddleware, SequentialMiddleware
from ductor_bot.bot.sender import send_files_from_text, send_rich
from ductor_bot.bot.streaming import create_stream_editor
from ductor_bot.bot.typing import TypingContext
from ductor_bot.bot.welcome import (
    build_welcome_keyboard,
    build_welcome_text,
    get_welcome_button_label,
    is_welcome_callback,
    resolve_welcome_callback,
)
from ductor_bot.cli.coalescer import CoalesceConfig, StreamCoalescer
from ductor_bot.commands import BOT_COMMANDS as _COMMAND_DEFS
from ductor_bot.config import AgentConfig
from ductor_bot.infra.restart import EXIT_RESTART, consume_restart_marker, consume_restart_sentinel
from ductor_bot.infra.updater import (
    UpdateObserver,
    consume_upgrade_sentinel,
    perform_upgrade,
    write_upgrade_sentinel,
)
from ductor_bot.infra.version import VersionInfo, get_current_version
from ductor_bot.log_context import set_log_context
from ductor_bot.workspace.paths import DuctorPaths

if TYPE_CHECKING:
    from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

    from ductor_bot.orchestrator.core import Orchestrator

logger = logging.getLogger(__name__)

_WELCOME_IMAGE = Path(__file__).resolve().parent / "ductor_images" / "welcome.png"
_CAPTION_LIMIT = 1024

_BOT_COMMANDS = [BotCommand(command=cmd, description=desc) for cmd, desc in _COMMAND_DEFS]

_HELP_TEXT = "\n".join(
    [
        "**ductor.dev -- Command Reference**",
        "",
        *(f"/{cmd} -- {desc}" for cmd, desc in _COMMAND_DEFS),
        "",
        "Just send a message to start working with your AI agent.",
    ],
)


async def _cancel_task(task: asyncio.Task[None] | None) -> None:
    """Cancel an asyncio task and suppress CancelledError."""
    if task and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


class TelegramBot:
    """Telegram frontend. All logic lives in the Orchestrator."""

    def __init__(self, config: AgentConfig) -> None:
        self._config = config
        self._orchestrator: Orchestrator | None = None

        self._bot = Bot(
            token=config.telegram_token,
            default=DefaultBotProperties(parse_mode=ParseMode.HTML),
        )
        self._bot_id: int | None = None
        self._bot_username: str | None = None

        self._dp = Dispatcher()
        self._router = Router(name="main")
        self._exit_code: int = 0
        self._restart_watcher: asyncio.Task[None] | None = None
        self._update_observer: UpdateObserver | None = None

        allowed = set(config.allowed_user_ids)
        self._sequential = SequentialMiddleware()
        self._sequential.set_abort_handler(self._on_abort)
        self._sequential.set_quick_command_handler(self._on_quick_command)
        self._router.message.outer_middleware(AuthMiddleware(allowed))
        self._router.message.outer_middleware(self._sequential)
        self._router.callback_query.outer_middleware(AuthMiddleware(allowed))

        self._register_handlers()
        self._dp.include_router(self._router)
        self._dp.startup.register(self._on_startup)

    @property
    def _orch(self) -> Orchestrator:
        if self._orchestrator is None:
            msg = "Orchestrator not initialized -- call after startup"
            raise RuntimeError(msg)
        return self._orchestrator

    def _file_roots(self, paths: DuctorPaths) -> list[Path]:
        """Allowed root directories for ``<file:...>`` tag sends."""
        return [paths.workspace]

    async def _on_startup(self) -> None:
        from ductor_bot.orchestrator.core import Orchestrator

        self._orchestrator = await Orchestrator.create(self._config)

        me = await self._bot.get_me()
        self._bot_id = me.id
        self._bot_username = (me.username or "").lower()
        logger.info("Bot online: @%s (id=%d)", me.username, me.id)

        sentinel_path = self._orch.paths.ductor_home / "restart-sentinel.json"
        sentinel = await asyncio.to_thread(consume_restart_sentinel, sentinel_path=sentinel_path)
        if sentinel:
            chat_id = int(sentinel.get("chat_id", 0))
            msg = str(sentinel.get("message", "Restart completed."))
            if chat_id:
                await send_rich(
                    self._bot, chat_id, msg, allowed_roots=self._file_roots(self._orch.paths)
                )

        self._orchestrator.set_cron_result_handler(self._on_cron_result)
        self._orchestrator.set_heartbeat_handler(self._on_heartbeat_result)
        self._orchestrator.set_webhook_result_handler(self._on_webhook_result)
        self._orchestrator.set_webhook_wake_handler(self._handle_webhook_wake)

        # Check for post-upgrade notification
        upgrade = await asyncio.to_thread(consume_upgrade_sentinel, self._orch.paths.ductor_home)
        if upgrade:
            uid = int(upgrade.get("chat_id", 0))
            old_v = upgrade.get("old_version", "?")
            new_v = upgrade.get("new_version", get_current_version())
            if uid:
                await send_rich(
                    self._bot,
                    uid,
                    f"**Upgrade complete** `{old_v}` -> `{new_v}`",
                    allowed_roots=self._file_roots(self._orch.paths),
                )

        # Start background version checker (skip for dev/source installs)
        from ductor_bot.infra.install import is_upgradeable

        if is_upgradeable():
            self._update_observer = UpdateObserver(notify=self._on_update_available)
            self._update_observer.start()

        await self._sync_commands()
        self._restart_watcher = asyncio.create_task(self._watch_restart_marker())

    def _register_handlers(self) -> None:
        r = self._router
        r.message(CommandStart())(self._on_start)
        r.message(Command("help"))(self._on_help)
        r.message(Command("stop"))(self._on_stop)
        r.message(Command("restart"))(self._on_restart)
        r.message(Command("new"))(self._on_new)
        for cmd in ("status", "memory", "model", "cron", "diagnose", "upgrade"):
            r.message(Command(cmd))(self._on_command)
        r.message()(self._on_message)
        r.callback_query()(self._on_callback_query)

    # -- Welcome & help ---------------------------------------------------------

    async def _show_welcome(self, message: Message) -> None:
        """Send the welcome screen with auth status and quick-start buttons."""
        from ductor_bot.cli.auth import check_all_auth

        chat_id = message.chat.id
        user_name = message.from_user.first_name if message.from_user else ""

        auth_results = await asyncio.to_thread(check_all_auth)
        text = build_welcome_text(user_name, auth_results, self._config)
        keyboard = build_welcome_keyboard()

        sent_with_image = await self._send_welcome_image(chat_id, text, keyboard, message)
        if not sent_with_image:
            await send_rich(self._bot, chat_id, text, reply_to=message, reply_markup=keyboard)

    async def _send_welcome_image(
        self,
        chat_id: int,
        text: str,
        keyboard: InlineKeyboardMarkup,
        reply_to: Message,
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
            )
        except TelegramBadRequest:
            logger.warning("Welcome image caption failed, retrying without")
            try:
                await self._bot.send_photo(
                    chat_id=chat_id,
                    photo=FSInputFile(_WELCOME_IMAGE),
                    reply_parameters=ReplyParameters(message_id=reply_to.message_id),
                )
            except Exception:
                logger.exception("Failed to send welcome image")
                return False
            return False
        except Exception:
            logger.exception("Failed to send welcome image")
            return False
        return html_caption is not None

    async def _on_start(self, message: Message) -> None:
        """Handle /start: always show welcome screen."""
        await self._show_welcome(message)

    async def _on_help(self, message: Message) -> None:
        """Handle /help: show command reference."""
        await send_rich(self._bot, message.chat.id, _HELP_TEXT, reply_to=message)

    # -- Abort, commands, sessions ---------------------------------------------

    async def _on_abort(self, chat_id: int, message: Message) -> bool:
        return await handle_abort(
            self._orchestrator,
            self._bot,
            chat_id=chat_id,
            message=message,
        )

    async def _on_quick_command(self, chat_id: int, message: Message) -> bool:
        """Handle a read-only command without the sequential lock."""
        if self._orchestrator is None:
            return False
        await handle_command(self._orchestrator, self._bot, message)
        return True

    async def _on_stop(self, message: Message) -> None:
        await handle_abort(
            self._orchestrator,
            self._bot,
            chat_id=message.chat.id,
            message=message,
        )

    async def _on_command(self, message: Message) -> None:
        await handle_command(self._orch, self._bot, message)

    async def _on_new(self, message: Message) -> None:
        await handle_new_session(self._orch, self._bot, message)

    async def _on_restart(self, message: Message) -> None:
        from ductor_bot.infra.restart import write_restart_sentinel

        chat_id = message.chat.id
        paths = self._orch.paths
        sentinel = paths.ductor_home / "restart-sentinel.json"
        await asyncio.to_thread(
            write_restart_sentinel, chat_id, "Restart completed.", sentinel_path=sentinel
        )
        await message.answer("Bot is restarting...")
        self._exit_code = EXIT_RESTART
        asyncio.create_task(self._dp.stop_polling())  # noqa: RUF006

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

        if data.startswith("upg:"):
            await self._handle_upgrade_callback(chat_id, msg.message_id, data)
            return

        from ductor_bot.orchestrator.model_selector import is_model_selector_callback

        if is_model_selector_callback(data):
            await self._handle_model_selector(chat_id, msg.message_id, data)
            return

        await self._mark_button_choice(chat_id, msg, display_label)

        async with self._sequential.get_lock(chat_id):
            if self._config.streaming.enabled:
                await self._handle_streaming(msg, chat_id, data)
            else:
                async with TypingContext(self._bot, chat_id):
                    result = await self._orch.handle_message(chat_id, data)
                roots = self._file_roots(self._orch.paths)
                await send_rich(self._bot, chat_id, result.text, allowed_roots=roots)

    async def _handle_model_selector(self, chat_id: int, message_id: int, data: str) -> None:
        """Handle model selector wizard by editing the message in-place.

        Acquires the per-chat lock so model switches are atomic with respect
        to active CLI calls and webhook wake dispatch.
        """
        from ductor_bot.orchestrator.model_selector import handle_model_callback

        async with self._sequential.get_lock(chat_id):
            text, keyboard = await handle_model_callback(self._orch, chat_id, data)
        with contextlib.suppress(TelegramBadRequest):
            await self._bot.edit_message_text(
                text=text,
                chat_id=chat_id,
                message_id=message_id,
                reply_markup=keyboard,
            )

    async def _mark_button_choice(self, chat_id: int, msg: Message, label: str) -> None:
        """Edit the bot message to append ``[USER ANSWER] label`` and remove the keyboard.

        Falls back to keyboard-only removal when the message is a caption
        (photo/video) or the updated text would exceed Telegram limits.
        """
        if msg.text is not None:
            original_html = msg.html_text or msg.text
            escaped = html_mod.escape(label)
            updated = f"{original_html}\n\n<i>[USER ANSWER] {escaped}</i>"
            try:
                await self._bot.edit_message_text(
                    text=updated,
                    chat_id=chat_id,
                    message_id=msg.message_id,
                    parse_mode=ParseMode.HTML,
                    reply_markup=None,
                )
            except TelegramBadRequest:
                pass
            else:
                return

        with contextlib.suppress(TelegramBadRequest):
            await self._bot.edit_message_reply_markup(
                chat_id=chat_id,
                message_id=msg.message_id,
                reply_markup=None,
            )

    # -- Messages --------------------------------------------------------------

    async def _on_message(self, message: Message) -> None:
        text = await self._resolve_text(message)
        if text is None:
            return

        chat_id = message.chat.id
        logger.debug("Message text=%s", text[:80])

        if self._config.streaming.enabled:
            await self._handle_streaming(message, chat_id, text)
        else:
            async with TypingContext(self._bot, chat_id):
                result = await self._orch.handle_message(chat_id, text)
            roots = self._file_roots(self._orch.paths)
            await send_rich(self._bot, chat_id, result.text, reply_to=message, allowed_roots=roots)

    async def _resolve_text(self, message: Message) -> str | None:
        """Extract processable text from *message* (plain text or media prompt)."""
        if has_media(message):
            is_group = message.chat.type in ("group", "supergroup")
            if is_group and not is_media_addressed(message, self._bot_id, self._bot_username):
                return None
            paths = self._orch.paths
            return await resolve_media_text(
                self._bot, message, paths.telegram_files_dir, paths.workspace
            )
        if not message.text:
            return None
        return strip_mention(message.text, self._bot_username)

    async def _handle_streaming(self, message: Message, chat_id: int, text: str) -> None:
        """Streaming flow: coalescer -> stream editor -> Telegram."""
        logger.info("Streaming flow started")
        cfg = self._config.streaming
        editor = create_stream_editor(
            self._bot,
            chat_id,
            reply_to=message,
            cfg=cfg,
        )
        coalescer = StreamCoalescer(
            config=CoalesceConfig(
                min_chars=cfg.min_chars,
                max_chars=cfg.max_chars,
                idle_ms=cfg.idle_ms,
                sentence_break=cfg.sentence_break,
            ),
            on_flush=editor.append_text,
        )

        async def on_text(delta: str) -> None:
            await coalescer.feed(delta)

        async def on_tool(tool_name: str) -> None:
            await coalescer.flush(force=True)
            await editor.append_tool(tool_name)

        async def on_system(status: str | None) -> None:
            if status == "thinking":
                await coalescer.flush(force=True)
                await editor.append_system("THINKING")
            elif status == "compacting":
                await coalescer.flush(force=True)
                await editor.append_system("COMPACTING")

        async with TypingContext(self._bot, chat_id):
            result = await self._orch.handle_message_streaming(
                chat_id,
                text,
                on_text_delta=on_text,
                on_tool_activity=on_tool,
                on_system_status=on_system,
            )

        await coalescer.flush(force=True)
        coalescer.stop()
        await editor.finalize(result.text)
        logger.info(
            "Streaming flow completed fallback=%s content=%s",
            result.stream_fallback,
            editor.has_content,
        )

        roots = self._file_roots(self._orch.paths)
        if result.stream_fallback or not editor.has_content:
            await send_rich(self._bot, chat_id, result.text, reply_to=message, allowed_roots=roots)
        else:
            # Streaming sent text already; extract and deliver any <file:...> tags.
            await send_files_from_text(self._bot, chat_id, result.text, allowed_roots=roots)

    # -- Background handlers ---------------------------------------------------

    async def _on_cron_result(self, title: str, result: str, status: str) -> None:
        """Send cron job result to all allowed users."""
        text = f"**TASK: {title}**\n\n{result}" if result else f"**TASK: {title}**\n\n_{status}_"
        roots = self._file_roots(self._orch.paths)
        for uid in self._config.allowed_user_ids:
            await send_rich(self._bot, uid, text, allowed_roots=roots)

    async def _on_heartbeat_result(self, chat_id: int, text: str) -> None:
        """Send heartbeat alert to the user."""
        logger.debug("Heartbeat delivery chars=%d", len(text))
        await send_rich(self._bot, chat_id, text, allowed_roots=self._file_roots(self._orch.paths))
        logger.info("Heartbeat delivered")

    async def _handle_webhook_wake(self, chat_id: int, prompt: str) -> str | None:
        """Process webhook wake prompt through the normal message pipeline.

        Acquires the per-chat lock (queues behind active conversations),
        processes the prompt through the standard orchestrator path, and
        sends the response to Telegram like a normal message.
        """
        set_log_context(operation="wh", chat_id=chat_id)
        lock = self._sequential.get_lock(chat_id)
        async with lock:
            result = await self._orch.handle_message(chat_id, prompt)
        roots = self._file_roots(self._orch.paths)
        await send_rich(self._bot, chat_id, result.text, allowed_roots=roots)
        return result.text

    async def _on_webhook_result(self, result: object) -> None:
        """Send webhook cron_task result to all allowed users.

        Wake mode results are already sent to Telegram by ``_handle_webhook_wake``.
        """
        from ductor_bot.webhook.models import WebhookResult

        if not isinstance(result, WebhookResult):
            return
        if result.mode == "wake":
            return
        if result.result_text:
            text = f"**WEBHOOK (CRON TASK): {result.hook_title}**\n\n{result.result_text}"
        else:
            text = f"**WEBHOOK (CRON TASK): {result.hook_title}**\n\n_{result.status}_"
        roots = self._file_roots(self._orch.paths)
        for uid in self._config.allowed_user_ids:
            await send_rich(self._bot, uid, text, allowed_roots=roots)

    # -- Update notifications --------------------------------------------------

    async def _on_update_available(self, info: VersionInfo) -> None:
        """Notify all users about a new version via Telegram."""
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text="Upgrade now",
                        callback_data=f"upg:yes:{info.latest}",
                    ),
                    InlineKeyboardButton(text="Later", callback_data="upg:no"),
                ],
            ],
        )
        text = (
            f"**New version available**\n\n"
            f"Installed: `{info.current}`\n"
            f"New:       `{info.latest}`\n\n"
            f"_{info.summary}_"
        )
        for uid in self._config.allowed_user_ids:
            await send_rich(self._bot, uid, text, reply_markup=keyboard)

    async def _handle_upgrade_callback(self, chat_id: int, message_id: int, data: str) -> None:
        """Handle ``upg:yes:<version>`` and ``upg:no`` callbacks."""
        with contextlib.suppress(TelegramBadRequest):
            await self._bot.edit_message_reply_markup(
                chat_id=chat_id, message_id=message_id, reply_markup=None
            )

        if data == "upg:no":
            with contextlib.suppress(TelegramBadRequest):
                await self._bot.edit_message_text(
                    text="Upgrade skipped.",
                    chat_id=chat_id,
                    message_id=message_id,
                )
            return

        # upg:yes:<version>
        target_version = data.split(":", 2)[2] if data.count(":") >= 2 else "latest"
        current = get_current_version()

        await self._bot.send_message(
            chat_id,
            f"Upgrading to {target_version}...",
            parse_mode=None,
        )

        success, output = await perform_upgrade()
        if not success:
            logger.error("Upgrade failed: %s", output[-500:])
            await self._bot.send_message(
                chat_id,
                f"Upgrade failed:\n{output[-300:]}",
                parse_mode=None,
            )
            return

        # Write sentinel for post-restart message
        await asyncio.to_thread(
            write_upgrade_sentinel,
            self._orch.paths.ductor_home,
            chat_id=chat_id,
            old_version=current,
            new_version=target_version,
        )

        await self._bot.send_message(chat_id, "Bot is restarting...", parse_mode=None)
        self._exit_code = EXIT_RESTART
        asyncio.create_task(self._dp.stop_polling())  # noqa: RUF006

    async def _sync_commands(self) -> None:
        current = await self._bot.get_my_commands()
        current_set = {(c.command, c.description) for c in current}
        desired_set = {(c.command, c.description) for c in _BOT_COMMANDS}
        if current_set != desired_set:
            await self._bot.set_my_commands(_BOT_COMMANDS)
            logger.info("Updated %d bot commands", len(_BOT_COMMANDS))

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
        allowed_updates = self._dp.resolve_used_update_types()
        logger.info("Polling allowed_updates=%s", ",".join(allowed_updates))
        await self._dp.start_polling(
            self._bot,
            allowed_updates=allowed_updates,
            close_bot_session=True,
        )
        return self._exit_code

    async def shutdown(self) -> None:
        await _cancel_task(self._restart_watcher)
        if self._update_observer:
            await self._update_observer.stop()
        if self._orchestrator:
            await self._orchestrator.shutdown()
        logger.info("Telegram bot shut down")
