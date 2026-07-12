"""Telegram delivery adapter for the MessageBus.

Translates :class:`Envelope` instances into Telegram messages using
the formatting logic that was previously spread across
``bot/result_delivery.py``.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import TYPE_CHECKING

from ductor_bot.bus.cron_sanitize import sanitize_cron_result_text
from ductor_bot.bus.envelope import Envelope, Origin
from ductor_bot.messenger.telegram.sender import SendRichOpts, send_rich
from ductor_bot.text.response_format import SEP, fmt

if TYPE_CHECKING:
    from ductor_bot.messenger.telegram.app import TelegramBot

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Transport adapter
# ---------------------------------------------------------------------------


class TelegramTransport:
    """Implements the ``TransportAdapter`` protocol for Telegram delivery."""

    def __init__(self, bot: TelegramBot) -> None:
        self._bot = bot

    # -- Protocol methods ---------------------------------------------------

    @property
    def transport_name(self) -> str:
        return "tg"

    async def deliver(self, envelope: Envelope) -> None:
        """Deliver a unicast envelope to the target chat_id."""
        handler = _HANDLERS.get(envelope.origin)
        if handler is not None:
            await handler(self, envelope)
        else:
            logger.warning("No handler for origin=%s", envelope.origin.value)

    async def deliver_broadcast(self, envelope: Envelope) -> None:
        """Deliver an envelope to all allowed users."""
        handler = _BROADCAST_HANDLERS.get(envelope.origin)
        if handler is not None:
            await handler(self, envelope)
        else:
            logger.warning("No broadcast handler for origin=%s", envelope.origin.value)

    # -- Internal helpers ---------------------------------------------------

    def _roots(self) -> list[Path] | None:
        return self._bot.file_roots(self._bot._orch.paths)

    def _opts(self, envelope: Envelope) -> SendRichOpts:
        return SendRichOpts(
            reply_to_message_id=envelope.reply_to_message_id,
            allowed_roots=self._roots(),
            thread_id=envelope.topic_id or envelope.thread_id,
        )

    # -- Origin handlers (unicast) -----------------------------------------

    async def _deliver_background(self, env: Envelope) -> None:
        """Deliver background session / stateless task result."""
        elapsed = f"{env.elapsed_seconds:.0f}s"

        if env.session_name:
            # Update named session registry
            self._bot._orch.named_sessions.update_after_response(
                env.chat_id, env.session_name, env.session_id
            )
            text = self._format_named_session(env, elapsed)
            from ductor_bot.messenger.telegram.buttons import extract_buttons_for_session

            cleaned, markup = extract_buttons_for_session(text, env.session_name)
            opts = self._opts(env)
            opts.reply_markup = markup
            await send_rich(self._bot.bot_instance, env.chat_id, cleaned, opts)
        else:
            text = self._format_stateless(env, elapsed)
            await send_rich(self._bot.bot_instance, env.chat_id, text, self._opts(env))

    @staticmethod
    def _format_named_session(env: Envelope, elapsed: str) -> str:
        name = env.session_name
        if env.status == "aborted":
            return fmt(f"**[{name}] Cancelled**", SEP, f"_{env.prompt_preview}_")
        if env.is_error:
            body = env.result_text[:2000] if env.result_text else "_No output._"
            return fmt(f"**[{name}] Failed** ({elapsed})", SEP, body)
        return fmt(f"**[{name}] Complete** ({elapsed})", SEP, env.result_text or "_No output._")

    @staticmethod
    def _format_stateless(env: Envelope, elapsed: str) -> str:
        task_id = env.metadata.get("task_id", "?")
        if env.status == "aborted":
            return fmt(
                "**Background Task Cancelled**",
                SEP,
                f"Task `{task_id}` was cancelled.\nPrompt: _{env.prompt_preview}_",
            )
        if env.is_error:
            return fmt(
                f"**Background Task Failed** ({elapsed})",
                SEP,
                f"Task `{task_id}` failed ({env.status}).\n"
                f"Prompt: _{env.prompt_preview}_\n\n"
                + (env.result_text[:2000] if env.result_text else "_No output._"),
            )
        return fmt(
            f"**Background Task Complete** ({elapsed})",
            SEP,
            env.result_text or "_No output._",
        )

    async def _deliver_heartbeat(self, env: Envelope) -> None:
        """Deliver heartbeat to chat/topic. Falls back to main user on failure."""
        from aiogram.exceptions import TelegramAPIError

        opts = SendRichOpts(allowed_roots=self._roots(), thread_id=env.topic_id)
        try:
            await send_rich(self._bot.bot_instance, env.chat_id, env.result_text, opts)
            logger.info("Heartbeat delivered")
        except TelegramAPIError:
            target = f"Chat {env.chat_id}"
            if env.topic_id:
                target += f" / Topic {env.topic_id}"
            logger.warning("Heartbeat delivery failed for %s, falling back to main user", target)
            fallback_text = (
                f"**Heartbeat delivery failed**\n\n"
                f"Could not deliver to {target}.\n\n"
                f"---\n{env.result_text}"
            )
            fallback_id = self._bot._config.allowed_user_ids[0]
            await send_rich(
                self._bot.bot_instance,
                fallback_id,
                fallback_text,
                SendRichOpts(allowed_roots=self._roots()),
            )

    async def _deliver_interagent(self, env: Envelope) -> None:
        """Deliver inter-agent result (error notification or injected response)."""
        opts = self._opts(env)

        if env.is_error:
            session_info = f"\nSession: `{env.session_name}`" if env.session_name else ""
            error_text = (
                f"**Inter-Agent Request Failed**\n\n"
                f"Agent: `{env.metadata.get('recipient', '?')}`{session_info}\n"
                f"Error: {env.metadata.get('error', 'unknown')}\n"
                f"Request: _{env.prompt_preview}_"
            )
            await send_rich(self._bot.bot_instance, env.chat_id, error_text, opts)
            return

        # Provider switch notice (before result)
        notice = env.metadata.get("provider_switch_notice", "")
        if notice:
            await send_rich(
                self._bot.bot_instance,
                env.chat_id,
                f"**Provider Switch Detected**\n\n{notice}",
                opts,
            )

        # Result text (filled by bus injection)
        if env.result_text:
            await send_rich(self._bot.bot_instance, env.chat_id, env.result_text, opts)

    async def _deliver_task_result(self, env: Envelope) -> None:
        """Deliver task result notification + injected response."""
        opts = self._opts(env)
        name = env.metadata.get("name", env.metadata.get("task_id", "?"))

        # 1. Notification (skip "waiting" — question already shown)
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
            await send_rich(self._bot.bot_instance, env.chat_id, note, opts)

        # 2. Injected response (filled by bus injection for done/failed)
        if env.needs_injection and env.result_text:
            await send_rich(self._bot.bot_instance, env.chat_id, env.result_text, opts)

    async def _deliver_task_question(self, env: Envelope) -> None:
        """Deliver task question notification + injected agent response."""
        opts = self._opts(env)
        task_id = env.metadata.get("task_id", "?")

        # 1. Notification
        note = f"**Task `{task_id}` has a question:**\n{env.prompt}"
        await send_rich(self._bot.bot_instance, env.chat_id, note, opts)

        # 2. Agent response (filled by bus injection)
        if env.result_text:
            await send_rich(self._bot.bot_instance, env.chat_id, env.result_text, opts)

    async def _deliver_webhook_wake(self, env: Envelope) -> None:
        """Deliver webhook wake result."""
        if env.result_text:
            await send_rich(self._bot.bot_instance, env.chat_id, env.result_text, self._opts(env))

    async def _deliver_cron(self, env: Envelope) -> None:
        """Deliver cron result to a specific chat/topic (unicast).

        Falls back to the main agent when delivery fails.
        """
        from aiogram.exceptions import TelegramAPIError

        title = env.metadata.get("title", "?")
        clean_result = sanitize_cron_result_text(env.result_text)
        if env.result_text and not clean_result and env.status == "success":
            logger.debug(
                "Cron result only had transport confirmations; skipping unicast task=%s",
                title,
            )
            return
        text = (
            f"**TASK: {title}**\n\n{clean_result}"
            if clean_result
            else f"**TASK: {title}**\n\n_{env.status}_"
        )
        opts = SendRichOpts(
            allowed_roots=self._roots(),
            thread_id=env.topic_id,
        )
        try:
            delivered = await send_rich(self._bot.bot_instance, env.chat_id, text, opts)
        except TelegramAPIError as exc:
            logger.warning(
                "Cron '%s' delivery failed for chat %d, falling back to main agent",
                title,
                env.chat_id,
            )
            env.delivered = False
            env.delivery_error = type(exc).__name__
            target = f"Chat {env.chat_id}"
            if env.topic_id:
                target += f" / Topic {env.topic_id}"
            fallback_text = (
                f"**Cron delivery failed**\n\n"
                f"Task **{title}** could not be delivered to {target}.\n\n"
                f"---\n{clean_result or env.status}"
            )
            fallback_id = self._bot._config.allowed_user_ids[0]
            await send_rich(
                self._bot.bot_instance,
                fallback_id,
                fallback_text,
                SendRichOpts(allowed_roots=self._roots()),
            )
        else:
            if not delivered:
                # send_rich swallowed a network error (#160) — record the miss
                # so the cron observer can persist the result for resend.
                env.delivered = False
                env.delivery_error = "TelegramNetworkError"

    # -- Origin handlers (broadcast) ----------------------------------------

    async def _broadcast_cron(self, env: Envelope) -> None:
        title = env.metadata.get("title", "?")
        clean_result = sanitize_cron_result_text(env.result_text)
        if env.result_text and not clean_result and env.status == "success":
            logger.debug(
                "Cron result only had transport confirmations; skipping broadcast task=%s", title
            )
            return
        text = (
            f"**TASK: {title}**\n\n{clean_result}"
            if clean_result
            else f"**TASK: {title}**\n\n_{env.status}_"
        )
        await self._bot.broadcast(text, SendRichOpts(allowed_roots=self._roots()))

    async def _broadcast_webhook_cron(self, env: Envelope) -> None:
        title = env.metadata.get("hook_title", "?")
        if env.result_text:
            text = f"**WEBHOOK (CRON TASK): {title}**\n\n{env.result_text}"
        else:
            text = f"**WEBHOOK (CRON TASK): {title}**\n\n_{env.status}_"
        await self._bot.broadcast(text, SendRichOpts(allowed_roots=self._roots()))


# ---------------------------------------------------------------------------
# Handler dispatch tables
# ---------------------------------------------------------------------------

_Handler = Callable[[TelegramTransport, Envelope], Awaitable[None]]

_HANDLERS: dict[Origin, _Handler] = {
    Origin.BACKGROUND: TelegramTransport._deliver_background,
    Origin.CRON: TelegramTransport._deliver_cron,
    Origin.HEARTBEAT: TelegramTransport._deliver_heartbeat,
    Origin.INTERAGENT: TelegramTransport._deliver_interagent,
    Origin.TASK_RESULT: TelegramTransport._deliver_task_result,
    Origin.TASK_QUESTION: TelegramTransport._deliver_task_question,
    Origin.WEBHOOK_WAKE: TelegramTransport._deliver_webhook_wake,
}

_BROADCAST_HANDLERS: dict[Origin, _Handler] = {
    Origin.CRON: TelegramTransport._broadcast_cron,
    Origin.WEBHOOK_CRON: TelegramTransport._broadcast_webhook_cron,
}
