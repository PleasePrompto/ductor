"""Matrix delivery adapter for the MessageBus.

Translates :class:`Envelope` instances into Matrix messages, mirroring
the structure of ``bus/telegram_transport.py``.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from ductor_bot.bus.cron_sanitize import sanitize_cron_result_text
from ductor_bot.bus.envelope import Envelope, Origin
from ductor_bot.messenger.matrix.sender import MatrixSendOpts
from ductor_bot.messenger.matrix.sender import send_rich as matrix_send_rich
from ductor_bot.text.response_format import SEP, fmt

if TYPE_CHECKING:
    from ductor_bot.messenger.matrix.bot import MatrixBot

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Transport adapter
# ---------------------------------------------------------------------------


class MatrixTransport:
    """Implements the ``TransportAdapter`` protocol for Matrix delivery."""

    def __init__(self, bot: MatrixBot) -> None:
        self._bot = bot

    # -- Protocol methods ---------------------------------------------------

    @property
    def transport_name(self) -> str:
        return "mx"

    async def deliver(self, envelope: Envelope) -> None:
        """Deliver a unicast envelope to the target room."""
        handler = _HANDLERS.get(envelope.origin)
        if handler is not None:
            await handler(self, envelope)
        else:
            logger.warning("No handler for origin=%s", envelope.origin.value)

    async def deliver_broadcast(self, envelope: Envelope) -> None:
        """Deliver an envelope to all allowed rooms."""
        handler = _BROADCAST_HANDLERS.get(envelope.origin)
        if handler is not None:
            await handler(self, envelope)
        else:
            logger.warning("No broadcast handler for origin=%s", envelope.origin.value)

    # -- Internal helpers ---------------------------------------------------

    def _resolve_room(self, env: Envelope) -> str | None:
        """Resolve envelope chat_id back to Matrix room_id."""
        return self._bot.id_map.int_to_room(env.chat_id)

    def _opts(self, env: Envelope) -> MatrixSendOpts:
        orch = self._bot.orchestrator
        roots = self._bot.file_roots(orch.paths) if orch else None
        return MatrixSendOpts(allowed_roots=roots)

    # -- Origin handlers (unicast) -----------------------------------------

    async def _deliver_background(self, env: Envelope) -> None:  # noqa: PLR0912
        room_id = self._resolve_room(env)
        if not room_id:
            return
        elapsed = f"{env.elapsed_seconds:.0f}s"
        if env.session_name:
            orch = self._bot.orchestrator
            if orch is not None:
                session_update = str(env.metadata.get("session_update", "") or "")
                if session_update == "end":
                    orch.named_sessions.end_session(env.chat_id, env.session_name)
                elif env.is_error or env.status == "aborted":
                    orch.named_sessions.set_status(env.chat_id, env.session_name, "idle")
                else:
                    orch.named_sessions.update_after_response(
                        env.chat_id, env.session_name, env.session_id
                    )
            if env.status == "aborted":
                text = fmt(f"**[{env.session_name}] Cancelled**", SEP, f"_{env.prompt_preview}_")
            elif env.is_error:
                body = env.result_text[:2000] if env.result_text else "_No output._"
                text = fmt(f"**[{env.session_name}] Failed** ({elapsed})", SEP, body)
            else:
                text = fmt(
                    f"**[{env.session_name}] Complete** ({elapsed})",
                    SEP,
                    env.result_text or "_No output._",
                )
        else:
            task_id = env.metadata.get("task_id", "?")
            if env.status == "aborted":
                text = fmt(
                    "**Background Task Cancelled**",
                    SEP,
                    f"Task `{task_id}` was cancelled.\nPrompt: _{env.prompt_preview}_",
                )
            elif env.is_error:
                text = fmt(
                    f"**Background Task Failed** ({elapsed})",
                    SEP,
                    f"Task `{task_id}` failed ({env.status}).\nPrompt: _{env.prompt_preview}_\n\n"
                    + (env.result_text[:2000] if env.result_text else "_No output._"),
                )
            else:
                text = fmt(
                    f"**Background Task Complete** ({elapsed})",
                    SEP,
                    env.result_text or "_No output._",
                )
        await matrix_send_rich(self._bot.client, room_id, text, self._opts(env))

    async def _deliver_heartbeat(self, env: Envelope) -> None:
        room_id = self._resolve_room(env)
        if room_id and env.result_text:
            await matrix_send_rich(self._bot.client, room_id, env.result_text, self._opts(env))

    async def _deliver_interagent(self, env: Envelope) -> None:
        room_id = self._resolve_room(env)
        if not room_id:
            return
        if env.is_error:
            session_info = f"\nSession: `{env.session_name}`" if env.session_name else ""
            text = (
                f"**Inter-Agent Request Failed**\n\n"
                f"Agent: `{env.metadata.get('recipient', '?')}`{session_info}\n"
                f"Error: {env.metadata.get('error', 'unknown')}\n"
                f"Request: _{env.prompt_preview}_"
            )
            await matrix_send_rich(self._bot.client, room_id, text)
            return

        notice = env.metadata.get("provider_switch_notice", "")
        if notice:
            await matrix_send_rich(
                self._bot.client,
                room_id,
                f"**Provider Switch Detected**\n\n{notice}",
            )
        if env.result_text:
            await matrix_send_rich(self._bot.client, room_id, env.result_text)

    async def _deliver_task_result(self, env: Envelope) -> None:
        room_id = self._resolve_room(env)
        if not room_id:
            return
        opts = self._opts(env)
        if env.status in {"failed", "timeout", "cancelled"}:
            opts.parse_file_tags = False
        name = env.metadata.get("name", env.metadata.get("task_id", "?"))

        note = ""
        if env.status == "done":
            duration = f"{env.elapsed_seconds:.0f}s"
            target = f"{env.provider}/{env.model}" if env.provider else ""
            detail = f"{duration}, {target}" if target else duration
            note = f"**Task `{name}` completed** ({detail})"
        elif env.status == "cancelled":
            note = f"**Task `{name}` cancelled**"
        elif env.status == "failed":
            note = f"**Task `{name}` failed**\nReason: {env.metadata.get('error', 'unknown')}"

        if note:
            await matrix_send_rich(self._bot.client, room_id, note, opts)
        if env.needs_injection and env.result_text:
            await matrix_send_rich(self._bot.client, room_id, env.result_text, opts)

    async def _deliver_task_question(self, env: Envelope) -> None:
        room_id = self._resolve_room(env)
        if not room_id:
            return
        task_id = env.metadata.get("task_id", "?")
        note = f"**Task `{task_id}` has a question:**\n{env.prompt}"
        await matrix_send_rich(self._bot.client, room_id, note)
        if env.result_text:
            await matrix_send_rich(self._bot.client, room_id, env.result_text)

    async def _deliver_webhook_wake(self, env: Envelope) -> None:
        room_id = self._resolve_room(env)
        if room_id and env.result_text:
            await matrix_send_rich(self._bot.client, room_id, env.result_text)

    async def _deliver_cron(self, env: Envelope) -> None:
        """Deliver cron result to a specific room (unicast).

        Falls back to broadcast when the target room cannot be resolved.
        """
        room_id = self._resolve_room(env)
        if not room_id:
            logger.warning(
                "Cron unicast: cannot resolve chat_id=%d, falling back to broadcast",
                env.chat_id,
            )
            await self._broadcast_cron(env)
            return
        title = env.metadata.get("title", "?")
        clean_result = sanitize_cron_result_text(env.result_text)
        if env.result_text and not clean_result and env.status == "success":
            return
        text = (
            f"**TASK: {title}**\n\n{clean_result}"
            if clean_result
            else f"**TASK: {title}**\n\n_{env.status}_"
        )
        await matrix_send_rich(self._bot.client, room_id, text)

    # -- Origin handlers (broadcast) ----------------------------------------

    async def _broadcast_cron(self, env: Envelope) -> None:
        title = env.metadata.get("title", "?")
        clean_result = sanitize_cron_result_text(env.result_text)
        if env.result_text and not clean_result and env.status == "success":
            return
        text = (
            f"**TASK: {title}**\n\n{clean_result}"
            if clean_result
            else f"**TASK: {title}**\n\n_{env.status}_"
        )
        await self._broadcast(text)

    async def _broadcast_webhook_cron(self, env: Envelope) -> None:
        title = env.metadata.get("hook_title", "?")
        text = (
            f"**WEBHOOK (CRON TASK): {title}**\n\n{env.result_text}"
            if env.result_text
            else f"**WEBHOOK (CRON TASK): {title}**\n\n_{env.status}_"
        )
        await self._broadcast(text)

    async def _broadcast(self, text: str) -> None:
        """Send to all allowed rooms (falls back to last active room)."""
        from ductor_bot.messenger.matrix.bot import resolve_broadcast_rooms

        rooms = resolve_broadcast_rooms(self._bot.config, self._bot._last_active_room)
        if not rooms:
            logger.warning("_broadcast: no rooms available, message lost: %s", text[:80])
            return
        for room_id in rooms:
            await matrix_send_rich(self._bot.client, room_id, text)


# ---------------------------------------------------------------------------
# Handler dispatch tables
# ---------------------------------------------------------------------------

_Handler = Callable[[MatrixTransport, Envelope], Awaitable[None]]

_HANDLERS: dict[Origin, _Handler] = {
    Origin.BACKGROUND: MatrixTransport._deliver_background,
    Origin.CRON: MatrixTransport._deliver_cron,
    Origin.HEARTBEAT: MatrixTransport._deliver_heartbeat,
    Origin.INTERAGENT: MatrixTransport._deliver_interagent,
    Origin.TASK_RESULT: MatrixTransport._deliver_task_result,
    Origin.TASK_QUESTION: MatrixTransport._deliver_task_question,
    Origin.WEBHOOK_WAKE: MatrixTransport._deliver_webhook_wake,
}

_BROADCAST_HANDLERS: dict[Origin, _Handler] = {
    Origin.CRON: MatrixTransport._broadcast_cron,
    Origin.WEBHOOK_CRON: MatrixTransport._broadcast_webhook_cron,
}
