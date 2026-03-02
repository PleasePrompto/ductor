"""Shared message execution flows for TelegramBot (streaming and non-streaming)."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ductor_bot.bot.sender import SendRichOpts, send_files_from_text, send_rich
from ductor_bot.bot.streaming import create_stream_editor
from ductor_bot.bot.typing import TypingContext
from ductor_bot.cli.coalescer import CoalesceConfig, StreamCoalescer

if TYPE_CHECKING:
    from aiogram import Bot
    from aiogram.types import Message

    from ductor_bot.config import StreamingConfig
    from ductor_bot.orchestrator.core import Orchestrator

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class NonStreamingDispatch:
    """Input payload for one non-streaming message turn."""

    bot: Bot
    orchestrator: Orchestrator
    chat_id: int
    text: str
    allowed_roots: list[Path] | None
    reply_to: Message | None = None
    thread_id: int | None = None


@dataclass(slots=True)
class StreamingDispatch:
    """Input payload for one streaming message turn."""

    bot: Bot
    orchestrator: Orchestrator
    message: Message
    chat_id: int
    text: str
    streaming_cfg: StreamingConfig
    allowed_roots: list[Path] | None
    thread_id: int | None = None


async def run_non_streaming_message(
    dispatch: NonStreamingDispatch,
) -> str:
    """Execute one non-streaming turn and deliver the result to Telegram."""
    async with TypingContext(dispatch.bot, dispatch.chat_id, thread_id=dispatch.thread_id):
        result = await dispatch.orchestrator.handle_message(dispatch.chat_id, dispatch.text)

    reply_id = dispatch.reply_to.message_id if dispatch.reply_to else None
    await send_rich(
        dispatch.bot,
        dispatch.chat_id,
        result.text,
        SendRichOpts(
            reply_to_message_id=reply_id,
            allowed_roots=dispatch.allowed_roots,
            thread_id=dispatch.thread_id,
        ),
    )
    return result.text


async def run_streaming_message(
    dispatch: StreamingDispatch,
) -> str:
    """Execute one streaming turn and deliver text/files to Telegram."""
    logger.info("Streaming flow started")

    editor = create_stream_editor(
        dispatch.bot,
        dispatch.chat_id,
        reply_to=dispatch.message,
        cfg=dispatch.streaming_cfg,
        thread_id=dispatch.thread_id,
    )
    coalescer = StreamCoalescer(
        config=CoalesceConfig(
            min_chars=dispatch.streaming_cfg.min_chars,
            max_chars=dispatch.streaming_cfg.max_chars,
            idle_ms=dispatch.streaming_cfg.idle_ms,
            sentence_break=dispatch.streaming_cfg.sentence_break,
        ),
        on_flush=editor.append_text,
    )

    async def on_text(delta: str) -> None:
        await coalescer.feed(delta)

    async def on_tool(tool_name: str) -> None:
        await coalescer.flush(force=True)
        await editor.append_tool(tool_name)

    async def on_system(status: str | None) -> None:
        system_map: dict[str, str] = {
            "thinking": "THINKING",
            "compacting": "COMPACTING",
            "recovering": "Please wait, recovering...",
            "timeout_warn_60": "Still working. About 60 seconds left before timeout.",
            "timeout_warn_10": "Still working. About 10 seconds left before timeout.",
            "timeout_extended": "Long-running task detected. Timeout window extended automatically.",
        }
        label = system_map.get(status or "")
        if label is None:
            return
        await coalescer.flush(force=True)
        await editor.append_system(label)

    async with TypingContext(dispatch.bot, dispatch.chat_id, thread_id=dispatch.thread_id):
        result = await dispatch.orchestrator.handle_message_streaming(
            dispatch.chat_id,
            dispatch.text,
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

    if result.stream_fallback or not editor.has_content:
        await send_rich(
            dispatch.bot,
            dispatch.chat_id,
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
            dispatch.chat_id,
            result.text,
            allowed_roots=dispatch.allowed_roots,
            thread_id=dispatch.thread_id,
        )

    return result.text
