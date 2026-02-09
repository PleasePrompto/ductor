"""Message and command handler functions for the Telegram bot."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ductor_bot.bot.sender import send_rich
from ductor_bot.bot.typing import TypingContext

if TYPE_CHECKING:
    from aiogram import Bot
    from aiogram.types import Message

    from ductor_bot.orchestrator.core import Orchestrator

logger = logging.getLogger(__name__)


async def handle_abort(
    orchestrator: Orchestrator | None,
    bot: Bot,
    *,
    chat_id: int,
    message: Message,
) -> bool:
    """Kill active CLI processes and send feedback.

    Returns True if handled, False if orchestrator not ready.
    """
    if orchestrator is None:
        return False

    killed = await orchestrator.abort(chat_id)
    logger.info("Abort requested killed=%d", killed)
    if killed:
        provider = orchestrator.active_provider_name
        text = f"**{provider} has been terminated!** All queued messages will be discarded."
    else:
        text = "Nothing running right now."
    await send_rich(bot, chat_id, text, reply_to=message)
    return True


async def handle_command(orchestrator: Orchestrator, bot: Bot, message: Message) -> None:
    """Route an orchestrator command (e.g. /status, /model)."""
    if not message.text:
        return
    chat_id = message.chat.id
    logger.info("Command dispatched cmd=%s", message.text.strip()[:40])
    async with TypingContext(bot, chat_id):
        result = await orchestrator.handle_message(chat_id, message.text.strip())
    await send_rich(bot, chat_id, result.text, reply_to=message, reply_markup=result.reply_markup)


async def handle_new_session(orchestrator: Orchestrator, bot: Bot, message: Message) -> None:
    """Handle /new: reset session."""
    logger.info("Session reset requested")
    chat_id = message.chat.id
    async with TypingContext(bot, chat_id):
        await orchestrator.reset_session(chat_id)
    await send_rich(
        bot, chat_id, "**Fresh session.** Everything cleared -- ready to go.", reply_to=message
    )


def strip_mention(text: str, bot_username: str | None) -> str:
    """Remove @botusername from message text (case-insensitive)."""
    if not bot_username:
        return text
    tag = f"@{bot_username}"
    lower = text.lower()
    if tag in lower:
        idx = lower.index(tag)
        stripped = (text[:idx] + text[idx + len(tag) :]).strip()
        return stripped or text
    return text
