"""Matrix transport bot, parallel to TelegramBot.

Implements BotProtocol so the supervisor can manage it identically
to TelegramBot without knowing which transport is active.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections import deque
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING

from ductor_bot.bus.bus import MessageBus
from ductor_bot.bus.lock_pool import LockPool
from ductor_bot.commands import BOT_COMMANDS, MULTIAGENT_SUB_COMMANDS
from ductor_bot.config import AgentConfig
from ductor_bot.files.allowed_roots import resolve_allowed_roots
from ductor_bot.infra.version import get_current_version
from ductor_bot.matrix.buttons import ButtonTracker
from ductor_bot.matrix.credentials import login_or_restore
from ductor_bot.matrix.id_map import MatrixIdMap
from ductor_bot.matrix.sender import send_rich as matrix_send_rich
from ductor_bot.matrix.typing import MatrixTypingContext
from ductor_bot.notifications import NotificationService
from ductor_bot.session.key import SessionKey
from ductor_bot.text.response_format import SEP, fmt

if TYPE_CHECKING:
    from nio import AsyncClient, MatrixRoom, RoomMessageMedia, RoomMessageText

    from ductor_bot.infra.updater import UpdateObserver
    from ductor_bot.multiagent.bus import AsyncInterAgentResult
    from ductor_bot.orchestrator.core import Orchestrator
    from ductor_bot.tasks.models import TaskResult
    from ductor_bot.workspace.paths import DuctorPaths

logger = logging.getLogger(__name__)


def resolve_broadcast_rooms(config: AgentConfig, last_active_room: str | None) -> list[str]:
    """Return rooms for broadcast: allowed_rooms, or last active room as fallback."""
    rooms = list(config.matrix.allowed_rooms)
    if not rooms and last_active_room:
        rooms = [last_active_room]
    return rooms


class MatrixNotificationService:
    """NotificationService implementation for Matrix."""

    def __init__(self, bot: MatrixBot) -> None:
        self._bot = bot

    async def notify(self, chat_id: int, text: str) -> None:
        room_id = self._bot.id_map.int_to_room(chat_id)
        if room_id:
            event_id = await matrix_send_rich(self._bot.client, room_id, text)
            self._bot._track_sent_event(event_id)
        else:
            logger.warning("notify: cannot resolve chat_id=%d to room, falling back to notify_all", chat_id)
            await self.notify_all(text)

    async def notify_all(self, text: str) -> None:
        for room_id in resolve_broadcast_rooms(self._bot.config, self._bot._last_active_room):
            event_id = await matrix_send_rich(self._bot.client, room_id, text)
            self._bot._track_sent_event(event_id)


class MatrixBot:
    """Matrix transport bot implementing BotProtocol."""

    def __init__(self, config: AgentConfig, *, agent_name: str = "main") -> None:
        try:
            from nio import AsyncClient
        except ImportError:
            raise ImportError(
                "matrix-nio is required for Matrix transport. "
                "Install with: pip install 'ductor[matrix]'"
            ) from None

        self._config = config
        self._agent_name = agent_name
        mx = config.matrix
        self._store_path = Path(config.ductor_home).expanduser() / mx.store_path
        self._store_path.mkdir(parents=True, exist_ok=True)

        self._client = AsyncClient(mx.homeserver, mx.user_id)
        self._id_map = MatrixIdMap(self._store_path)
        self._button_tracker = ButtonTracker()
        self._lock_pool = LockPool()
        self._bus = MessageBus(lock_pool=self._lock_pool)

        from ductor_bot.matrix.transport import MatrixTransport

        self._bus.register_transport(MatrixTransport(self))

        self._orchestrator: Orchestrator | None = None
        self._startup_hooks: list[Callable[[], Awaitable[None]]] = []
        self._notification_service: NotificationService = MatrixNotificationService(self)
        self._abort_all_callback: Callable[[], Awaitable[int]] | None = None
        self._exit_code: int = 0
        self._update_observer: UpdateObserver | None = None
        self._restart_watcher: asyncio.Task[None] | None = None
        self._sync_task: asyncio.Task[None] | None = None

        # Pre-compute allowed rooms set (resolve aliases later if needed)
        self._allowed_rooms_set: set[str] = set(mx.allowed_rooms)

        # Track sent event IDs for reply-to-bot detection (bounded)
        self._sent_event_ids: deque[str] = deque(maxlen=1000)

        # Rooms currently in join→leave cycle (reject flow)
        self._leaving_rooms: set[str] = set()

        # Last room that sent a message (fallback for delivery when allowed_rooms is empty)
        self._last_active_room: str | None = None

    # --- BotProtocol implementation ---

    @property
    def _orch(self) -> Orchestrator:
        if self._orchestrator is None:
            msg = "Orchestrator not initialized -- call after startup"
            raise RuntimeError(msg)
        return self._orchestrator

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
    def client(self) -> AsyncClient:
        """The nio AsyncClient instance."""
        return self._client

    @property
    def id_map(self) -> MatrixIdMap:
        return self._id_map

    def register_startup_hook(self, hook: Callable[[], Awaitable[None]]) -> None:
        self._startup_hooks.append(hook)

    def set_abort_all_callback(self, callback: Callable[[], Awaitable[int]]) -> None:
        self._abort_all_callback = callback

    def file_roots(self, paths: DuctorPaths) -> list[Path] | None:
        return resolve_allowed_roots(self._config.file_access, paths.workspace)

    async def run(self) -> int:
        """Login, sync, run event loop."""
        from nio import (
            InviteMemberEvent,
            ReactionEvent,
            RoomMessageAudio,
            RoomMessageFile,
            RoomMessageImage,
            RoomMessageText,
            RoomMessageVideo,
        )

        await login_or_restore(self._client, self._config.matrix, self._store_path)

        # Restore sync token (Risk R2 mitigation)
        self._restore_sync_token()

        # Register event callbacks
        self._client.add_event_callback(self._on_message, RoomMessageText)
        self._client.add_event_callback(self._on_media, RoomMessageImage)
        self._client.add_event_callback(self._on_media, RoomMessageAudio)
        self._client.add_event_callback(self._on_media, RoomMessageVideo)
        self._client.add_event_callback(self._on_media, RoomMessageFile)
        self._client.add_event_callback(self._on_reaction, ReactionEvent)
        self._client.add_event_callback(self._on_invite, InviteMemberEvent)

        # Initial sync to populate room list (needed for notifications before
        # any user message arrives, e.g. inter-agent delivery).
        await self._client.sync(timeout=10000, full_state=True)
        self._populate_rooms_from_sync()

        # Run startup (orchestrator, observers, hooks)
        from ductor_bot.matrix.startup import run_matrix_startup

        await run_matrix_startup(self)

        # Start restart marker watcher
        self._restart_watcher = asyncio.create_task(self._watch_restart_marker())

        # Sync loop (blocks) — wrap in task so restart watcher can cancel it
        self._exit_code = 0
        self._sync_task = asyncio.current_task()
        try:
            await self._client.sync_forever(timeout=30000, full_state=False)
        except asyncio.CancelledError:
            pass
        except Exception:
            logger.exception("sync_forever exited with error")

        return self._exit_code

    async def shutdown(self) -> None:
        """Gracefully shut down."""
        if self._restart_watcher:
            self._restart_watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._restart_watcher

        if self._update_observer:
            self._update_observer.stop()

        self._save_sync_token()
        await self._client.close()

        if self._orchestrator:
            await self._orchestrator.shutdown()

        logger.info("MatrixBot shut down")

    # --- Message handling ---

    async def _on_message(self, room: MatrixRoom | object, event: RoomMessageText | object) -> None:
        """Handle incoming room messages."""
        from nio import MatrixRoom, RoomMessageText

        if not isinstance(room, MatrixRoom) or not isinstance(event, RoomMessageText):
            return

        if event.sender == self._client.user_id:
            return  # Ignore own messages

        if room.room_id in self._leaving_rooms:
            return  # Room is in join→leave rejection cycle

        if not self._is_authorized(room, event):
            return

        text = event.body.strip()
        if not text:
            return

        # Group mention-only filter: in multi-user rooms, ignore
        # messages not addressed to the bot via @mention or reply.
        is_group_room = not self._is_dm_room(room)
        if is_group_room and self._config.group_mention_only:
            if not self._is_message_addressed(event):
                return
            text = self._strip_mention(text)

        room_id = room.room_id
        self._last_active_room = room_id
        chat_id = self._id_map.room_to_int(room_id)

        # Check button match (text-input fallback for reactions)
        button_match = self._button_tracker.match_input(room_id, text)
        if button_match:
            await self._handle_button_callback(room_id, "", button_match)
            return

        # Handle commands (! prefix for Matrix, / also accepted)
        if text.startswith("!") or text.startswith("/"):
            await self._handle_command(text, room_id, chat_id, event)
            return

        key = SessionKey(chat_id=chat_id, topic_id=None)
        asyncio.create_task(
            self._dispatch_with_lock(key, text, room_id, event),
            name=f"mx-msg-{room_id[:8]}",
        )

    async def _on_media(self, room: MatrixRoom | object, event: RoomMessageMedia | object) -> None:
        """Handle incoming media messages (images, audio, video, files)."""
        from nio import MatrixRoom, RoomMessageMedia

        if not isinstance(room, MatrixRoom):
            return
        if not isinstance(event, RoomMessageMedia):
            return

        if event.sender == self._client.user_id:
            return  # Ignore own messages

        if room.room_id in self._leaving_rooms:
            return

        if not self._is_authorized(room, event):
            return

        # Group mention-only filter: in group rooms, only process if addressed
        is_group_room = not self._is_dm_room(room)
        if is_group_room and self._config.group_mention_only:
            if not self._is_message_addressed(event):
                return

        room_id = room.room_id
        self._last_active_room = room_id
        chat_id = self._id_map.room_to_int(room_id)

        # Download and build prompt
        from ductor_bot.matrix.media import resolve_matrix_media

        paths = self._orch.paths

        async def _on_error(msg: str) -> None:
            await self._send_rich(room_id, msg)

        text = await resolve_matrix_media(
            self._client, event, paths.matrix_files_dir, paths.workspace,
            error_callback=_on_error,
        )

        if not text:
            return

        key = SessionKey(chat_id=chat_id, topic_id=None)
        asyncio.create_task(
            self._dispatch_with_lock(key, text, room_id, event),
            name=f"mx-media-{room_id[:8]}",
        )

    # Commands routed to the orchestrator's CommandRegistry
    _ORCHESTRATOR_COMMANDS = frozenset({
        "status", "model", "memory", "cron", "diagnose", "upgrade",
        "sessions", "tasks", "agents", "agent_start", "agent_stop",
        "agent_restart",
    })

    async def _handle_command(
        self, text: str, room_id: str, chat_id: int, event: object
    ) -> None:
        """Handle commands in Matrix. Supports both !cmd and /cmd prefixes."""
        # Normalize: strip prefix, extract command name
        cmd = text.split()[0].lower().lstrip("/!")
        # Ensure text has / prefix for orchestrator compatibility
        if text.startswith("!"):
            text = "/" + text[1:]
        key = SessionKey(chat_id=chat_id, topic_id=None)

        handler = self._COMMAND_DISPATCH.get(cmd)
        if handler is not None:
            if cmd in self._IMMEDIATE_COMMANDS:
                # Immediate commands (stop, interrupt, help, …) run without
                # the lock so they can abort in-flight work instantly.
                await handler(self, text=text, room_id=room_id, key=key, event=event)
            else:
                # Other dispatch-table commands (new, session) may call the
                # orchestrator — run as a background task with the lock.
                asyncio.create_task(
                    self._run_handler_with_lock(
                        handler, text=text, room_id=room_id, key=key, event=event,
                    ),
                    name=f"mx-cmd-{cmd}",
                )
        elif cmd in self._ORCHESTRATOR_COMMANDS:
            # Orchestrator commands may call the CLI — run with lock.
            asyncio.create_task(
                self._cmd_orchestrator_locked(text=text, room_id=room_id, key=key, event=event),
                name=f"mx-orch-{cmd}",
            )
        else:
            # Unknown command → treat as regular message
            asyncio.create_task(
                self._dispatch_with_lock(key, text, room_id, event),
                name=f"mx-cmd-{cmd}",
            )

    # -- Individual command handlers ----------------------------------------

    async def _cmd_stop(self, *, text: str, room_id: str, key: SessionKey, event: object) -> None:
        orch = self._orchestrator
        if orch:
            killed = await orch.abort(key.chat_id)
            msg = f"Stopped {killed} process(es)." if killed else "No active processes."
            await self._send_rich(room_id, msg)

    async def _cmd_interrupt(self, *, text: str, room_id: str, key: SessionKey, event: object) -> None:
        orch = self._orchestrator
        if orch:
            interrupted = orch.interrupt(key.chat_id)
            msg = f"Interrupted {interrupted} process(es)." if interrupted else "No active processes."
            await self._send_rich(room_id, msg)

    async def _cmd_stop_all(self, *, text: str, room_id: str, key: SessionKey, event: object) -> None:
        orch = self._orchestrator
        killed = 0
        if orch:
            killed = await orch.abort(key.chat_id)
        if self._abort_all_callback:
            killed += await self._abort_all_callback()
        msg = f"Stopped {killed} process(es)." if killed else "No active processes."
        await self._send_rich(room_id, msg)

    async def _cmd_restart(self, *, text: str, room_id: str, key: SessionKey, event: object) -> None:
        from ductor_bot.infra.restart import EXIT_RESTART, write_restart_marker

        home = Path(self._config.ductor_home).expanduser()
        write_restart_marker(marker_path=home / "restart-requested")
        await self._send_rich(
            room_id,
            fmt("**Restarting**", SEP, "Bot is shutting down and will be back shortly."),
        )
        self._exit_code = EXIT_RESTART
        if self._sync_task and not self._sync_task.done():
            self._sync_task.cancel()

    async def _cmd_new(self, *, text: str, room_id: str, key: SessionKey, event: object) -> None:
        orch = self._orchestrator
        if orch:
            result = await orch.handle_message(key, "/new")
            if result and result.text:
                await self._send_rich(room_id, result.text)

    async def _cmd_help(self, *, text: str, room_id: str, key: SessionKey, event: object) -> None:
        await self._send_rich(room_id, self._build_help_text())

    async def _cmd_info(self, *, text: str, room_id: str, key: SessionKey, event: object) -> None:
        version = get_current_version()
        text_out = fmt(
            "**ductor.dev**",
            f"Version: `{version}`",
            SEP,
            "AI coding agents (Claude, Codex, Gemini) on Matrix.\n"
            "Named sessions, persistent memory, cron jobs, webhooks, live streaming.",
        )
        await self._send_rich(room_id, text_out)

    async def _cmd_agent_commands(self, *, text: str, room_id: str, key: SessionKey, event: object) -> None:
        lines = [
            "The multi-agent system lets you run additional bots as "
            "sub-agents — each with its own workspace and user list. "
            "All agents share a single process and can communicate "
            "via the inter-agent bus.",
            "",
            "**Commands**",
            "`!agents` — list all agents and their status",
            "`!agent_start <name>` — start a sub-agent",
            "`!agent_stop <name>` — stop a sub-agent",
            "`!agent_restart <name>` — restart a sub-agent",
            "",
            "**Setup**",
            "Ask your agent to create a new sub-agent or edit "
            "`agents.json` in your ductor home directory.",
        ]
        await self._send_rich(
            room_id,
            fmt("**Multi-Agent System**", SEP, "\n".join(lines)),
        )

    async def _cmd_showfiles(self, *, text: str, room_id: str, key: SessionKey, event: object) -> None:
        await self._send_rich(
            room_id,
            "File browser is not yet supported in Matrix. "
            "Use `!status` to see workspace info.",
        )

    async def _cmd_session(self, *, text: str, room_id: str, key: SessionKey, event: object) -> None:
        parts = text.split(None, 1)
        if len(parts) < 2 or not parts[1].strip():
            await self._send_rich(
                room_id,
                fmt(
                    "**Background Sessions**", SEP,
                    "`!session <prompt>` — start a background session\n"
                    "`!sessions` — view and manage all sessions\n"
                    "`!stop` — cancel running session",
                ),
            )
            return
        # Session with prompt — route to orchestrator as conversation
        await self._dispatch_message(key, text, room_id, event)

    async def _cmd_orchestrator(self, *, text: str, room_id: str, key: SessionKey, event: object) -> None:
        orch = self._orchestrator
        if not orch:
            return
        result = await orch.handle_message(key, text)
        if result and result.text:
            await self._send_selector_response(room_id, result.text, result.buttons)

    async def _dispatch_with_lock(
        self, key: SessionKey, text: str, room_id: str, event: object
    ) -> None:
        """Acquire the per-chat lock, then dispatch the message.

        Spawned as an ``asyncio.create_task`` so the nio sync loop is never
        blocked, allowing ``!stop`` / ``!interrupt`` to be processed
        immediately even while a long-running message is in flight.
        """
        lock = self._lock_pool.get(key.lock_key)
        async with lock:
            await self._dispatch_message(key, text, room_id, event)

    async def _run_handler_with_lock(
        self, handler: Callable[..., Awaitable[None]], **kwargs: object
    ) -> None:
        """Run a command handler under the per-chat lock."""
        key: SessionKey = kwargs["key"]  # type: ignore[assignment]
        lock = self._lock_pool.get(key.lock_key)
        async with lock:
            await handler(self, **kwargs)

    async def _cmd_orchestrator_locked(
        self, *, text: str, room_id: str, key: SessionKey, event: object
    ) -> None:
        """Run an orchestrator command under the per-chat lock."""
        lock = self._lock_pool.get(key.lock_key)
        async with lock:
            await self._cmd_orchestrator(text=text, room_id=room_id, key=key, event=event)

    async def _dispatch_message(self, key: SessionKey, text: str, room_id: str, event: object) -> None:
        """Route a message through streaming or non-streaming pipeline."""
        if self._config.streaming.enabled:
            await self._run_streaming(key, text, room_id, event)
        else:
            await self._run_non_streaming(key, text, room_id, event)

    # Dispatch table: command name → handler method
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

    # Commands that run immediately without the per-chat lock.
    # These must be fast and non-blocking (no CLI calls).
    _IMMEDIATE_COMMANDS: frozenset[str] = frozenset({
        "stop", "stop_all", "interrupt", "restart",
        "help", "start", "info", "agent_commands", "showfiles",
    })

    def _build_help_text(self) -> str:
        """Build the help text showing all available commands."""
        cmd_desc = {**dict(BOT_COMMANDS), **dict(MULTIAGENT_SUB_COMMANDS)}

        def _line(c: str) -> str:
            desc = cmd_desc.get(c, "")
            return f"`!{c}` — {desc}" if desc else f"`!{c}`"

        return fmt(
            "**Command Reference**",
            SEP,
            f"**Daily**\n{_line('new')}\n{_line('stop')}\n{_line('stop_all')}\n"
            f"{_line('model')}\n{_line('status')}\n{_line('memory')}",
            f"**Automation**\n{_line('session')}\n{_line('tasks')}\n{_line('cron')}",
            f"**Multi-Agent**\n{_line('agent_commands')}\n{_line('agents')}\n"
            f"{_line('agent_start')}\n{_line('agent_stop')}\n{_line('agent_restart')}",
            f"**Browse & Info**\n{_line('showfiles')}\n{_line('info')}\n{_line('help')}",
            f"**Maintenance**\n{_line('diagnose')}\n{_line('upgrade')}\n{_line('restart')}",
            SEP,
            "Use `!` or `/` prefix. Send any message to start.",
        )

    async def _run_streaming(
        self, key: SessionKey, text: str, room_id: str, event: object
    ) -> None:
        """Run with streaming — each reasoning segment as a separate message."""
        orch = self._orchestrator
        if orch is None:
            return

        buffer = [""]
        segment_count = [0]

        async def _on_delta(delta: str) -> None:
            buffer[0] += delta

        async def _flush_and_tag(tag: str) -> None:
            """Flush buffer and re-set typing indicator.

            The *tag* (tool/system marker) is intentionally not sent to
            keep the Matrix chat clean — only reasoning text and the
            final summary are visible to the user.
            """
            seg_text = buffer[0].strip()
            if seg_text:
                await self._send_rich(room_id, buffer[0])
            buffer[0] = ""
            # Re-set typing indicator (sending messages clears it in Matrix)
            with contextlib.suppress(Exception):
                await self._client.room_typing(
                    room_id, typing_state=True, timeout=30000
                )

        async def _on_tool(tool_name: str) -> None:
            segment_count[0] += 1
            logger.info(
                "Matrix streaming: tool=%s segment=%d buf_len=%d",
                tool_name,
                segment_count[0],
                len(buffer[0].strip()),
            )
            await _flush_and_tag(f"**[TOOL: {tool_name}]**")

        async def _on_system(status: str | None) -> None:
            system_map: dict[str, str] = {
                "thinking": "THINKING",
                "compacting": "COMPACTING",
                "recovering": "Please wait, recovering...",
                "timeout_warning": "TIMEOUT APPROACHING",
                "timeout_extended": "TIMEOUT EXTENDED",
            }
            label = system_map.get(status or "")
            if label is None:
                return
            await _flush_and_tag(f"*[{label}]*")

        async with MatrixTypingContext(self._client, room_id):
            result = await orch.handle_message_streaming(
                key,
                text,
                on_text_delta=_on_delta,
                on_tool_activity=_on_tool,
                on_system_status=_on_system,
            )

        # Send the final segment (with button extraction).
        final_text = buffer[0].strip()
        logger.info(
            "Matrix streaming done: segments=%d final_buf_len=%d result_len=%d",
            segment_count[0],
            len(final_text),
            len(result.text) if result.text else 0,
        )
        if final_text:
            formatted = self._button_tracker.extract_and_format(room_id, final_text)
            await self._send_rich(room_id, formatted)
        elif result.text:
            # Fallback: no deltas received but orchestrator returned text.
            formatted = self._button_tracker.extract_and_format(room_id, result.text)
            await self._send_rich(room_id, formatted)

    async def _run_non_streaming(
        self, key: SessionKey, text: str, room_id: str, event: object
    ) -> None:
        """Run without streaming."""
        orch = self._orchestrator
        if orch is None:
            return

        async with MatrixTypingContext(self._client, room_id):
            result = await orch.handle_message(key, text)

        if result.text:
            formatted = self._button_tracker.extract_and_format(room_id, result.text)
            await self._send_rich(room_id, formatted)

    def _is_authorized(self, room: object, event: object) -> bool:
        """Check if the sender/room is authorized.

        In group rooms with ``group_mention_only``, the user check is
        bypassed — the @mention itself acts as the access gate (matching
        Telegram behaviour).
        """
        mx = self._config.matrix
        room_id = getattr(room, "room_id", "")
        sender = getattr(event, "sender", "")

        room_ok = not mx.allowed_rooms or room_id in self._allowed_rooms_set

        # In group rooms with group_mention_only, skip user check
        if self._config.group_mention_only and not self._is_dm_room(room):
            return room_ok

        user_ok = not mx.allowed_users or sender in mx.allowed_users
        return room_ok and user_ok

    # --- Group / mention helpers ---

    @staticmethod
    def _is_dm_room(room: object) -> bool:
        """True if the room is a direct message (2 or fewer members).

        Named rooms (with a name or canonical alias) are always treated
        as groups, even when ``member_count`` is low — nio may not have
        the full member list yet right after joining.
        """
        # Named rooms are never DMs
        name = getattr(room, "name", None)
        alias = getattr(room, "canonical_alias", None)
        if name or alias:
            return False
        member_count = getattr(room, "member_count", 0)
        return member_count <= 2

    def _is_message_addressed(self, event: object) -> bool:
        """True if a group message is addressed to this bot.

        Checks:
        1. Bot's Matrix user_id in plain text body
        2. Bot's Matrix user_id in formatted_body (HTML mention pill)
        3. Reply to a message previously sent by this bot
        """
        bot_user_id = self._client.user_id
        if not bot_user_id:
            return False

        body = getattr(event, "body", "") or ""
        formatted_body = getattr(event, "formatted_body", "") or ""

        # 1+2: user_id in body or formatted_body
        if bot_user_id in body or bot_user_id in formatted_body:
            return True

        # 3: reply to a bot message
        source = getattr(event, "source", {})
        content = source.get("content", {}) if isinstance(source, dict) else {}
        relates_to = content.get("m.relates_to", {})
        if isinstance(relates_to, dict):
            reply_to = relates_to.get("m.in_reply_to", {})
            if isinstance(reply_to, dict):
                replied_id = reply_to.get("event_id")
                if replied_id and replied_id in self._sent_event_ids:
                    return True

        return False

    def _strip_mention(self, text: str) -> str:
        """Remove the bot's Matrix user_id from *text*."""
        bot_user_id = self._client.user_id
        if bot_user_id and bot_user_id in text:
            return text.replace(bot_user_id, "").strip()
        return text

    def _track_sent_event(self, event_id: str | None) -> None:
        """Record a sent event ID for reply-to-bot detection."""
        if event_id:
            self._sent_event_ids.append(event_id)

    async def _send_rich(self, room_id: str, text: str) -> str | None:
        """Send a message and track the event ID for reply detection."""
        event_id = await matrix_send_rich(self._client, room_id, text)
        self._track_sent_event(event_id)
        return event_id

    # --- Selector response (ButtonGrid → reactions) ---

    async def _send_selector_response(
        self,
        room_id: str,
        text: str,
        buttons: object | None = None,
    ) -> None:
        """Send a selector response: text + optional reaction-based buttons."""
        from ductor_bot.matrix.buttons import REACTION_DIGITS
        from ductor_bot.orchestrator.selectors.models import ButtonGrid

        if not isinstance(buttons, ButtonGrid) or not buttons.rows:
            await self._send_rich(room_id, text)
            return

        all_buttons = [btn for row in buttons.rows for btn in row]
        labels = [btn.text for btn in all_buttons]
        cbs = [btn.callback_data for btn in all_buttons]

        # Build numbered list with emoji digits
        numbered = "\n".join(
            f"  {REACTION_DIGITS[i]} {lbl}" if i < len(REACTION_DIGITS) else f"  {i + 1}. {lbl}"
            for i, lbl in enumerate(labels)
        )
        out = f"{text}\n\n{numbered}"
        event_id = await self._send_rich(room_id, out)

        if event_id:
            # Register for reaction matching
            self._button_tracker.register_buttons(room_id, event_id, labels, cbs)
            # Send reactions as clickable indicators
            for i in range(min(len(labels), len(REACTION_DIGITS))):
                try:
                    await self._client.room_send(
                        room_id,
                        "m.reaction",
                        {
                            "m.relates_to": {
                                "rel_type": "m.annotation",
                                "event_id": event_id,
                                "key": REACTION_DIGITS[i],
                            }
                        },
                    )
                except Exception:
                    logger.debug("Failed to send reaction %d", i + 1, exc_info=True)
                    break

    # --- Reaction handling ---

    async def _on_reaction(self, room: object, event: object) -> None:
        """Handle m.reaction events for button selection."""
        from nio import MatrixRoom, ReactionEvent

        if not isinstance(room, MatrixRoom) or not isinstance(event, ReactionEvent):
            return
        if event.sender == self._client.user_id:
            return  # Ignore own reactions

        room_id = room.room_id
        cb = self._button_tracker.match_reaction(room_id, event.reacts_to, event.key)
        if cb is None:
            return

        logger.info("Reaction button match: room=%s key=%s cb=%s", room_id, event.key, cb)
        await self._handle_button_callback(room_id, event.reacts_to, cb)

    async def _handle_button_callback(self, room_id: str, message_event_id: str, callback_data: str) -> None:
        """Route a button callback_data through the selector handlers."""
        from ductor_bot.orchestrator.selectors.cron_selector import (
            handle_cron_callback,
            is_cron_selector_callback,
        )
        from ductor_bot.orchestrator.selectors.model_selector import (
            handle_model_callback,
            is_model_selector_callback,
        )
        from ductor_bot.orchestrator.selectors.session_selector import (
            handle_session_callback,
            is_session_selector_callback,
        )
        from ductor_bot.orchestrator.selectors.task_selector import (
            handle_task_callback,
            is_task_selector_callback,
        )

        orch = self._orchestrator
        if not orch:
            return

        chat_id = self._id_map.room_to_int(room_id)
        key = SessionKey(chat_id=chat_id, topic_id=None)
        resp = None

        if is_model_selector_callback(callback_data):
            resp = await handle_model_callback(orch, key, callback_data)
        elif is_cron_selector_callback(callback_data):
            resp = await handle_cron_callback(orch, callback_data)
        elif is_session_selector_callback(callback_data):
            resp = await handle_session_callback(orch, chat_id, callback_data)
        elif is_task_selector_callback(callback_data):
            resp = await handle_task_callback(orch, callback_data)
        else:
            # Unknown callback — treat as text input to the orchestrator
            result = await orch.handle_message(key, callback_data)
            if result and result.text:
                await self._send_rich(room_id, result.text)
            return

        if resp:
            await self._send_selector_response(room_id, resp.text, resp.buttons)

    # --- Join notification ---

    async def _send_join_notification(self, room_id: str) -> None:
        """Send JOIN_NOTIFICATION.md content and try to pin it."""
        if not self._orchestrator:
            return
        path = self._orchestrator.paths.join_notification_path
        if not path.is_file():
            return
        text = path.read_text(encoding="utf-8").strip()
        if not text:
            return
        event_id = await self._send_rich(room_id, text)
        if event_id:
            try:
                from nio import RoomPutStateError

                resp = await self._client.room_put_state(
                    room_id=room_id,
                    event_type="m.room.pinned_events",
                    content={"pinned": [event_id]},
                    state_key="",
                )
                if isinstance(resp, RoomPutStateError):
                    logger.warning("Could not pin join notification in %s: %s", room_id, resp.message)
            except Exception:
                logger.warning("Failed to pin join notification in %s", room_id, exc_info=True)

    # --- Room invite handling ---

    async def _on_invite(self, room: object, event: object) -> None:
        """Auto-join allowed rooms; reject and leave unauthorized ones."""
        room_id = getattr(room, "room_id", "")
        if not self._allowed_rooms_set or room_id in self._allowed_rooms_set:
            await self._client.join(room_id)
            logger.info("Auto-joined room: %s", room_id)
            self._last_active_room = room_id
            await self._send_join_notification(room_id)
        elif self._allowed_rooms_set:
            # Unauthorized room — join briefly to send rejection, then leave
            self._leaving_rooms.add(room_id)
            try:
                await self._client.join(room_id)
                await self._send_rich(room_id, "This bot is not authorized for this room.")
                await self._client.room_leave(room_id)
                logger.info("Auto-left unauthorized room: %s", room_id)
            finally:
                self._leaving_rooms.discard(room_id)

    # --- Room discovery ---

    def _populate_rooms_from_sync(self) -> None:
        """Pre-populate id_map and _last_active_room from joined rooms.

        After the initial sync, ``self._client.rooms`` contains all joined
        rooms.  We register them in the id_map so that inter-agent
        notifications can resolve a target room even before a user sends a
        direct message.
        """
        rooms = getattr(self._client, "rooms", {})
        if not rooms:
            return
        for room_id in rooms:
            self._id_map.room_to_int(room_id)  # ensure mapping exists
        # Set _last_active_room to the first DM-like room, or any room
        if self._last_active_room is None:
            # Prefer DM rooms (unnamed, ≤2 members) as default target
            for room_id, room in rooms.items():
                if self._is_dm_room(room):
                    self._last_active_room = room_id
                    break
            # Fallback: first joined room
            if self._last_active_room is None:
                self._last_active_room = next(iter(rooms))
        logger.info("Populated %d rooms from sync, default=%s", len(rooms), self._last_active_room)

    # --- Sync token persistence ---

    def _restore_sync_token(self) -> None:
        token_file = self._store_path / "next_batch"
        if token_file.exists():
            self._client.next_batch = token_file.read_text(encoding="utf-8").strip()

    def _save_sync_token(self) -> None:
        if self._client.next_batch:
            token_file = self._store_path / "next_batch"
            token_file.parent.mkdir(parents=True, exist_ok=True)
            token_file.write_text(self._client.next_batch, encoding="utf-8")

    # --- Inter-agent & task handlers (BotProtocol) ---

    async def on_async_interagent_result(self, result: AsyncInterAgentResult) -> None:
        from ductor_bot.bus.adapters import from_interagent_result

        chat_id = self._default_chat_id()
        if not chat_id:
            logger.warning("No chat_id for async interagent result (task=%s) — delivering to all rooms", result.task_id)
            text = result.result_text or f"Inter-agent result from {result.recipient}"
            await self._notification_service.notify_all(text)
            return
        await self._bus.submit(from_interagent_result(result, chat_id))

    async def on_task_result(self, result: TaskResult) -> None:
        from ductor_bot.bus.adapters import from_task_result

        await self._bus.submit(from_task_result(result))

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
        await self._bus.submit(from_task_question(task_id, question, prompt_preview, chat_id))

    def _default_chat_id(self) -> int:
        """Default delivery target: first allowed room, or last active room."""
        if self._config.matrix.allowed_rooms:
            return self._id_map.room_to_int(self._config.matrix.allowed_rooms[0])
        if self._last_active_room:
            return self._id_map.room_to_int(self._last_active_room)
        logger.warning("No default chat_id: no allowed_rooms and no active room yet")
        return 0

    # --- Restart watcher ---

    async def _watch_restart_marker(self) -> None:
        """Watch for restart marker file (created by /restart command)."""
        from ductor_bot.infra.restart import EXIT_RESTART

        marker = Path(self._config.ductor_home).expanduser() / "restart-requested"
        while True:
            await asyncio.sleep(2)
            if marker.exists():
                logger.info("Restart marker detected")
                self._exit_code = EXIT_RESTART
                if self._sync_task and not self._sync_task.done():
                    self._sync_task.cancel()
                break

    async def broadcast(self, text: str) -> None:
        """Send a message to all allowed rooms (falls back to last active room)."""
        rooms = resolve_broadcast_rooms(self._config, self._last_active_room)
        if not rooms:
            logger.warning("broadcast: no rooms available, message lost: %s", text[:80])
            return
        for room_id in rooms:
            await self._send_rich(room_id, text)
