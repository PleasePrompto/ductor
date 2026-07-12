"""Shared message execution flows for TelegramBot (streaming and non-streaming)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ductor_bot.cli.coalescer import CoalesceConfig, StreamCoalescer
from ductor_bot.messenger.telegram.sender import (
    SendRichOpts,
    send_files_from_text,
    send_rich,
)
from ductor_bot.messenger.telegram.streaming import StreamEditor, create_stream_editor
from ductor_bot.messenger.telegram.typing import TypingContext
from ductor_bot.orchestrator.registry import OrchestratorResult
from ductor_bot.session.key import SessionKey

if TYPE_CHECKING:
    from aiogram import Bot
    from aiogram.types import Message

    from ductor_bot.config import SceneConfig, StreamingConfig
    from ductor_bot.orchestrator.core import Orchestrator

logger = logging.getLogger(__name__)


# -- Emoji reaction status (#63) -------------------------------------------------
#
# Opt-in via ``scene.status_reaction``. Constants live at module level so tests
# can assert against them without importing bot state. All chosen emoji are in
# Telegram's bot-reaction whitelist.

_REACTION_THINKING = "\U0001f914"  # 🤔
_REACTION_SYSTEM = "\U0001f4af"  # 💯
_REACTION_DEFAULT = _REACTION_THINKING

# Tool-name prefix (lowercase) -> emoji. First matching prefix wins.
_REACTION_TOOL_MAP: tuple[tuple[tuple[str, ...], str], ...] = (
    (("read", "grep", "glob", "ls"), "\U0001f440"),  # 👀
    (("edit", "write", "multiedit", "str_replace"), "✍️"),  # ✍️
    (("bash", "shell", "run", "exec"), "\U0001f468‍\U0001f4bb"),  # 👨‍💻
)


class ReactionTracker:
    """Stage-aware wrapper around ``bot.set_message_reaction``.

    Owns an in-memory "current emoji" and dedups consecutive identical
    stages so we never spam the Telegram API. Every call is best-effort:
    exceptions from the underlying bot are swallowed at debug level.

    When ``enabled`` is False every method is a no-op, so the streaming
    flow can unconditionally call the tracker regardless of config.
    """

    __slots__ = ("_bot", "_chat_id", "_current", "_enabled", "_message_id")

    def __init__(
        self,
        bot: Bot,
        chat_id: int,
        message_id: int,
        *,
        enabled: bool,
    ) -> None:
        self._bot = bot
        self._chat_id = chat_id
        self._message_id = message_id
        self._enabled = enabled
        self._current: str | None = None

    async def set_thinking(self) -> None:
        """Mark the turn as "thinking" (default idle/processing state)."""
        await self._apply(_REACTION_THINKING)

    async def set_system(self) -> None:
        """Mark a system event (compacting, timeout warning, ...)."""
        await self._apply(_REACTION_SYSTEM)

    async def set_tool(self, tool_name: str) -> None:
        """Map ``tool_name`` to an emoji via ``_REACTION_TOOL_MAP``.

        Unknown tool names fall back to ``_REACTION_DEFAULT`` (🤔) — never
        no-op. Callers want *some* visible stage change.
        """
        lower = (tool_name or "").lower()
        emoji = _REACTION_DEFAULT
        for prefixes, candidate in _REACTION_TOOL_MAP:
            if any(lower.startswith(p) for p in prefixes):
                emoji = candidate
                break
        await self._apply(emoji)

    async def clear(self) -> None:
        """Remove any reaction set by this tracker."""
        await self._apply(None)

    async def _apply(self, emoji: str | None) -> None:
        if not self._enabled:
            return
        if emoji == self._current:
            return
        self._current = emoji
        try:
            from aiogram.types import (
                ReactionTypeCustomEmoji,
                ReactionTypeEmoji,
                ReactionTypePaid,
            )

            payload: list[ReactionTypeEmoji | ReactionTypeCustomEmoji | ReactionTypePaid] = (
                [ReactionTypeEmoji(emoji=emoji)] if emoji is not None else []
            )
            await self._bot.set_message_reaction(
                chat_id=self._chat_id,
                message_id=self._message_id,
                reaction=payload,
            )
        except Exception:
            logger.debug("ReactionTracker: set_message_reaction failed", exc_info=True)


def _build_footer(result: OrchestratorResult, scene: SceneConfig | None) -> str:
    """Build technical footer string if enabled and metadata is available."""
    if scene is None or not scene.technical_footer or not result.model_name:
        return ""
    from ductor_bot.text.response_format import format_technical_footer

    return format_technical_footer(
        result.model_name,
        result.total_tokens,
        result.input_tokens,
        result.cost_usd,
        result.duration_ms,
    )


def _status_reaction_enabled(scene: SceneConfig | None) -> bool:
    return bool(scene is not None and scene.status_reaction)


def _format_reasoning_chunk(text: str) -> str:
    normalized = text.strip()
    if not normalized:
        return ""
    return f"**Thinking**\n\n{normalized}"


@dataclass(slots=True)
class NonStreamingDispatch:
    """Input payload for one non-streaming message turn.

    ``message`` is the user's current trigger (text message, callback query
    message, or ``None`` for inline commands). It anchors the reaction
    tracker so reactions always land on the message that initiated the
    turn — mirroring ``StreamingDispatch.message`` (MED #10).

    ``reply_to`` is the optional reply-to destination for the outgoing
    Telegram message. Usually the same as ``message`` but can differ.
    """

    bot: Bot
    orchestrator: Orchestrator
    key: SessionKey
    text: str
    allowed_roots: list[Path] | None
    message: Message | None = None
    reply_to: Message | None = None
    thread_id: int | None = None
    scene_config: SceneConfig | None = None


@dataclass(slots=True)
class StreamingDispatch:
    """Input payload for one streaming message turn."""

    bot: Bot
    orchestrator: Orchestrator
    message: Message
    key: SessionKey
    text: str
    streaming_cfg: StreamingConfig
    allowed_roots: list[Path] | None
    thread_id: int | None = None
    scene_config: SceneConfig | None = None


async def run_non_streaming_message(
    dispatch: NonStreamingDispatch,
) -> str:
    """Execute one non-streaming turn and deliver the result to Telegram."""
    # MED #10: anchor the reaction tracker on the user's current trigger
    # message (``dispatch.message``), mirroring the streaming path. Using
    # ``reply_to`` instead risked landing reactions on a prior bot message
    # when a callback query repurposed ``reply_to`` for the outgoing reply.
    reaction_target = dispatch.message
    tracker = ReactionTracker(
        dispatch.bot,
        dispatch.key.chat_id,
        reaction_target.message_id if reaction_target is not None else 0,
        enabled=_status_reaction_enabled(dispatch.scene_config) and reaction_target is not None,
    )
    try:
        await tracker.set_thinking()
        async with TypingContext(dispatch.bot, dispatch.key.chat_id, thread_id=dispatch.thread_id):
            result = await dispatch.orchestrator.handle_message(dispatch.key, dispatch.text)

        footer = _build_footer(result, dispatch.scene_config)
        result.text += footer
        reply_id = dispatch.reply_to.message_id if dispatch.reply_to else None
        await send_rich(
            dispatch.bot,
            dispatch.key.chat_id,
            result.text,
            SendRichOpts(
                reply_to_message_id=reply_id,
                allowed_roots=dispatch.allowed_roots,
                thread_id=dispatch.thread_id,
            ),
        )
        return result.text
    finally:
        await tracker.clear()


async def run_streaming_message(  # noqa: C901, PLR0915
    dispatch: StreamingDispatch,
) -> str:
    """Execute one streaming turn and deliver text/files to Telegram."""
    logger.info("Streaming flow started")

    tracker = ReactionTracker(
        dispatch.bot,
        dispatch.key.chat_id,
        dispatch.message.message_id,
        enabled=_status_reaction_enabled(dispatch.scene_config),
    )

    editor = create_stream_editor(
        dispatch.bot,
        dispatch.key.chat_id,
        reply_to=dispatch.message,
        cfg=dispatch.streaming_cfg,
        thread_id=dispatch.thread_id,
    )
    streamed_text_sent = False

    async def _flush_text_chunk(chunk: str) -> None:
        nonlocal streamed_text_sent
        if chunk.strip():
            streamed_text_sent = True
        await editor.append_text(chunk)

    coalescer = StreamCoalescer(
        config=CoalesceConfig(
            min_chars=dispatch.streaming_cfg.min_chars,
            max_chars=dispatch.streaming_cfg.max_chars,
            idle_ms=dispatch.streaming_cfg.idle_ms,
            sentence_break=dispatch.streaming_cfg.sentence_break,
        ),
        on_flush=_flush_text_chunk,
    )

    reasoning_coalescer: StreamCoalescer | None = None
    if dispatch.streaming_cfg.show_reasoning_stream:
        reasoning_editor = StreamEditor(
            dispatch.bot,
            dispatch.key.chat_id,
            thread_id=dispatch.thread_id,
        )

        async def _flush_reasoning_chunk(chunk: str) -> None:
            formatted = _format_reasoning_chunk(chunk)
            if not formatted:
                return
            await reasoning_editor.append_text(formatted)

        reasoning_coalescer = StreamCoalescer(
            config=CoalesceConfig(
                min_chars=dispatch.streaming_cfg.min_chars,
                max_chars=dispatch.streaming_cfg.max_chars,
                idle_ms=dispatch.streaming_cfg.idle_ms,
                sentence_break=dispatch.streaming_cfg.sentence_break,
            ),
            on_flush=_flush_reasoning_chunk,
        )

    thinking_indicator_sent = False

    async def on_text(delta: str) -> None:
        await coalescer.feed(delta)

    async def on_tool(tool: object) -> None:
        tool_name = str(getattr(tool, "tool_name", tool))
        await tracker.set_tool(tool_name)
        await coalescer.flush(force=True)
        if reasoning_coalescer is not None:
            await reasoning_coalescer.flush(force=True)
        if dispatch.streaming_cfg.show_tool_progress:
            await editor.append_tool(tool_name)

    async def on_system(status: str | None) -> None:
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
        if status == "thinking":
            if dispatch.streaming_cfg.show_reasoning_stream:
                return
            if not dispatch.streaming_cfg.show_thinking_indicator:
                return
        await tracker.set_system()
        await coalescer.flush(force=True)
        if reasoning_coalescer is not None:
            await reasoning_coalescer.flush(force=True)
        await editor.append_system(label)

    async def on_reasoning(delta: str) -> None:
        nonlocal thinking_indicator_sent
        if not delta.strip():
            return
        await tracker.set_thinking()
        await coalescer.flush(force=True)
        if reasoning_coalescer is not None:
            await reasoning_coalescer.feed(delta)
            return
        if dispatch.streaming_cfg.show_thinking_indicator and not thinking_indicator_sent:
            thinking_indicator_sent = True
            await on_system("thinking")

    try:
        await tracker.set_thinking()
        async with TypingContext(dispatch.bot, dispatch.key.chat_id, thread_id=dispatch.thread_id):
            result = await dispatch.orchestrator.handle_message_streaming(
                dispatch.key,
                dispatch.text,
                on_text_delta=on_text,
                on_tool_activity=on_tool,
                on_system_status=on_system,
                on_reasoning_delta=on_reasoning,
            )

        await coalescer.flush(force=True)
        coalescer.stop()
        if reasoning_coalescer is not None:
            await reasoning_coalescer.flush(force=True)
            reasoning_coalescer.stop()
        footer = _build_footer(result, dispatch.scene_config)
        if footer:
            await editor.append_text(footer)
            result.text += footer
        await editor.finalize(result.text)

        logger.info(
            "Streaming flow completed fallback=%s content=%s",
            result.stream_fallback,
            editor.has_content,
        )

        if result.stream_fallback or not streamed_text_sent or not editor.has_content:
            await send_rich(
                dispatch.bot,
                dispatch.key.chat_id,
                result.text,
                SendRichOpts(
                    reply_to_message_id=dispatch.message.message_id,
                    allowed_roots=dispatch.allowed_roots,
                    thread_id=dispatch.thread_id,
                ),
            )
        else:
            await send_files_from_text(
                dispatch.bot,
                dispatch.key.chat_id,
                result.text,
                allowed_roots=dispatch.allowed_roots,
                thread_id=dispatch.thread_id,
            )

        return result.text
    finally:
        await tracker.clear()
