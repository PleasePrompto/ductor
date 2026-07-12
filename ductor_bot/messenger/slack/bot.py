"""Slack transport bot using Socket Mode."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ductor_bot.bus.bus import MessageBus
from ductor_bot.bus.lock_pool import LockPool
from ductor_bot.commands import BOT_COMMANDS, MULTIAGENT_SUB_COMMANDS
from ductor_bot.config import AgentConfig
from ductor_bot.files.allowed_roots import resolve_allowed_roots
from ductor_bot.i18n import t
from ductor_bot.infra.version import get_current_version
from ductor_bot.messenger.commands import classify_command
from ductor_bot.messenger.notifications import NotificationService
from ductor_bot.messenger.slack.id_map import SlackIdMap
from ductor_bot.messenger.slack.sender import SlackSendOpts, send_rich
from ductor_bot.session.key import SessionKey
from ductor_bot.text.response_format import SEP, fmt

if TYPE_CHECKING:
    from ductor_bot.infra.updater import UpdateObserver
    from ductor_bot.multiagent.bus import AsyncInterAgentResult
    from ductor_bot.orchestrator.core import Orchestrator
    from ductor_bot.tasks.models import TaskResult
    from ductor_bot.workspace.paths import DuctorPaths

if TYPE_CHECKING:
    _SlackSocketModeHandler: Any = Any
    _SlackAsyncApp: Any = Any
    _SLACK_AVAILABLE = True
else:
    try:
        from slack_bolt.adapter.socket_mode.async_handler import (
            AsyncSocketModeHandler as _SlackSocketModeHandler,
        )
        from slack_bolt.async_app import AsyncApp as _SlackAsyncApp

        _SLACK_AVAILABLE = True
    except ImportError:  # pragma: no cover - import fallback
        _SLACK_AVAILABLE = False
        _SlackSocketModeHandler = object
        _SlackAsyncApp = object

logger = logging.getLogger(__name__)

_DEFAULT_MENTIONED_THREAD_TTL_SECONDS = 3600.0
_DEFAULT_MENTIONED_THREAD_MAX_SIZE = 200
_DEFAULT_THREAD_CONTEXT_CACHE_MAX_SIZE = 200
_DEFAULT_RECENT_EVENT_TTL_SECONDS = 120.0
_DEFAULT_RECENT_EVENT_MAX_SIZE = 500
_MESSAGE_COMMANDS_WITH_ARGS = frozenset(
    {"agent_restart", "agent_start", "agent_stop", "model", "session", "showfiles"}
)


@dataclass(slots=True)
class _ThreadContextCache:
    """Cache entry for fetched Slack thread context."""

    content: str
    fetched_at: float = field(default_factory=time.monotonic)
    message_count: int = 0


def _restart_marker_path(ductor_home: str) -> Path:
    """Return the restart marker path."""
    return Path(ductor_home).expanduser() / "restart-requested"


def _slack_ts_is_at_or_after(candidate_ts: str, current_ts: str) -> bool:
    """Return whether *candidate_ts* is the current Slack message or later."""
    try:
        return Decimal(candidate_ts) >= Decimal(current_ts)
    except (InvalidOperation, ValueError):
        return candidate_ts >= current_ts


class SlackNotificationService:
    """Notification service implementation for Slack."""

    def __init__(self, bot: SlackBot) -> None:
        self._bot = bot

    async def notify(self, chat_id: int, text: str) -> None:
        channel_id = self._bot.id_map.int_to_channel(chat_id)
        if channel_id:
            await self._bot._send_rich(channel_id, text)
            return
        logger.warning("notify: cannot resolve chat_id=%d to Slack channel, falling back", chat_id)
        await self.notify_all(text)

    async def notify_all(self, text: str) -> None:
        await self._bot.broadcast(text)


class SlackBot:
    """Slack bot implementing ``BotProtocol``."""

    def __init__(
        self,
        config: AgentConfig,
        *,
        agent_name: str = "main",
        bus: MessageBus | None = None,
        lock_pool: LockPool | None = None,
    ) -> None:
        if not _SLACK_AVAILABLE:
            raise ImportError(
                "slack-bolt is required for Slack transport. "
                "Install with: pip install 'ductor[slack]'"
            ) from None

        self._config = config
        self._agent_name = agent_name
        self._store_path = Path(config.ductor_home).expanduser() / "slack_store"
        self._store_path.mkdir(parents=True, exist_ok=True)

        self._app: Any = _SlackAsyncApp(token=config.slack.bot_token)
        self._socket_handler: Any | None = None
        self._socket_task: asyncio.Task[None] | None = None
        self._lock_pool = lock_pool or LockPool()
        self._bus = bus or MessageBus(lock_pool=self._lock_pool)
        self._id_map = SlackIdMap(self._store_path)

        from ductor_bot.messenger.slack.transport import SlackTransport

        self._bus.register_transport(SlackTransport(self))
        self._orchestrator: Orchestrator | None = None
        self._startup_hooks: list[Callable[[], Awaitable[None]]] = []
        self._notification_service: NotificationService = SlackNotificationService(self)
        self._abort_all_callback: Callable[[], Awaitable[int]] | None = None
        self._exit_code = 0
        self._update_observer: UpdateObserver | None = None
        self._restart_watcher: asyncio.Task[None] | None = None
        self._bot_user_id = ""
        self._bot_name = "slack-bot"
        self._team_id = ""
        self._last_active_channel: str | None = None
        self._mentioned_threads: dict[tuple[str, str], float] = {}
        self._recent_events: dict[tuple[str, str], float] = {}
        self._user_name_cache: dict[str, str] = {}
        self._thread_context_cache: dict[str, _ThreadContextCache] = {}
        self._MENTIONED_THREAD_TTL = _DEFAULT_MENTIONED_THREAD_TTL_SECONDS
        self._MENTIONED_THREAD_MAX_SIZE = _DEFAULT_MENTIONED_THREAD_MAX_SIZE
        self._RECENT_EVENT_TTL = _DEFAULT_RECENT_EVENT_TTL_SECONDS
        self._RECENT_EVENT_MAX_SIZE = _DEFAULT_RECENT_EVENT_MAX_SIZE
        self._THREAD_CACHE_TTL = 60.0
        self._THREAD_CONTEXT_CACHE_MAX_SIZE = _DEFAULT_THREAD_CONTEXT_CACHE_MAX_SIZE

        self._register_handlers()

    @property
    def orchestrator(self) -> Orchestrator | None:
        return self._orchestrator

    @property
    def config(self) -> AgentConfig:
        return self._config

    @property
    def notification_service(self) -> NotificationService:
        return self._notification_service

    @property
    def id_map(self) -> SlackIdMap:
        return self._id_map

    @property
    def client(self) -> Any:
        return self._app.client

    @property
    def bot_name(self) -> str:
        return self._bot_name

    def register_startup_hook(self, hook: Callable[[], Awaitable[None]]) -> None:
        self._startup_hooks.append(hook)

    def set_abort_all_callback(self, callback: Callable[[], Awaitable[int]]) -> None:
        self._abort_all_callback = callback

    def file_roots(self, paths: DuctorPaths) -> list[Path] | None:
        return resolve_allowed_roots(self._config.file_access, paths.workspace)

    async def run(self) -> int:
        auth = await self.client.auth_test()
        self._bot_user_id = str(auth.get("user_id", ""))
        self._bot_name = str(auth.get("user", "slack-bot"))
        self._team_id = str(auth.get("team_id", ""))

        from ductor_bot.messenger.slack.startup import run_slack_startup

        await run_slack_startup(self)

        self._restart_watcher = asyncio.create_task(self._watch_restart_marker())
        self._socket_handler = _SlackSocketModeHandler(self._app, self._config.slack.app_token)
        handler: Any = self._socket_handler
        self._socket_task = asyncio.create_task(handler.start_async())
        try:
            await self._socket_task
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("Slack Socket Mode exited with error, requesting restart")
            from ductor_bot.infra.restart import EXIT_RESTART

            self._exit_code = EXIT_RESTART
        return self._exit_code

    async def shutdown(self) -> None:
        if self._restart_watcher:
            self._restart_watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._restart_watcher

        if self._socket_handler:
            handler: Any = self._socket_handler
            with contextlib.suppress(Exception):
                await handler.close_async()
        if self._socket_task and not self._socket_task.done():
            self._socket_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._socket_task

        if self._update_observer:
            await self._update_observer.stop()
        if self._orchestrator:
            await self._orchestrator.shutdown()

        logger.info("SlackBot shut down")

    def _register_handlers(self) -> None:
        self._app.event("message")(self._handle_message_event)
        self._app.event("app_mention")(self._handle_mention_event)

    async def _handle_message_event(self, event: dict[str, Any]) -> None:
        await self._on_message(event)

    async def _handle_mention_event(self, event: dict[str, Any]) -> None:
        await self._on_message(event)

    async def _on_message(self, event: dict[str, Any]) -> None:
        subtype = str(event.get("subtype", "") or "")
        user_id = str(event.get("user", "") or "")
        channel_id = str(event.get("channel", "") or "")
        text = str(event.get("text", "") or "").strip()
        if (
            subtype in {"bot_message", "message_changed", "message_deleted"}
            or event.get("bot_id")
            or not user_id
            or not channel_id
            or not text
        ):
            return

        is_dm = str(event.get("channel_type", "") or "") == "im"
        if not self._is_authorized(channel_id, user_id, is_dm=is_dm):
            return

        thread_ts = str(event.get("thread_ts", "") or "")
        ts = str(event.get("ts", "") or "")
        if self._should_skip_recent_event(channel_id, ts):
            logger.debug("Skipping duplicate Slack event for %s/%s", channel_id, ts)
            return
        reply_thread_ts = thread_ts or (ts if not is_dm else "")
        is_thread_reply = bool(reply_thread_ts and reply_thread_ts != ts)
        has_thread_session = bool(
            reply_thread_ts
            and await self._has_active_session_for_thread(channel_id, reply_thread_ts)
        )
        if not is_dm and self._config.group_mention_only:
            is_mentioned = bool(self._bot_user_id and f"<@{self._bot_user_id}>" in text)
            in_mentioned_thread = bool(
                is_thread_reply
                and reply_thread_ts
                and self._has_recent_mentioned_thread(channel_id, reply_thread_ts)
            )
            if not is_mentioned and not in_mentioned_thread and not has_thread_session:
                return
            text = self._strip_mention(text)
            if is_mentioned and reply_thread_ts:
                self._mark_mentioned_thread(channel_id, reply_thread_ts)

        chat_id = self._id_map.channel_to_int(channel_id)
        topic_id = (
            self._id_map.thread_to_int(channel_id, reply_thread_ts) if reply_thread_ts else None
        )
        key = SessionKey.for_transport("sl", chat_id, topic_id)
        self._last_active_channel = channel_id

        command_text = self._normalize_command_text(text)
        if command_text is not None:
            await self._handle_command(
                command_text,
                channel_id,
                key,
                reply_thread_ts or None,
                stream_thread_ts=reply_thread_ts or ts,
                recipient_user_id=user_id,
            )
            return

        if is_thread_reply and reply_thread_ts and not has_thread_session:
            thread_context = await self._fetch_thread_context(
                channel_id=channel_id,
                thread_ts=reply_thread_ts,
                current_ts=ts,
            )
            if thread_context:
                text = thread_context + text

        await self._dispatch_with_lock(
            key,
            text,
            channel_id,
            reply_thread_ts or None,
            stream_thread_ts=reply_thread_ts or ts,
            recipient_user_id=user_id,
        )

    async def _handle_command(  # noqa: PLR0913
        self,
        text: str,
        channel_id: str,
        key: SessionKey,
        thread_ts: str | None,
        *,
        stream_thread_ts: str | None = None,
        recipient_user_id: str | None = None,
    ) -> None:
        cmd = text.split(maxsplit=1)[0].lower().lstrip("/")
        handler = self._COMMAND_DISPATCH.get(cmd)
        if handler is not None:
            if cmd in self._IMMEDIATE_COMMANDS:
                await handler(self, text=text, channel_id=channel_id, key=key, thread_ts=thread_ts)
            else:
                await self._run_handler_with_lock(
                    handler,
                    text=text,
                    channel_id=channel_id,
                    key=key,
                    thread_ts=thread_ts,
                )
        elif classify_command(cmd) in ("orchestrator", "multiagent"):
            await self._cmd_orchestrator(
                text=text, channel_id=channel_id, key=key, thread_ts=thread_ts
            )
        else:
            await self._dispatch_with_lock(
                key,
                text,
                channel_id,
                thread_ts,
                stream_thread_ts=stream_thread_ts,
                recipient_user_id=recipient_user_id,
            )

    async def _dispatch_with_lock(  # noqa: PLR0913
        self,
        key: SessionKey,
        text: str,
        channel_id: str,
        thread_ts: str | None,
        *,
        stream_thread_ts: str | None = None,
        recipient_user_id: str | None = None,
    ) -> None:
        lock = self._lock_pool.get(key.lock_key)
        async with lock:
            await self._dispatch_message(
                key,
                text,
                channel_id,
                thread_ts,
                stream_thread_ts=stream_thread_ts,
                recipient_user_id=recipient_user_id,
            )

    async def _run_handler_with_lock(
        self,
        handler: Callable[..., Awaitable[None]],
        **kwargs: object,
    ) -> None:
        key = kwargs["key"]
        assert isinstance(key, SessionKey)
        lock = self._lock_pool.get(key.lock_key)
        async with lock:
            await handler(self, **kwargs)

    async def _dispatch_message(  # noqa: PLR0913
        self,
        key: SessionKey,
        text: str,
        channel_id: str,
        thread_ts: str | None,
        *,
        stream_thread_ts: str | None = None,
        recipient_user_id: str | None = None,
    ) -> None:
        if self._config.streaming.enabled:
            await self._run_streaming(
                key,
                text,
                channel_id,
                stream_thread_ts or thread_ts,
                recipient_user_id=recipient_user_id,
            )
            return
        await self._run_non_streaming(key, text, channel_id, thread_ts)

    async def _run_streaming(
        self,
        key: SessionKey,
        text: str,
        channel_id: str,
        thread_ts: str | None,
        *,
        recipient_user_id: str | None = None,
    ) -> None:
        orch = self._orchestrator
        if orch is None:
            return
        if thread_ts is None:
            await self._run_non_streaming(key, text, channel_id, thread_ts)
            return

        from ductor_bot.messenger.slack.streaming import SlackStreamEditor

        editor = SlackStreamEditor(
            self.client,
            channel_id,
            thread_ts=thread_ts,
            recipient_user_id=recipient_user_id if not channel_id.startswith("D") else None,
            recipient_team_id=self._team_id or None if not channel_id.startswith("D") else None,
            edit_interval_seconds=self._config.streaming.edit_interval_seconds,
        )
        result = await orch.handle_message_streaming(
            key,
            text,
            on_text_delta=editor.on_delta,
            on_thinking_delta=editor.on_thinking,
            on_tool_activity=editor.on_tool,
            on_system_status=editor.on_system,
        )
        self._maybe_append_footer(result)
        await editor.finalize(result.text)

    async def _run_non_streaming(
        self,
        key: SessionKey,
        text: str,
        channel_id: str,
        thread_ts: str | None,
    ) -> None:
        orch = self._orchestrator
        if orch is None:
            return
        result = await orch.handle_message(key, text)
        self._maybe_append_footer(result)
        if result.text:
            await self._send_rich(channel_id, result.text, thread_ts=thread_ts)

    async def _cmd_stop(
        self,
        *,
        text: str,
        channel_id: str,
        key: SessionKey,
        thread_ts: str | None,
    ) -> None:
        del text
        orch = self._orchestrator
        if orch:
            killed = await orch.abort(key.chat_id)
            msg = t("abort_all.done", count=killed) if killed else t("abort_all.nothing")
        else:
            msg = t("abort_all.nothing")
        await self._send_rich(channel_id, msg, thread_ts=thread_ts)

    async def _cmd_interrupt(
        self,
        *,
        text: str,
        channel_id: str,
        key: SessionKey,
        thread_ts: str | None,
    ) -> None:
        del text
        orch = self._orchestrator
        if orch:
            interrupted = orch.interrupt(key.chat_id)
            msg = t("interrupt.done", count=interrupted) if interrupted else t("interrupt.nothing")
            await self._send_rich(channel_id, msg, thread_ts=thread_ts)

    async def _cmd_stop_all(
        self,
        *,
        text: str,
        channel_id: str,
        key: SessionKey,
        thread_ts: str | None,
    ) -> None:
        del text, key
        orch = self._orchestrator
        killed = await orch.abort_all() if orch else 0
        if self._abort_all_callback:
            killed += await self._abort_all_callback()
        msg = t("abort_all.done", count=killed) if killed else t("abort_all.nothing")
        await self._send_rich(channel_id, msg, thread_ts=thread_ts)

    async def _cmd_restart(
        self,
        *,
        text: str,
        channel_id: str,
        key: SessionKey,
        thread_ts: str | None,
    ) -> None:
        del text, key
        from ductor_bot.infra.restart import EXIT_RESTART, write_restart_marker

        marker = _restart_marker_path(self._config.ductor_home)
        write_restart_marker(marker_path=marker)
        await self._send_rich(
            channel_id,
            fmt(t("startup.restart_header"), SEP, t("startup.restart_body")),
            thread_ts=thread_ts,
        )
        self._exit_code = EXIT_RESTART
        if self._socket_task and not self._socket_task.done():
            self._socket_task.cancel()

    async def _cmd_new(
        self,
        *,
        text: str,
        channel_id: str,
        key: SessionKey,
        thread_ts: str | None,
    ) -> None:
        del text
        orch = self._orchestrator
        if orch:
            result = await orch.handle_message(key, "/new")
            if result and result.text:
                await self._send_rich(channel_id, result.text, thread_ts=thread_ts)

    async def _cmd_help(
        self,
        *,
        text: str,
        channel_id: str,
        key: SessionKey,
        thread_ts: str | None,
    ) -> None:
        del text, key
        await self._send_rich(channel_id, self._build_help_text(), thread_ts=thread_ts)

    async def _cmd_info(
        self,
        *,
        text: str,
        channel_id: str,
        key: SessionKey,
        thread_ts: str | None,
    ) -> None:
        del text, key
        text_out = fmt(
            t("info.header"),
            t("info.version", version=get_current_version()),
            SEP,
            t("info.slack_description"),
        )
        await self._send_rich(channel_id, text_out, thread_ts=thread_ts)

    async def _cmd_agent_commands(
        self,
        *,
        text: str,
        channel_id: str,
        key: SessionKey,
        thread_ts: str | None,
    ) -> None:
        del text, key
        lines = [
            "Slack sub-agents use the same multi-agent runtime.",
            "",
            "`/agents` — list all agents and their status",
            "`/agent_start <name>` — start a sub-agent",
            "`/agent_stop <name>` — stop a sub-agent",
            "`/agent_restart <name>` — restart a sub-agent",
        ]
        await self._send_rich(
            channel_id,
            fmt(t("agents.system_header"), SEP, "\n".join(lines)),
            thread_ts=thread_ts,
        )

    async def _cmd_showfiles(
        self,
        *,
        text: str,
        channel_id: str,
        key: SessionKey,
        thread_ts: str | None,
    ) -> None:
        del key
        orch = self._orchestrator
        if not orch:
            return
        from ductor_bot.messenger.matrix.file_browser import format_file_listing

        parts = text.split(None, 1)
        subdir = parts[1].strip() if len(parts) > 1 else ""
        listing = await asyncio.to_thread(format_file_listing, orch.paths, subdir)
        await self._send_rich(channel_id, listing, thread_ts=thread_ts)

    async def _cmd_session(
        self,
        *,
        text: str,
        channel_id: str,
        key: SessionKey,
        thread_ts: str | None,
    ) -> None:
        parts = text.split(None, 1)
        if len(parts) < 2 or not parts[1].strip():
            await self._send_rich(
                channel_id,
                fmt(
                    t("session_help.header"),
                    SEP,
                    "`/session <prompt>` — start a background session\n"
                    "`/sessions` — list running sessions\n"
                    "`/stop` — stop the active run",
                ),
                thread_ts=thread_ts,
            )
            return
        await self._dispatch_message(key, text, channel_id, thread_ts)

    async def _cmd_orchestrator(
        self,
        *,
        text: str,
        channel_id: str,
        key: SessionKey,
        thread_ts: str | None,
    ) -> None:
        orch = self._orchestrator
        if not orch:
            return
        result = await orch.handle_message(key, text)
        if result and result.text:
            await self._send_rich(channel_id, result.text, thread_ts=thread_ts)

    _COMMAND_DISPATCH: dict[str, Callable[..., Awaitable[None]]] = {
        "stop": _cmd_stop,
        "stop_all": _cmd_stop_all,
        "interrupt": _cmd_interrupt,
        "restart": _cmd_restart,
        "new": _cmd_new,
        "help": _cmd_help,
        "start": _cmd_help,
        "info": _cmd_info,
        "agent_commands": _cmd_agent_commands,
        "showfiles": _cmd_showfiles,
        "session": _cmd_session,
    }

    _IMMEDIATE_COMMANDS: frozenset[str] = frozenset(
        {
            "stop",
            "stop_all",
            "interrupt",
            "restart",
            "help",
            "start",
            "info",
            "agent_commands",
            "showfiles",
        }
    )

    def _build_help_text(self) -> str:
        cmd_desc = {**dict(BOT_COMMANDS), **dict(MULTIAGENT_SUB_COMMANDS)}

        def _line(command: str) -> str:
            description = cmd_desc.get(command, "")
            return f"`/{command}` — {description}" if description else f"`/{command}`"

        return fmt(
            t("help.header"),
            SEP,
            f"**{t('help.cat_daily')}**\n{_line('new')}\n{_line('stop')}\n{_line('stop_all')}\n"
            f"{_line('model')}\n{_line('status')}\n{_line('memory')}",
            f"**{t('help.cat_automation')}**\n{_line('session')}\n{_line('tasks')}\n{_line('cron')}",
            f"**{t('help.cat_multiagent')}**\n{_line('agent_commands')}\n{_line('agents')}\n"
            f"{_line('agent_start')}\n{_line('agent_stop')}\n{_line('agent_restart')}",
            f"**{t('help.cat_browse')}**\n{_line('showfiles')}\n{_line('info')}\n{_line('help')}",
            f"**{t('help.cat_maintenance')}**\n{_line('diagnose')}\n{_line('upgrade')}\n{_line('restart')}",
            SEP,
            t("help.slack_footer"),
        )

    def _is_authorized(self, channel_id: str, user_id: str, *, is_dm: bool) -> bool:
        slack = self._config.slack
        channel_ok = is_dm or not slack.allowed_channels or channel_id in slack.allowed_channels
        if self._config.group_mention_only and not is_dm:
            return channel_ok
        user_ok = not slack.allowed_users or user_id in slack.allowed_users
        return channel_ok and user_ok

    def _normalize_command_text(self, text: str) -> str | None:
        stripped = text.strip()
        if not stripped:
            return None
        parts = stripped.split(None, 1)
        raw_cmd = parts[0]
        cmd = raw_cmd.lower().lstrip("/")
        if classify_command(cmd) == "unknown":
            return None
        has_args = len(parts) > 1 and bool(parts[1].strip())
        if not raw_cmd.startswith("/") and has_args and cmd not in _MESSAGE_COMMANDS_WITH_ARGS:
            return None
        suffix = f" {parts[1].strip()}" if has_args else ""
        return f"/{cmd}{suffix}"

    def _prune_mentioned_threads(self, now: float) -> None:
        if self._MENTIONED_THREAD_TTL > 0:
            cutoff = now - self._MENTIONED_THREAD_TTL
            expired = [key for key, seen_at in self._mentioned_threads.items() if seen_at < cutoff]
            for key in expired:
                del self._mentioned_threads[key]
        max_size = max(1, self._MENTIONED_THREAD_MAX_SIZE)
        while len(self._mentioned_threads) > max_size:
            oldest = next(iter(self._mentioned_threads))
            del self._mentioned_threads[oldest]

    def _prune_recent_events(self, now: float) -> None:
        if self._RECENT_EVENT_TTL > 0:
            cutoff = now - self._RECENT_EVENT_TTL
            expired = [key for key, seen_at in self._recent_events.items() if seen_at < cutoff]
            for key in expired:
                del self._recent_events[key]
        max_size = max(1, self._RECENT_EVENT_MAX_SIZE)
        while len(self._recent_events) > max_size:
            oldest = next(iter(self._recent_events))
            del self._recent_events[oldest]

    def _should_skip_recent_event(self, channel_id: str, message_ts: str) -> bool:
        if not channel_id or not message_ts:
            return False
        now = time.monotonic()
        self._prune_recent_events(now)
        key = (channel_id, message_ts)
        seen_at = self._recent_events.get(key)
        if seen_at is not None:
            if self._RECENT_EVENT_TTL <= 0 or now - seen_at < self._RECENT_EVENT_TTL:
                return True
            del self._recent_events[key]
        self._recent_events[key] = now
        self._prune_recent_events(now)
        return False

    def _mark_mentioned_thread(self, channel_id: str, thread_ts: str) -> None:
        now = time.monotonic()
        key = (channel_id, thread_ts)
        self._mentioned_threads.pop(key, None)
        self._mentioned_threads[key] = now
        self._prune_mentioned_threads(now)

    def _has_recent_mentioned_thread(self, channel_id: str, thread_ts: str) -> bool:
        if not thread_ts:
            return False
        now = time.monotonic()
        self._prune_mentioned_threads(now)
        key = (channel_id, thread_ts)
        seen_at = self._mentioned_threads.get(key)
        if seen_at is None:
            return False
        if self._MENTIONED_THREAD_TTL > 0 and now - seen_at >= self._MENTIONED_THREAD_TTL:
            del self._mentioned_threads[key]
            return False
        return True

    async def _has_active_session_for_thread(self, channel_id: str, thread_ts: str) -> bool:
        """Return whether this Slack thread already has a fresh persisted session."""
        orch = self._orchestrator
        if orch is None:
            return False
        chat_id = self._id_map.channel_to_int(channel_id)
        topic_id = self._id_map.thread_to_int(channel_id, thread_ts)
        sessions = await orch._sessions.list_active_for_chat(chat_id)
        for session in sessions:
            if session.topic_id == topic_id and bool(session.session_id):
                return True
        return False

    async def _resolve_user_name(self, user_id: str, *, channel_id: str) -> str:
        """Resolve a Slack user ID to a display name with a small in-memory cache."""
        if not user_id:
            return "unknown"
        cached = self._user_name_cache.get(user_id)
        if cached:
            return cached
        try:
            response = await self.client.users_info(user=user_id)
            user = response.get("user", {}) if isinstance(response, dict) else {}
            profile = user.get("profile", {}) if isinstance(user, dict) else {}
            name = (
                profile.get("display_name")
                or profile.get("real_name")
                or user.get("real_name")
                or user.get("name")
                or user_id
            )
        except Exception:
            logger.debug(
                "Failed to resolve Slack user name in channel %s", channel_id, exc_info=True
            )
            name = user_id
        resolved = str(name).strip() or user_id
        self._user_name_cache[user_id] = resolved
        return resolved

    async def _fetch_thread_context(
        self,
        *,
        channel_id: str,
        thread_ts: str,
        current_ts: str,
        limit: int = 30,
    ) -> str:
        """Fetch earlier Slack thread messages for the first message in a fresh session."""
        cache_key = f"{channel_id}:{thread_ts}"
        now = time.monotonic()
        self._prune_thread_context_cache(now)
        cached = self._thread_context_cache.get(cache_key)
        if cached:
            return cached.content

        try:
            response = await self.client.conversations_replies(
                channel=channel_id,
                ts=thread_ts,
                limit=limit + 1,
                inclusive=True,
            )
        except Exception:
            logger.warning(
                "Failed to fetch Slack thread context for %s/%s",
                channel_id,
                thread_ts,
                exc_info=True,
            )
            return ""

        messages = response.get("messages", []) if isinstance(response, dict) else []
        if not isinstance(messages, list) or not messages:
            return ""

        context_parts = await self._build_thread_context_parts(
            messages=messages,
            channel_id=channel_id,
            thread_ts=thread_ts,
            current_ts=current_ts,
        )
        return self._cache_thread_context(
            cache_key=cache_key,
            content_parts=context_parts,
            fetched_at=now,
        )

    async def _build_thread_context_parts(
        self,
        *,
        messages: list[object],
        channel_id: str,
        thread_ts: str,
        current_ts: str,
    ) -> list[str]:
        """Build normalized thread-history lines from Slack reply payloads."""
        context_parts: list[str] = []
        for msg in messages:
            if not isinstance(msg, dict):
                continue
            msg_ts = str(msg.get("ts", "") or "")
            if not msg_ts or _slack_ts_is_at_or_after(msg_ts, current_ts):
                continue
            if msg.get("bot_id") or msg.get("subtype") == "bot_message":
                continue
            msg_text = str(msg.get("text", "") or "").strip()
            if not msg_text:
                continue
            if self._bot_user_id:
                msg_text = msg_text.replace(f"<@{self._bot_user_id}>", "").strip()
            if not msg_text:
                continue
            msg_user = str(msg.get("user", "") or "")
            user_name = await self._resolve_user_name(msg_user, channel_id=channel_id)
            prefix = "[thread parent] " if msg_ts == thread_ts else ""
            context_parts.append(f"{prefix}{user_name}: {msg_text}")
        return context_parts

    def _cache_thread_context(
        self,
        *,
        cache_key: str,
        content_parts: list[str],
        fetched_at: float,
    ) -> str:
        """Persist thread-context cache entry and return the formatted content."""
        self._prune_thread_context_cache(fetched_at)
        if not content_parts:
            self._thread_context_cache.pop(cache_key, None)
            self._thread_context_cache[cache_key] = _ThreadContextCache(
                content="",
                fetched_at=fetched_at,
            )
            self._prune_thread_context_cache(fetched_at)
            return ""

        content = (
            "[Thread context — prior messages in this thread (not yet in conversation history):]\n"
            + "\n".join(content_parts)
            + "\n[End of thread context]\n\n"
        )
        self._thread_context_cache.pop(cache_key, None)
        self._thread_context_cache[cache_key] = _ThreadContextCache(
            content=content,
            fetched_at=fetched_at,
            message_count=len(content_parts),
        )
        self._prune_thread_context_cache(fetched_at)
        return content

    def _prune_thread_context_cache(self, now: float) -> None:
        if self._THREAD_CACHE_TTL > 0:
            cutoff = now - self._THREAD_CACHE_TTL
            expired = [
                key
                for key, entry in self._thread_context_cache.items()
                if entry.fetched_at < cutoff
            ]
            for key in expired:
                del self._thread_context_cache[key]
        max_size = max(1, self._THREAD_CONTEXT_CACHE_MAX_SIZE)
        while len(self._thread_context_cache) > max_size:
            oldest = next(iter(self._thread_context_cache))
            del self._thread_context_cache[oldest]

    def _strip_mention(self, text: str) -> str:
        if not self._bot_user_id:
            return text
        return text.replace(f"<@{self._bot_user_id}>", "").strip()

    def _maybe_append_footer(self, result: object) -> None:
        from ductor_bot.orchestrator.registry import OrchestratorResult

        if not isinstance(result, OrchestratorResult):
            return
        if not self._config.scene.technical_footer or not result.model_name:
            return
        from ductor_bot.text.response_format import format_technical_footer

        footer = format_technical_footer(
            result.model_name,
            result.total_tokens,
            result.input_tokens,
            result.cost_usd,
            result.duration_ms,
        )
        result.text += footer

    async def _send_rich(
        self,
        channel_id: str,
        text: str,
        *,
        thread_ts: str | None = None,
    ) -> str | None:
        return await send_rich(self.client, channel_id, text, SlackSendOpts(thread_ts=thread_ts))

    def _broadcast_channels(self) -> list[str]:
        channels = list(self._config.slack.allowed_channels)
        if not channels and self._last_active_channel:
            channels = [self._last_active_channel]
        return channels

    async def broadcast(self, text: str) -> None:
        channels = self._broadcast_channels()
        if not channels:
            logger.warning("Slack broadcast: no channels available, message lost: %s", text[:80])
            return
        for channel_id in channels:
            await self._send_rich(channel_id, text)

    async def notify_startup(self, text: str) -> None:
        await self._notification_service.notify_all(text)

    async def notify_upgrade(self, text: str) -> None:
        await self._notification_service.notify_all(text)

    async def on_async_interagent_result(self, result: AsyncInterAgentResult) -> None:
        from ductor_bot.bus.adapters import from_interagent_result

        chat_id = self._default_chat_id()
        if not chat_id:
            logger.warning(
                "No chat_id for async interagent result (task=%s) — delivering to all channels",
                result.task_id,
            )
            text = result.result_text or f"Inter-agent result from {result.recipient}"
            await self._notification_service.notify_all(text)
            return
        env = from_interagent_result(result, chat_id)
        env.transport = "sl"
        await self._bus.submit(env)

    async def on_task_result(self, result: TaskResult) -> None:
        from ductor_bot.bus.adapters import from_task_result

        env = from_task_result(result)
        env.transport = "sl"
        await self._bus.submit(env)

    async def on_task_question(
        self,
        task_id: str,
        question: str,
        prompt_preview: str,
        chat_id: int,
        thread_id: int | None = None,
    ) -> None:
        from ductor_bot.bus.adapters import from_task_question

        if not chat_id:
            chat_id = self._default_chat_id()
        env = from_task_question(task_id, question, prompt_preview, chat_id, topic_id=thread_id)
        env.transport = "sl"
        await self._bus.submit(env)

    def _default_chat_id(self) -> int:
        if self._config.slack.allowed_channels:
            return self._id_map.channel_to_int(self._config.slack.allowed_channels[0])
        if self._last_active_channel:
            return self._id_map.channel_to_int(self._last_active_channel)
        logger.warning("No default chat_id: no allowed_channels and no active channel yet")
        return 0

    async def _watch_restart_marker(self) -> None:
        from ductor_bot.infra.restart import EXIT_RESTART

        marker = _restart_marker_path(self._config.ductor_home)
        while True:
            await asyncio.sleep(2)
            if marker.exists():
                logger.info("Restart marker detected")
                self._exit_code = EXIT_RESTART
                if self._socket_task and not self._socket_task.done():
                    self._socket_task.cancel()
                break
