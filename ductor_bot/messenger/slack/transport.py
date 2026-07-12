"""Slack delivery adapter for the MessageBus."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from ductor_bot.bus.cron_sanitize import sanitize_cron_result_text
from ductor_bot.bus.envelope import Envelope, Origin
from ductor_bot.messenger.slack.sender import SlackSendOpts
from ductor_bot.messenger.slack.sender import send_rich as slack_send_rich
from ductor_bot.text.response_format import SEP, fmt

if TYPE_CHECKING:
    from ductor_bot.messenger.slack.bot import SlackBot

logger = logging.getLogger(__name__)


class SlackTransport:
    """Implements the transport adapter protocol for Slack delivery."""

    def __init__(self, bot: SlackBot) -> None:
        self._bot = bot

    @property
    def transport_name(self) -> str:
        return "sl"

    async def deliver(self, envelope: Envelope) -> None:
        handler = _HANDLERS.get(envelope.origin)
        if handler is None:
            logger.warning("No handler for origin=%s", envelope.origin.value)
            return
        await handler(self, envelope)

    async def deliver_broadcast(self, envelope: Envelope) -> None:
        handler = _BROADCAST_HANDLERS.get(envelope.origin)
        if handler is None:
            logger.warning("No broadcast handler for origin=%s", envelope.origin.value)
            return
        await handler(self, envelope)

    def _resolve_target(self, env: Envelope) -> tuple[str | None, str | None]:
        channel_id = self._bot.id_map.int_to_channel(env.chat_id)
        if channel_id is None:
            return None, None
        if env.topic_id is None:
            return channel_id, None
        thread = self._bot.id_map.int_to_thread(env.topic_id)
        if thread is None:
            return channel_id, None
        thread_channel, thread_ts = thread
        if thread_channel != channel_id:
            logger.warning(
                "Slack topic mapping mismatch for chat_id=%s topic_id=%s", env.chat_id, env.topic_id
            )
            return channel_id, None
        return channel_id, thread_ts

    def _opts(self, env: Envelope) -> SlackSendOpts:
        channel_id, thread_ts = self._resolve_target(env)
        del channel_id
        orch = self._bot.orchestrator
        roots = self._bot.file_roots(orch.paths) if orch else None
        return SlackSendOpts(allowed_roots=roots, thread_ts=thread_ts)

    async def _deliver_background(self, env: Envelope) -> None:
        channel_id, _thread_ts = self._resolve_target(env)
        if not channel_id:
            return
        elapsed = f"{env.elapsed_seconds:.0f}s"
        if env.session_name:
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
        await slack_send_rich(self._bot.client, channel_id, text, self._opts(env))

    async def _deliver_heartbeat(self, env: Envelope) -> None:
        channel_id, _thread_ts = self._resolve_target(env)
        if channel_id and env.result_text:
            await slack_send_rich(self._bot.client, channel_id, env.result_text, self._opts(env))

    async def _deliver_interagent(self, env: Envelope) -> None:
        channel_id, _thread_ts = self._resolve_target(env)
        if not channel_id:
            return
        if env.is_error:
            session_info = f"\nSession: `{env.session_name}`" if env.session_name else ""
            text = (
                f"**Inter-Agent Request Failed**\n\n"
                f"Agent: `{env.metadata.get('recipient', '?')}`{session_info}\n"
                f"Error: {env.metadata.get('error', 'unknown')}\n"
                f"Request: _{env.prompt_preview}_"
            )
            await slack_send_rich(self._bot.client, channel_id, text, self._opts(env))
            return

        notice = env.metadata.get("provider_switch_notice", "")
        if notice:
            await slack_send_rich(
                self._bot.client,
                channel_id,
                f"**Provider Switch Detected**\n\n{notice}",
                self._opts(env),
            )
        if env.result_text:
            await slack_send_rich(self._bot.client, channel_id, env.result_text, self._opts(env))

    async def _deliver_task_result(self, env: Envelope) -> None:
        channel_id, _thread_ts = self._resolve_target(env)
        if not channel_id:
            return
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
            await slack_send_rich(self._bot.client, channel_id, note, self._opts(env))
        if env.needs_injection and env.result_text:
            await slack_send_rich(self._bot.client, channel_id, env.result_text, self._opts(env))

    async def _deliver_task_question(self, env: Envelope) -> None:
        channel_id, _thread_ts = self._resolve_target(env)
        if not channel_id:
            return
        task_id = env.metadata.get("task_id", "?")
        note = f"**Task `{task_id}` has a question:**\n{env.prompt}"
        await slack_send_rich(self._bot.client, channel_id, note, self._opts(env))
        if env.result_text:
            await slack_send_rich(self._bot.client, channel_id, env.result_text, self._opts(env))

    async def _deliver_webhook_wake(self, env: Envelope) -> None:
        channel_id, _thread_ts = self._resolve_target(env)
        if channel_id and env.result_text:
            await slack_send_rich(self._bot.client, channel_id, env.result_text, self._opts(env))

    async def _deliver_cron(self, env: Envelope) -> None:
        channel_id, _thread_ts = self._resolve_target(env)
        if not channel_id:
            logger.warning(
                "Slack cron unicast: cannot resolve chat_id=%d, falling back to broadcast",
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
        await slack_send_rich(self._bot.client, channel_id, text, self._opts(env))

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
        await self._bot.broadcast(text)

    async def _broadcast_webhook_cron(self, env: Envelope) -> None:
        title = env.metadata.get("hook_title", "?")
        text = (
            f"**WEBHOOK (CRON TASK): {title}**\n\n{env.result_text}"
            if env.result_text
            else f"**WEBHOOK (CRON TASK): {title}**\n\n_{env.status}_"
        )
        await self._bot.broadcast(text)


_Handler = Callable[[SlackTransport, Envelope], Awaitable[None]]

_HANDLERS: dict[Origin, _Handler] = {
    Origin.BACKGROUND: SlackTransport._deliver_background,
    Origin.CRON: SlackTransport._deliver_cron,
    Origin.HEARTBEAT: SlackTransport._deliver_heartbeat,
    Origin.INTERAGENT: SlackTransport._deliver_interagent,
    Origin.TASK_RESULT: SlackTransport._deliver_task_result,
    Origin.TASK_QUESTION: SlackTransport._deliver_task_question,
    Origin.WEBHOOK_WAKE: SlackTransport._deliver_webhook_wake,
}

_BROADCAST_HANDLERS: dict[Origin, _Handler] = {
    Origin.CRON: SlackTransport._broadcast_cron,
    Origin.WEBHOOK_CRON: SlackTransport._broadcast_webhook_cron,
}
