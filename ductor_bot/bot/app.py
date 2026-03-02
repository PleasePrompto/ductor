"""Telegram bot: aiogram 3.x frontend for the orchestrator."""

from __future__ import annotations

import asyncio
import contextlib
import html as html_mod
import json
import logging
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING

from aiogram import Bot, Dispatcher, Router
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramAPIError, TelegramBadRequest
from aiogram.filters import Command, CommandStart
from aiogram.types import BotCommand, FSInputFile, ReplyParameters

from ductor_bot.background import BackgroundResult
from ductor_bot.bot.buttons import extract_buttons_for_session
from ductor_bot.bot.file_browser import (
    file_browser_start,
    handle_file_browser_callback,
    is_file_browser_callback,
)
from ductor_bot.bot.formatting import markdown_to_telegram_html
from ductor_bot.bot.handlers import (
    handle_abort,
    handle_abort_all,
    handle_command,
    handle_new_session,
    strip_mention,
)
from ductor_bot.bot.media import (
    has_media,
    is_media_addressed,
    is_message_addressed,
    resolve_media_text,
)
from ductor_bot.bot.message_dispatch import (
    NonStreamingDispatch,
    StreamingDispatch,
    run_non_streaming_message,
    run_streaming_message,
)
from ductor_bot.bot.middleware import MQ_PREFIX, AuthMiddleware, SequentialMiddleware
from ductor_bot.bot.sender import SendRichOpts, send_rich
from ductor_bot.bot.sender import send_files_from_text as _send_files_from_text
from ductor_bot.bot.topic import get_thread_id
from ductor_bot.bot.typing import TypingContext as _TypingContext
from ductor_bot.bot.welcome import (
    build_welcome_keyboard,
    build_welcome_text,
    get_welcome_button_label,
    is_welcome_callback,
    resolve_welcome_callback,
)
from ductor_bot.commands import BOT_COMMANDS as _COMMAND_DEFS
from ductor_bot.commands import MULTIAGENT_SUB_COMMANDS as _MA_SUB_DEFS
from ductor_bot.config import AgentConfig
from ductor_bot.files.allowed_roots import resolve_allowed_roots
from ductor_bot.infra.restart import EXIT_RESTART, consume_restart_marker, consume_restart_sentinel
from ductor_bot.infra.updater import (
    UpdateObserver,
    consume_upgrade_sentinel,
    perform_upgrade_pipeline,
    write_upgrade_sentinel,
)
from ductor_bot.infra.version import VersionInfo, get_current_version
from ductor_bot.log_context import set_log_context
from ductor_bot.multiagent.bus import AsyncInterAgentResult
from ductor_bot.text.response_format import SEP, fmt
from ductor_bot.workspace.paths import DuctorPaths

if TYPE_CHECKING:
    from aiogram.types import CallbackQuery, InlineKeyboardMarkup, Message

    from ductor_bot.orchestrator.core import Orchestrator

logger = logging.getLogger(__name__)

_WELCOME_IMAGE = Path(__file__).resolve().parent / "ductor_images" / "welcome.png"
_CAPTION_LIMIT = 1024
_CRON_ACK_MARKERS = ("message sent successfully", "delivered to telegram")

# Backward-compatible patch points used by tests.
TypingContext = _TypingContext
send_files_from_text = _send_files_from_text

_BOT_COMMANDS = [BotCommand(command=cmd, description=desc) for cmd, desc in _COMMAND_DEFS]

_CMD_DESC: dict[str, str] = {**dict(_COMMAND_DEFS), **dict(_MA_SUB_DEFS)}


def _help_line(command: str) -> str:
    """Return one command line for the help panel."""
    description = _CMD_DESC.get(command, "")
    return f"/{command} -- {description}" if description else f"/{command}"


def _is_cron_transport_ack_line(line: str) -> bool:
    """Return True for known transport confirmation lines from task tools."""
    normalized = " ".join(line.lower().split())
    return all(marker in normalized for marker in _CRON_ACK_MARKERS)


def _sanitize_cron_result_text(result: str) -> str:
    """Strip tool transport confirmations from cron result text."""
    if not result:
        return ""
    lines = [line for line in result.splitlines() if not _is_cron_transport_ack_line(line)]
    return "\n".join(lines).strip()


_HELP_TEXT = fmt(
    "**Command Reference**",
    SEP,
    f"Daily\n{_help_line('new')}\n{_help_line('stop')}\n{_help_line('stop_all')}\n"
    f"{_help_line('model')}\n{_help_line('status')}\n{_help_line('memory')}",
    f"Automation\n{_help_line('session')}\n{_help_line('cron')}",
    f"Multi-Agent\n{_help_line('agent_commands')}",
    f"Browse & Info\n{_help_line('showfiles')}\n{_help_line('info')}\n{_help_line('help')}",
    f"Maintenance\n{_help_line('diagnose')}\n{_help_line('upgrade')}\n{_help_line('restart')}",
    SEP,
    "Send any message to start working with your agent.",
)


async def _cancel_task(task: asyncio.Task[None] | None) -> None:
    """Cancel an asyncio task and suppress CancelledError."""
    if task and not task.done():
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


def _read_boot_id() -> str:
    """Read Linux boot ID when available (empty string on failure)."""
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def _load_startup_state(path: Path) -> dict[str, str]:
    """Load persisted startup state from JSON."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    return {str(k): str(v) for k, v in data.items()}


def _save_startup_state(path: Path, state: dict[str, str]) -> None:
    """Persist startup state JSON atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _load_inflight_turns(path: Path) -> dict[str, dict[str, object]]:
    """Load per-chat inflight foreground turns from disk."""
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return {}
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return {}
    if not isinstance(data, dict):
        return {}
    chats = data.get("chats")
    if not isinstance(chats, dict):
        return {}
    out: dict[str, dict[str, object]] = {}
    for key, value in chats.items():
        if isinstance(value, dict):
            out[str(key)] = dict(value)
    return out


def _save_inflight_turns(path: Path, turns: dict[str, dict[str, object]]) -> None:
    """Persist per-chat inflight foreground turns atomically."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    payload = {"chats": turns}
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)


def _parse_utc_iso(ts: str) -> datetime | None:
    """Parse ISO timestamp to UTC datetime (best effort)."""
    if not ts:
        return None
    try:
        parsed = datetime.fromisoformat(ts)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


class TelegramBot:
    """Telegram frontend. All logic lives in the Orchestrator."""

    def __init__(self, config: AgentConfig, *, agent_name: str = "main") -> None:
        self._config = config
        self._agent_name = agent_name
        self._orchestrator: Orchestrator | None = None
        self._abort_all_callback: Callable[[], Awaitable[int]] | None = None

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
        self._upgrade_lock = asyncio.Lock()

        allowed = set(config.allowed_user_ids)
        self._sequential = SequentialMiddleware()
        self._sequential.set_bot(self._bot)
        self._sequential.set_abort_handler(self._on_abort)
        self._sequential.set_abort_all_handler(self._on_abort_all)
        self._sequential.set_quick_command_handler(self._on_quick_command)
        auth = AuthMiddleware(allowed, group_mention_only=config.group_mention_only)
        self._router.message.outer_middleware(auth)
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

    def _file_roots(self, paths: DuctorPaths) -> list[Path] | None:
        """Allowed root directories for ``<file:...>`` tag sends."""
        return resolve_allowed_roots(self._config.file_access, paths.workspace)

    async def _broadcast(self, text: str, opts: SendRichOpts | None = None) -> None:
        """Send a message to all allowed users."""
        for uid in self._config.allowed_user_ids:
            await send_rich(self._bot, uid, text, opts)

    async def _notify_startup_status(self) -> None:
        """Notify allowed users when service is started/restarted/rebooted."""
        state_path = self._orch.paths.ductor_home / "startup-state.json"
        prev = await asyncio.to_thread(_load_startup_state, state_path)
        now = datetime.now(UTC)
        now_label = now.strftime("%Y-%m-%d %H:%M:%S UTC")
        current_boot_id = await asyncio.to_thread(_read_boot_id)

        prev_boot_id = prev.get("boot_id", "")
        prev_started_at = prev.get("started_at", "")

        if prev_boot_id and current_boot_id and prev_boot_id != current_boot_id:
            title = "System reboot detected"
            body = f"Ductor service is online again.\nTime: {now_label}"
        elif prev_started_at:
            title = "Service restarted"
            body = f"Ductor service restarted and is online.\nTime: {now_label}"
        else:
            title = "Service started"
            body = f"Ductor service is online.\nTime: {now_label}"

        await self._broadcast(
            fmt(f"**{title}**", SEP, body),
            SendRichOpts(allowed_roots=self._file_roots(self._orch.paths)),
        )

        await asyncio.to_thread(
            _save_startup_state,
            state_path,
            {
                "boot_id": current_boot_id,
                "started_at": now.isoformat(),
            },
        )

    def _inflight_turns_path(self) -> Path:
        """Return the JSON path used for crash-safe foreground turn recovery."""
        return self._orch.paths.ductor_home / "inflight-turns.json"

    async def _mark_inflight_turn(
        self,
        chat_id: int,
        text: str,
        *,
        message_id: int,
        thread_id: int | None,
    ) -> None:
        """Persist the current foreground turn before execution starts."""
        path = self._inflight_turns_path()
        turns = await asyncio.to_thread(_load_inflight_turns, path)
        turns[str(chat_id)] = {
            "chat_id": chat_id,
            "text": text,
            "message_id": message_id,
            "thread_id": thread_id,
            "created_at": datetime.now(UTC).isoformat(),
            "attempts": 0,
        }
        await asyncio.to_thread(_save_inflight_turns, path, turns)

    async def _clear_inflight_turn(self, chat_id: int) -> None:
        """Remove persisted foreground turn after successful completion."""
        path = self._inflight_turns_path()
        turns = await asyncio.to_thread(_load_inflight_turns, path)
        if str(chat_id) not in turns:
            return
        turns.pop(str(chat_id), None)
        await asyncio.to_thread(_save_inflight_turns, path, turns)

    async def _resume_inflight_turns(self) -> None:
        """Auto-resume unfinished foreground turns detected after restart."""
        path = self._inflight_turns_path()
        turns = await asyncio.to_thread(_load_inflight_turns, path)
        if not turns:
            logger.info("No inflight foreground turns to resume")
            return

        logger.info("Found %d inflight foreground turn(s) for auto-resume", len(turns))
        now = datetime.now(UTC)
        changed = False
        for key in list(turns):
            payload = turns.get(key, {})
            chat_id = int(payload.get("chat_id", 0) or 0)
            text = str(payload.get("text", "") or "").strip()
            if chat_id <= 0 or not text:
                turns.pop(key, None)
                changed = True
                continue

            attempts = int(payload.get("attempts", 0) or 0)
            created_at = _parse_utc_iso(str(payload.get("created_at", "") or ""))
            if created_at and (now - created_at).total_seconds() > 2 * 60 * 60:
                turns.pop(key, None)
                changed = True
                continue
            if attempts >= 3:
                turns.pop(key, None)
                changed = True
                await send_rich(
                    self._bot,
                    chat_id,
                    fmt(
                        "**Auto-resume skipped**",
                        SEP,
                        "A previous task failed to auto-resume multiple times. "
                        "Send `continue from where you left off` to retry manually.",
                    ),
                    SendRichOpts(allowed_roots=self._file_roots(self._orch.paths)),
                )
                continue

            thread_raw = payload.get("thread_id")
            thread_id = int(thread_raw) if isinstance(thread_raw, int) else None
            logger.info("Auto-resuming inflight turn chat=%d attempts=%d", chat_id, attempts + 1)

            try:
                await send_rich(
                    self._bot,
                    chat_id,
                    fmt(
                        "**Resuming previous work**",
                        SEP,
                        "Service restart was detected. Continuing the interrupted task automatically.",
                    ),
                    SendRichOpts(
                        allowed_roots=self._file_roots(self._orch.paths), thread_id=thread_id
                    ),
                )
                await self._handle_non_streaming(None, chat_id, text, thread_id=thread_id)
            except Exception:
                logger.exception("Failed to auto-resume foreground turn chat=%d", chat_id)
                payload["attempts"] = attempts + 1
                turns[key] = payload
                changed = True
                with contextlib.suppress(Exception):
                    await send_rich(
                        self._bot,
                        chat_id,
                        fmt(
                            "**Auto-resume failed**",
                            SEP,
                            "The interrupted task could not be resumed automatically. "
                            "Send `continue from where you left off` and I will continue from context.",
                        ),
                        SendRichOpts(
                            allowed_roots=self._file_roots(self._orch.paths), thread_id=thread_id
                        ),
                    )
            else:
                turns.pop(key, None)
                changed = True

        if changed:
            await asyncio.to_thread(_save_inflight_turns, path, turns)

    def _build_named_session_resume_prompt(self, last_prompt: str, has_session_id: bool) -> str:
        """Build recovery prompt for interrupted named background sessions."""
        if has_session_id:
            return (
                "Service restart interrupted your work. Continue from the latest unfinished step "
                "in this same session. Do not repeat completed work. Then provide a concise "
                "progress update and next actions."
            )
        base = last_prompt.strip()
        if base:
            return base
        return (
            "Service restart interrupted this task. Continue the previous work and provide "
            "the latest result."
        )

    async def _resume_interrupted_named_sessions(self) -> None:
        """Auto-resume named background sessions that were running before restart."""
        sessions = self._orch.pop_recovered_named_sessions()
        if not sessions:
            logger.info("No interrupted named sessions to auto-resume")
            return

        logger.info("Found %d interrupted named session(s) for auto-resume", len(sessions))
        for ns in sessions:
            # Inter-agent sessions are resumed by explicit bus traffic, not startup replay.
            if ns.name.startswith("ia-"):
                continue

            prompt = self._build_named_session_resume_prompt(ns.last_prompt, bool(ns.session_id))
            try:
                logger.info("Auto-resuming named session name=%s chat=%d", ns.name, ns.chat_id)
                await send_rich(
                    self._bot,
                    ns.chat_id,
                    fmt(
                        f"**[{ns.name}] Auto-resuming**",
                        SEP,
                        "Service restart was detected. Resuming this background session now.",
                    ),
                    SendRichOpts(allowed_roots=self._file_roots(self._orch.paths)),
                )
                self._orch.submit_named_followup_bg(
                    ns.chat_id,
                    ns.name,
                    prompt,
                    message_id=0,
                    thread_id=None,
                )
            except Exception:
                logger.exception("Failed to auto-resume named session name=%s", ns.name)
                with contextlib.suppress(Exception):
                    await send_rich(
                        self._bot,
                        ns.chat_id,
                        fmt(
                            f"**[{ns.name}] Auto-resume failed**",
                            SEP,
                            f"Send `@{ns.name} continue from where you left off` to continue manually.",
                        ),
                        SendRichOpts(allowed_roots=self._file_roots(self._orch.paths)),
                    )

    async def _on_startup(self) -> None:
        from ductor_bot.orchestrator.core import Orchestrator

        self._orchestrator = await Orchestrator.create(
            self._config,
            agent_name=self._agent_name,
        )

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
                    self._bot,
                    chat_id,
                    msg,
                    SendRichOpts(
                        allowed_roots=self._file_roots(self._orch.paths),
                    ),
                )

        await self._notify_startup_status()

        self._orchestrator.set_cron_result_handler(self._on_cron_result)
        self._orchestrator.set_heartbeat_handler(self._on_heartbeat_result)
        self._orchestrator.set_webhook_result_handler(self._on_webhook_result)
        self._orchestrator.set_webhook_wake_handler(self._handle_webhook_wake)
        self._orchestrator.set_session_result_handler(self._on_session_result)
        await self._resume_interrupted_named_sessions()
        await self._resume_inflight_turns()

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
                    SendRichOpts(
                        allowed_roots=self._file_roots(self._orch.paths),
                    ),
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
        r.message(CommandStart(ignore_case=True))(self._on_start)
        r.message(Command("help", ignore_case=True))(self._on_help)
        r.message(Command("info", ignore_case=True))(self._on_info)
        r.message(Command("stop_all", ignore_case=True))(self._on_stop_all)
        r.message(Command("stop", ignore_case=True))(self._on_stop)
        r.message(Command("restart", ignore_case=True))(self._on_restart)
        r.message(Command("new", ignore_case=True))(self._on_new)
        r.message(Command("session", ignore_case=True))(self._on_session)
        r.message(Command("sessions", ignore_case=True))(self._on_sessions)
        r.message(Command("showfiles", ignore_case=True))(self._on_showfiles)
        r.message(Command("agent_commands", ignore_case=True))(self._on_agent_commands)
        base_cmds = ["status", "memory", "model", "cron", "diagnose", "upgrade"]
        if self._agent_name == "main":
            base_cmds += ["agents", "agent_start", "agent_stop", "agent_restart"]
        for cmd in base_cmds:
            r.message(Command(cmd, ignore_case=True))(self._on_command)
        r.message()(self._on_message)
        r.callback_query()(self._on_callback_query)

    # -- Welcome & help ---------------------------------------------------------

    async def _show_welcome(self, message: Message) -> None:
        """Send the welcome screen with auth status and quick-start buttons."""
        from ductor_bot.cli.auth import check_all_auth

        chat_id = message.chat.id
        thread_id = get_thread_id(message)
        user_name = message.from_user.first_name if message.from_user else ""

        auth_results = await asyncio.to_thread(check_all_auth)
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
        await self._show_welcome(message)

    async def _on_help(self, message: Message) -> None:
        """Handle /help: show command reference."""
        await send_rich(
            self._bot,
            message.chat.id,
            _HELP_TEXT,
            SendRichOpts(reply_to_message_id=message.message_id, thread_id=get_thread_id(message)),
        )

    async def _on_agent_commands(self, message: Message) -> None:
        """Handle /agent_commands: explain multi-agent system + list commands."""
        chat_id = message.chat.id
        thread_id = get_thread_id(message)

        lines = [
            "The multi-agent system lets you run additional bots as "
            "sub-agents — each with its own Telegram token, workspace, "
            "and user list. All agents share a single process and can "
            "communicate via the inter-agent bus.",
            "",
            "**Commands**",
            "`/agents` — list all agents and their status",
            "`/agent_start <name>` — start a sub-agent",
            "`/agent_stop <name>` — stop a sub-agent",
            "`/agent_restart <name>` — restart a sub-agent",
            "",
            "**Setup**",
            "Ask your agent to create a new sub-agent or edit "
            "`agents.json` in your ductor home directory.",
        ]
        text = fmt("**Multi-Agent System**", SEP, "\n".join(lines))
        await send_rich(
            self._bot,
            chat_id,
            text,
            SendRichOpts(reply_to_message_id=message.message_id, thread_id=thread_id),
        )

    async def _on_info(self, message: Message) -> None:
        """Handle /info: show project links and version."""
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        version = get_current_version()
        text = fmt(
            "**ductor.dev**",
            f"Version: `{version}`",
            SEP,
            "AI coding agents (Claude, Codex, Gemini) on Telegram.\n"
            "Named sessions, persistent memory, cron jobs, webhooks, live streaming.",
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

    async def _on_showfiles(self, message: Message) -> None:
        """Handle /showfiles: interactive file browser for ~/.ductor."""
        text, keyboard = await file_browser_start(self._orch.paths)
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

    # -- Abort, commands, sessions ---------------------------------------------

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

    async def _on_quick_command(self, chat_id: int, message: Message) -> bool:
        """Handle a read-only command without the sequential lock.

        ``/model`` is special: when the chat is busy it returns an immediate
        "agent is working" message; otherwise it acquires the lock for an
        atomic model switch.

        ``/showfiles`` is handled directly (no orchestrator needed).
        """
        if self._orchestrator is None:
            return False

        text_lower = (message.text or "").strip().lower()
        if text_lower.startswith("/showfiles"):
            await self._on_showfiles(message)
            return True

        if text_lower.startswith("/sessions"):
            await handle_command(self._orchestrator, self._bot, message)
            return True

        if text_lower.startswith("/model"):
            if self._sequential.is_busy(chat_id) or self._orch.is_chat_busy(chat_id):
                await send_rich(
                    self._bot,
                    chat_id,
                    "**Agent is working.** Use /stop to terminate first, then switch models.",
                    SendRichOpts(
                        reply_to_message_id=message.message_id, thread_id=get_thread_id(message)
                    ),
                )
                return True
            async with self._sequential.get_lock(chat_id):
                await handle_command(self._orchestrator, self._bot, message)
            return True

        await handle_command(self._orchestrator, self._bot, message)
        return True

    async def _on_stop_all(self, message: Message) -> None:
        await handle_abort_all(
            self._orchestrator,
            self._bot,
            chat_id=message.chat.id,
            message=message,
            abort_all_callback=self._abort_all_callback,
        )

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

    def _build_session_help(self) -> str:
        """Build the /session hub: explain the system + show commands."""
        providers = self._orch._available_providers
        lines: list[str] = [
            "Background sessions run tasks in parallel without blocking "
            "the main chat. Each session gets a unique name and runs "
            "independently — you can have multiple sessions active at once.",
            "",
            "**Usage**",
        ]

        if len(providers) == 1:
            p = next(iter(providers))
            if p == "claude":
                lines.append("`/session <prompt>` — runs on Claude")
                lines.append("`/session @opus <prompt>` — specific model")
            elif p == "codex":
                lines.append("`/session <prompt>` — runs on Codex")
            else:
                lines.append("`/session <prompt>` — runs on Gemini")
                lines.append("`/session @flash <prompt>` — specific model")
        else:
            lines.append("`/session <prompt>` — default provider")
            if "claude" in providers:
                lines.append("`/session @opus <prompt>` — Claude (opus)")
            if "codex" in providers:
                lines.append("`/session @codex <prompt>` — Codex")
            if "gemini" in providers:
                lines.append("`/session @flash <prompt>` — Gemini (flash)")
            lines.append("`/session @provider model <prompt>` — explicit")

        lines += [
            "",
            "**Follow up**",
            "`@session-name <message>` — send a follow-up to a running session",
            "",
            "**Commands**",
            "`/sessions` — view and manage all background sessions",
            "`/stop` — cancel the running session",
        ]

        return fmt("**Background Sessions**", SEP, "\n".join(lines))

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
                if key in ("claude", "codex", "gemini"):
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
                provider = ns.provider if ns else (provider_override or self._orch._config.provider)
                model = ns.model if ns else ""
                provider_label = {"claude": "Claude", "codex": "Codex", "gemini": "Gemini"}.get(
                    provider, provider
                )
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

    async def _on_sessions(self, message: Message) -> None:
        """Handle /sessions: show session management UI."""
        await handle_command(self._orch, self._bot, message)

    async def _on_restart(self, message: Message) -> None:
        from ductor_bot.infra.restart import write_restart_sentinel

        chat_id = message.chat.id
        paths = self._orch.paths
        sentinel = paths.ductor_home / "restart-sentinel.json"
        await asyncio.to_thread(
            write_restart_sentinel, chat_id, "Restart completed.", sentinel_path=sentinel
        )
        text = fmt("**Restarting**", SEP, "Bot is shutting down and will be back shortly.")
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

        if await self._route_special_callback(chat_id, msg.message_id, data, thread_id=thread_id):
            return

        await self._mark_button_choice(chat_id, msg, display_label)

        async with self._sequential.get_lock(chat_id):
            if self._config.streaming.enabled:
                await self._handle_streaming(msg, chat_id, data, thread_id=thread_id)
            else:
                await self._handle_non_streaming(msg, chat_id, data, thread_id=thread_id)

    async def _route_special_callback(
        self, chat_id: int, message_id: int, data: str, *, thread_id: int | None = None
    ) -> bool:
        """Handle known callback namespaces. Returns True when handled."""
        if await self._route_prefix_callback(chat_id, message_id, data, thread_id=thread_id):
            return True

        from ductor_bot.orchestrator.model_selector import is_model_selector_callback

        if is_model_selector_callback(data):
            await self._handle_model_selector(chat_id, message_id, data)
            return True

        from ductor_bot.orchestrator.cron_selector import is_cron_selector_callback

        if is_cron_selector_callback(data):
            await self._handle_cron_selector(chat_id, message_id, data)
            return True

        if is_file_browser_callback(data):
            await self._handle_file_browser(chat_id, message_id, data, thread_id=thread_id)
            return True

        return False

    async def _route_prefix_callback(
        self, chat_id: int, message_id: int, data: str, *, thread_id: int | None = None
    ) -> bool:
        """Handle prefix-based callback namespaces. Returns True when handled."""
        if data.startswith(MQ_PREFIX):
            await self._handle_queue_cancel(chat_id, data)
            return True

        if data.startswith("upg:"):
            await self._handle_upgrade_callback(chat_id, message_id, data, thread_id=thread_id)
            return True

        from ductor_bot.orchestrator.session_selector import is_session_selector_callback

        if is_session_selector_callback(data):
            await self._handle_session_selector(chat_id, message_id, data)
            return True

        if data.startswith("ns:"):
            await self._handle_ns_callback(chat_id, data, thread_id=thread_id)
            return True

        return False

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
                text=markdown_to_telegram_html(text),
                chat_id=chat_id,
                message_id=message_id,
                reply_markup=keyboard,
                parse_mode=ParseMode.HTML,
            )

    async def _handle_cron_selector(self, chat_id: int, message_id: int, data: str) -> None:
        """Handle cron selector wizard by editing the message in-place."""
        from ductor_bot.orchestrator.cron_selector import handle_cron_callback

        async with self._sequential.get_lock(chat_id):
            text, keyboard = await handle_cron_callback(self._orch, data)
        with contextlib.suppress(TelegramBadRequest):
            await self._bot.edit_message_text(
                text=markdown_to_telegram_html(text),
                chat_id=chat_id,
                message_id=message_id,
                reply_markup=keyboard,
                parse_mode=ParseMode.HTML,
            )

    async def _handle_session_selector(self, chat_id: int, message_id: int, data: str) -> None:
        """Handle session selector wizard by editing the message in-place."""
        from ductor_bot.orchestrator.session_selector import handle_session_callback

        async with self._sequential.get_lock(chat_id):
            text, keyboard = await handle_session_callback(self._orch, chat_id, data)
        with contextlib.suppress(TelegramBadRequest):
            await self._bot.edit_message_text(
                text=markdown_to_telegram_html(text),
                chat_id=chat_id,
                message_id=message_id,
                reply_markup=keyboard,
                parse_mode=ParseMode.HTML,
            )

    async def _handle_ns_callback(
        self, chat_id: int, data: str, *, thread_id: int | None = None
    ) -> None:
        """Handle ``ns:<session_name>:<label>`` button callbacks from session results.

        Routes the button label as a foreground follow-up to the named session.
        """
        # Parse: ns:<session_name>:<label>
        rest = data[3:]  # strip "ns:"
        colon = rest.find(":")
        if colon < 0:
            return
        session_name = rest[:colon]
        label = rest[colon + 1 :]
        if not session_name or not label:
            return

        async with self._sequential.get_lock(chat_id):
            if self._config.streaming.enabled:
                await self._handle_streaming_ns(chat_id, session_name, label, thread_id=thread_id)
            else:
                await self._handle_non_streaming_ns(
                    chat_id, session_name, label, thread_id=thread_id
                )

    async def _handle_streaming_ns(
        self,
        chat_id: int,
        session_name: str,
        text: str,
        *,
        thread_id: int | None = None,
    ) -> None:
        """Stream a named session follow-up from a button click."""
        from ductor_bot.orchestrator.flows import named_session_streaming

        result = await named_session_streaming(
            self._orch,
            chat_id,
            session_name,
            text,
        )
        if result.text:
            roots = self._file_roots(self._orch.paths)
            await send_rich(
                self._bot,
                chat_id,
                result.text,
                SendRichOpts(allowed_roots=roots, thread_id=thread_id),
            )

    async def _handle_non_streaming_ns(
        self,
        chat_id: int,
        session_name: str,
        text: str,
        *,
        thread_id: int | None = None,
    ) -> None:
        """Non-streaming named session follow-up from a button click."""
        from ductor_bot.orchestrator.flows import named_session_flow

        result = await named_session_flow(self._orch, chat_id, session_name, text)
        if result.text:
            roots = self._file_roots(self._orch.paths)
            await send_rich(
                self._bot,
                chat_id,
                result.text,
                SendRichOpts(allowed_roots=roots, thread_id=thread_id),
            )

    async def _handle_file_browser(
        self, chat_id: int, message_id: int, data: str, *, thread_id: int | None = None
    ) -> None:
        """Handle file browser navigation or file request."""
        text, keyboard, prompt = await handle_file_browser_callback(self._orch.paths, data)

        if prompt:
            # File request: remove the keyboard and send prompt to orchestrator
            with contextlib.suppress(TelegramBadRequest):
                await self._bot.edit_message_reply_markup(
                    chat_id=chat_id, message_id=message_id, reply_markup=None
                )
            async with self._sequential.get_lock(chat_id):
                if self._config.streaming.enabled:
                    fake_msg = await self._bot.send_message(
                        chat_id,
                        prompt,
                        parse_mode=None,
                        message_thread_id=thread_id,
                    )
                    await self._handle_streaming(fake_msg, chat_id, prompt, thread_id=thread_id)
                else:
                    await self._handle_non_streaming(None, chat_id, prompt, thread_id=thread_id)
            return

        # Directory navigation: edit message in-place
        with contextlib.suppress(TelegramBadRequest):
            await self._bot.edit_message_text(
                text=markdown_to_telegram_html(text),
                chat_id=chat_id,
                message_id=message_id,
                reply_markup=keyboard,
                parse_mode=ParseMode.HTML,
            )

    async def _handle_queue_cancel(self, chat_id: int, data: str) -> None:
        """Handle a ``mq:<entry_id>`` callback to cancel a queued message."""
        try:
            entry_id = int(data[len(MQ_PREFIX) :])
        except (ValueError, IndexError):
            return
        await self._sequential.cancel_entry(chat_id, entry_id)

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
        thread_id = get_thread_id(message)
        logger.debug("Message text=%s", text[:80])
        await self._mark_inflight_turn(
            chat_id,
            text,
            message_id=message.message_id,
            thread_id=thread_id,
        )
        completed = False

        try:
            if self._config.streaming.enabled:
                await self._handle_streaming(message, chat_id, text, thread_id=thread_id)
            else:
                await self._handle_non_streaming(message, chat_id, text, thread_id=thread_id)
            completed = True
        except TelegramAPIError as exc:
            # Network/API blips should not terminate the whole bot process.
            logger.warning("Telegram API error while handling message chat=%s: %s", chat_id, exc)
        except Exception:
            logger.exception("Unhandled error in message handler chat=%s", chat_id)
        finally:
            if completed:
                await self._clear_inflight_turn(chat_id)

    async def _resolve_text(self, message: Message) -> str | None:
        """Extract processable text from *message* (plain text or media prompt)."""
        is_group = message.chat.type in ("group", "supergroup")

        if has_media(message):
            if is_group and not is_media_addressed(message, self._bot_id, self._bot_username):
                return None
            paths = self._orch.paths
            return await resolve_media_text(
                self._bot, message, paths.telegram_files_dir, paths.workspace
            )
        if not message.text:
            return None
        if (
            is_group
            and self._config.group_mention_only
            and not is_message_addressed(message, self._bot_id, self._bot_username)
        ):
            return None
        return strip_mention(message.text, self._bot_username)

    async def _handle_streaming(
        self, message: Message, chat_id: int, text: str, *, thread_id: int | None = None
    ) -> None:
        """Streaming flow: coalescer -> stream editor -> Telegram."""
        await run_streaming_message(
            StreamingDispatch(
                bot=self._bot,
                orchestrator=self._orch,
                message=message,
                chat_id=chat_id,
                text=text,
                streaming_cfg=self._config.streaming,
                allowed_roots=self._file_roots(self._orch.paths),
                thread_id=thread_id,
            ),
        )

    async def _handle_non_streaming(
        self,
        reply_to: Message | None,
        chat_id: int,
        text: str,
        *,
        thread_id: int | None = None,
    ) -> None:
        """Non-streaming flow: one-shot orchestrator call -> Telegram delivery."""
        await run_non_streaming_message(
            NonStreamingDispatch(
                bot=self._bot,
                orchestrator=self._orch,
                chat_id=chat_id,
                text=text,
                allowed_roots=self._file_roots(self._orch.paths),
                reply_to=reply_to,
                thread_id=thread_id,
            ),
        )

    # -- Background handlers ---------------------------------------------------

    async def _on_session_result(self, result: BackgroundResult) -> None:
        """Send background task result as a NEW message (triggers notification)."""
        elapsed = f"{result.elapsed_seconds:.0f}s"

        if result.session_name:
            # Update named session registry
            self._orch._named_sessions.update_after_response(
                result.chat_id, result.session_name, result.session_id
            )
            await self._deliver_session_result(result, elapsed)
        else:
            await self._deliver_stateless_result(result, elapsed)

    async def _deliver_session_result(self, result: BackgroundResult, elapsed: str) -> None:
        """Deliver a named-session background result with session tag."""
        name = result.session_name
        if result.status == "aborted":
            text = fmt(f"**[{name}] Cancelled**", SEP, f"_{result.prompt_preview}_")
        elif result.status.startswith("error:"):
            text = fmt(
                f"**[{name}] Failed** ({elapsed})",
                SEP,
                result.result_text[:2000] if result.result_text else "_No output._",
            )
        else:
            text = fmt(
                f"**[{name}] Complete** ({elapsed})",
                SEP,
                result.result_text or "_No output._",
            )

        cleaned, markup = extract_buttons_for_session(text, name)
        roots = self._file_roots(self._orch.paths)
        await send_rich(
            self._bot,
            result.chat_id,
            cleaned,
            SendRichOpts(
                reply_to_message_id=result.message_id,
                reply_markup=markup,
                allowed_roots=roots,
                thread_id=result.thread_id,
            ),
        )

    async def _deliver_stateless_result(self, result: BackgroundResult, elapsed: str) -> None:
        """Deliver a legacy stateless background result."""
        if result.status == "aborted":
            text = fmt(
                "**Background Task Cancelled**",
                SEP,
                f"Task `{result.task_id}` was cancelled.\nPrompt: _{result.prompt_preview}_",
            )
        elif result.status.startswith("error:"):
            text = fmt(
                f"**Background Task Failed** ({elapsed})",
                SEP,
                f"Task `{result.task_id}` failed ({result.status}).\n"
                f"Prompt: _{result.prompt_preview}_\n\n"
                + (result.result_text[:2000] if result.result_text else "_No output._"),
            )
        else:
            text = fmt(
                f"**Background Task Complete** ({elapsed})",
                SEP,
                result.result_text or "_No output._",
            )

        roots = self._file_roots(self._orch.paths)
        await send_rich(
            self._bot,
            result.chat_id,
            text,
            SendRichOpts(
                reply_to_message_id=result.message_id,
                allowed_roots=roots,
                thread_id=result.thread_id,
            ),
        )

    async def _on_cron_result(self, title: str, result: str, status: str) -> None:
        """Send cron job result to all allowed users."""
        clean_result = _sanitize_cron_result_text(result)
        if result and not clean_result and status == "success":
            logger.debug(
                "Cron result only had transport confirmations; skipping broadcast task=%s", title
            )
            return
        text = (
            f"**TASK: {title}**\n\n{clean_result}"
            if clean_result
            else f"**TASK: {title}**\n\n_{status}_"
        )
        await self._broadcast(text, SendRichOpts(allowed_roots=self._file_roots(self._orch.paths)))

    async def _on_heartbeat_result(self, chat_id: int, text: str) -> None:
        """Send heartbeat alert to the user."""
        logger.debug("Heartbeat delivery chars=%d", len(text))
        await send_rich(
            self._bot, chat_id, text, SendRichOpts(allowed_roots=self._file_roots(self._orch.paths))
        )
        logger.info("Heartbeat delivered")

    async def on_async_interagent_result(
        self,
        result: AsyncInterAgentResult,
    ) -> None:
        """Handle async inter-agent result: inject into active main session.

        On error: sends the error notification to the primary user.
        On success: acquires the chat lock, resumes the current active session
        with a self-contained prompt (original task + result), and sends the
        orchestrator's response to Telegram.
        """
        chat_id = self._config.allowed_user_ids[0] if self._config.allowed_user_ids else 0
        if not chat_id:
            logger.warning("No chat_id available for async interagent result delivery")
            return

        set_log_context(operation="ia-async", chat_id=chat_id)
        roots = self._file_roots(self._orch.paths)

        logger.debug(
            "Async inter-agent result received: task=%s from=%s success=%s len=%d",
            result.task_id,
            result.recipient,
            result.success,
            len(result.result_text),
        )

        session_info = f"\nSession: `{result.session_name}`" if result.session_name else ""

        if not result.success:
            error_text = (
                f"**Inter-Agent Request Failed**\n\n"
                f"Agent: `{result.recipient}`{session_info}\n"
                f"Error: {result.error}\n"
                f"Request: _{result.message_preview}_"
            )
            await send_rich(self._bot, chat_id, error_text, SendRichOpts(allowed_roots=roots))
            return

        # Notify user about provider switch before processing the result
        if result.provider_switch_notice:
            await send_rich(
                self._bot,
                chat_id,
                f"**Provider Switch Detected**\n\n{result.provider_switch_notice}",
                SendRichOpts(allowed_roots=roots),
            )

        # Acquire the chat lock so we safely resume the current active session
        # (no concurrent CLI access). Queues behind any active user conversation.
        lock = self._sequential.get_lock(chat_id)
        logger.debug("Async inter-agent result: waiting for chat lock (chat_id=%d)", chat_id)
        async with lock, _TypingContext(self._bot, chat_id):
            logger.debug("Async inter-agent result: lock acquired, injecting into main session")
            response_text = await self._orch.handle_async_interagent_result(
                result,
                chat_id=chat_id,
            )

        if response_text:
            await send_rich(self._bot, chat_id, response_text, SendRichOpts(allowed_roots=roots))

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
        await send_rich(self._bot, chat_id, result.text, SendRichOpts(allowed_roots=roots))
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
        await self._broadcast(text, SendRichOpts(allowed_roots=self._file_roots(self._orch.paths)))

    # -- Update notifications --------------------------------------------------

    async def _on_update_available(self, info: VersionInfo) -> None:
        """Notify all users about a new version via Telegram."""
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=f"Changelog v{info.latest}",
                        callback_data=f"upg:cl:{info.latest}",
                    ),
                ],
                [
                    InlineKeyboardButton(
                        text="Upgrade now",
                        callback_data=f"upg:yes:{info.latest}",
                    ),
                    InlineKeyboardButton(text="Later", callback_data="upg:no"),
                ],
            ],
        )
        text = fmt(
            "**Update Available**",
            SEP,
            f"Installed: `{info.current}`\nNew:       `{info.latest}`",
        )
        await self._broadcast(text, SendRichOpts(reply_markup=keyboard))

    async def _handle_upgrade_callback(
        self, chat_id: int, message_id: int, data: str, *, thread_id: int | None = None
    ) -> None:
        """Handle ``upg:yes:<version>``, ``upg:no``, and ``upg:cl:<version>`` callbacks."""
        if data.startswith("upg:cl:"):
            await self._handle_changelog_callback(chat_id, message_id, data, thread_id=thread_id)
            return

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
        current_version = get_current_version()

        if self._upgrade_lock.locked():
            await self._bot.send_message(
                chat_id,
                "Upgrade already in progress. Please wait.",
                parse_mode=None,
                message_thread_id=thread_id,
            )
            return

        async with self._upgrade_lock:
            await self._bot.send_message(
                chat_id,
                f"Upgrading to {target_version}...",
                parse_mode=None,
                message_thread_id=thread_id,
            )

            changed, installed_version, output = await perform_upgrade_pipeline(
                current_version=current_version,
                target_version=target_version,
            )

            if not changed:
                logger.warning(
                    "Upgrade did not change version after retry: current=%s installed=%s target=%s",
                    current_version,
                    installed_version,
                    target_version,
                )
                tail = output[-300:] if output else ""
                details = f"\n\n{tail}" if tail else ""
                await self._bot.send_message(
                    chat_id,
                    (
                        f"Upgrade could not verify a new installed version "
                        f"(still {installed_version}) after automatic retry.{details}"
                    ),
                    parse_mode=None,
                    message_thread_id=thread_id,
                )
                return

            # Write sentinel for post-restart message (use actual installed version)
            await asyncio.to_thread(
                write_upgrade_sentinel,
                self._orch.paths.ductor_home,
                chat_id=chat_id,
                old_version=current_version,
                new_version=installed_version,
            )

            await self._bot.send_message(
                chat_id,
                "Bot is restarting...",
                parse_mode=None,
                message_thread_id=thread_id,
            )
            self._exit_code = EXIT_RESTART
            await self._dp.stop_polling()

    async def _handle_changelog_callback(
        self, chat_id: int, message_id: int, data: str, *, thread_id: int | None = None
    ) -> None:
        """Fetch and display changelog for ``upg:cl:<version>``."""
        from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

        from ductor_bot.infra.version import _parse_version, fetch_changelog

        version = data.split(":", 2)[2] if data.count(":") >= 2 else ""
        if not version:
            return

        # Only show upgrade buttons when the changelog version is newer than installed
        current = get_current_version()
        is_upgrade = _parse_version(version) > _parse_version(current)

        if is_upgrade:
            upgrade_keyboard: InlineKeyboardMarkup | None = InlineKeyboardMarkup(
                inline_keyboard=[
                    [
                        InlineKeyboardButton(
                            text="Upgrade now",
                            callback_data=f"upg:yes:{version}",
                        ),
                        InlineKeyboardButton(text="Later", callback_data="upg:no"),
                    ],
                ],
            )
        else:
            upgrade_keyboard = None

        # Update the original message: keep upgrade buttons if applicable, else remove all
        with contextlib.suppress(TelegramBadRequest):
            await self._bot.edit_message_reply_markup(
                chat_id=chat_id, message_id=message_id, reply_markup=upgrade_keyboard
            )

        body = await fetch_changelog(version)
        if not body:
            await self._bot.send_message(
                chat_id,
                f"No changelog found for v{version}.",
                parse_mode=None,
                message_thread_id=thread_id,
            )
            return

        roots = self._file_roots(self._orch.paths)
        await send_rich(
            self._bot,
            chat_id,
            f"**Changelog v{version}**\n\n{body}",
            SendRichOpts(
                allowed_roots=roots,
                reply_markup=upgrade_keyboard,
                thread_id=thread_id,
            ),
        )

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
