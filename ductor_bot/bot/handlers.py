"""Message and command handler functions for the Telegram bot."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from ductor_bot.bot.callbacks import button_grid_to_markup
from ductor_bot.bot.sender import SendRichOpts, send_rich
from ductor_bot.bot.topic import TopicNameCache, get_session_key, get_thread_id
from ductor_bot.bot.typing import TypingContext
from ductor_bot.session.key import SessionKey
from ductor_bot.text.response_format import new_session_text, stop_text

if TYPE_CHECKING:
    from aiogram import Bot
    from aiogram.types import Message

    from ductor_bot.orchestrator.core import Orchestrator

logger = logging.getLogger(__name__)


async def handle_interrupt(
    orchestrator: Orchestrator | None,
    bot: Bot,
    *,
    chat_id: int,
    message: Message,
) -> bool:
    """Send SIGINT to active CLI processes (soft interrupt, like pressing ESC).

    Returns True if handled, False if orchestrator not ready.
    """
    if orchestrator is None:
        return False

    interrupted = orchestrator.interrupt(chat_id)
    logger.info("Interrupt requested interrupted=%d", interrupted)
    msg = f"Interrupted {interrupted} process(es)." if interrupted else "No active processes."
    await send_rich(
        bot,
        chat_id,
        msg,
        SendRichOpts(reply_to_message_id=message.message_id, thread_id=get_thread_id(message)),
    )
    return True


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
    text = stop_text(bool(killed), orchestrator.active_provider_name)
    await send_rich(
        bot,
        chat_id,
        text,
        SendRichOpts(reply_to_message_id=message.message_id, thread_id=get_thread_id(message)),
    )
    return True


async def handle_abort_all(
    orchestrator: Orchestrator | None,
    bot: Bot,
    *,
    chat_id: int,
    message: Message,
    abort_all_callback: Callable[[], Awaitable[int]] | None = None,
) -> bool:
    """Kill active CLI processes on THIS agent AND all other agents.

    Returns True if handled, False if orchestrator not ready.
    """
    if orchestrator is None:
        return False

    # Kill local processes first
    killed = await orchestrator.abort(chat_id)

    # Kill processes on all other agents via the supervisor callback
    if abort_all_callback is not None:
        killed += await abort_all_callback()

    logger.info("Abort ALL requested killed=%d", killed)
    if killed:
        text = f"Stopped {killed} process(es) across all agents."
    else:
        text = "No active processes found on any agent."
    await send_rich(
        bot,
        chat_id,
        text,
        SendRichOpts(reply_to_message_id=message.message_id, thread_id=get_thread_id(message)),
    )
    return True


async def handle_command(orchestrator: Orchestrator, bot: Bot, message: Message) -> None:
    """Route an orchestrator command (e.g. /status, /model)."""
    if not message.text:
        return
    key = get_session_key(message)
    chat_id = key.chat_id
    thread_id = get_thread_id(message)
    logger.info("Command dispatched cmd=%s", message.text.strip()[:40])
    async with TypingContext(bot, chat_id, thread_id=thread_id):
        result = await orchestrator.handle_message(key, message.text.strip())
    markup = button_grid_to_markup(result.buttons) if result.buttons else None
    await send_rich(
        bot,
        chat_id,
        result.text,
        SendRichOpts(
            reply_to_message_id=message.message_id,
            reply_markup=markup,
            thread_id=thread_id,
        ),
    )


async def handle_new_session(
    orchestrator: Orchestrator,
    bot: Bot,
    message: Message,
    topic_names: TopicNameCache | None = None,
) -> None:
    """Handle ``/new`` and ``/new @topicname``.

    Plain ``/new`` resets the current session (the topic session if sent
    inside a topic, the main session otherwise).

    ``/new @topicname`` resets the named topic's session without entering
    the topic.  The topic is resolved via ``TopicNameCache``.
    """
    logger.info("Session reset requested")
    chat_id = message.chat.id
    thread_id = get_thread_id(message)
    text = (message.text or "").strip()

    # Parse optional @topicname argument.
    parts = text.split(None, 1)
    topic_arg = parts[1].strip() if len(parts) > 1 else ""

    if topic_arg.startswith("@") and topic_names is not None:
        topic_name = topic_arg[1:]
        topic_id = topic_names.find_by_name(chat_id, topic_name)
        if topic_id is None:
            await send_rich(
                bot,
                chat_id,
                f'Topic "{topic_name}" not found.',
                SendRichOpts(reply_to_message_id=message.message_id, thread_id=thread_id),
            )
            return
        key = SessionKey(chat_id=chat_id, topic_id=topic_id)
        resolved_name = topic_names.resolve(chat_id, topic_id)
        async with TypingContext(bot, chat_id, thread_id=thread_id):
            provider = await orchestrator.reset_active_provider_session(key)
        await send_rich(
            bot,
            chat_id,
            f"New session for **{resolved_name}** ({provider}).",
            SendRichOpts(reply_to_message_id=message.message_id, thread_id=thread_id),
        )
        return

    key = get_session_key(message)
    async with TypingContext(bot, chat_id, thread_id=thread_id):
        provider = await orchestrator.reset_active_provider_session(key)
    await send_rich(
        bot,
        chat_id,
        new_session_text(provider),
        SendRichOpts(reply_to_message_id=message.message_id, thread_id=thread_id),
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
