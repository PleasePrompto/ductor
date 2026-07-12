"""Message and command handler functions for the Telegram bot."""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from ductor_bot.i18n import t
from ductor_bot.messenger.telegram.callbacks import button_grid_to_markup
from ductor_bot.messenger.telegram.sender import SendRichOpts, send_rich
from ductor_bot.messenger.telegram.topic import (
    TopicNameCache,
    get_session_key,
    get_thread_id,
)
from ductor_bot.messenger.telegram.typing import TypingContext
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
    msg = t("interrupt.done", count=interrupted) if interrupted else t("interrupt.nothing")
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
    """Kill active CLI processes in the current topic and send feedback.

    Returns True if handled, False if orchestrator not ready.
    """
    if orchestrator is None:
        return False

    thread_id = get_thread_id(message)
    killed = await orchestrator.abort(chat_id, topic_id=thread_id)
    logger.info("Abort requested chat=%d topic=%s killed=%d", chat_id, thread_id, killed)
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

    # Kill all local processes (across all chats/transports)
    killed = await orchestrator.abort_all()

    # Kill processes on all other agents via the supervisor callback
    if abort_all_callback is not None:
        killed += await abort_all_callback()

    logger.info("Abort ALL requested killed=%d", killed)
    text = t("abort_all.done", count=killed) if killed else t("abort_all.nothing")
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
                t("new.topic_not_found", name=topic_name),
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
            t("new.topic_reset", name=resolved_name, provider=provider),
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


def build_reply_prompt(message: Message, user_text: str) -> str:
    """Prefix *user_text* with the cited message of a Telegram reply (#135).

    When the user replies to a message, the quoted text is prepended with
    explicit labels so the agent can tell the citation from the new message::

        The user is replying to this quoted message:
        > <quoted text>

        The user's message:
        <user_text>

    Prefers the user-selected quote fragment (Bot API 7.0+) over the full
    replied-to body. Returns *user_text* unchanged when the message is not a
    reply or the cited message carries no text — e.g. forum-topic service
    messages or media-only replies without a caption.
    """
    cited = _cited_reply_text(message)
    if cited is None:
        return user_text
    quoted = "\n".join(f"> {line}" for line in cited.splitlines())
    return (
        f"The user is replying to this quoted message:\n{quoted}\n\n"
        f"The user's message:\n{user_text}"
    )


def prepend_reply_to_media(message: Message, media_prompt: str) -> str:
    """Prefix a media prompt with the cited reply message (#135).

    For non-text replies (voice/photo/video/...), the user's actual reply is the
    attachment, so the quoted text plus an explicit attachment-type note is
    prepended ahead of the ``[INCOMING FILE]`` block — e.g. a voicemail reply to
    a cron brief. Returns *media_prompt* unchanged when the message is not a
    reply or the cited message has no text (forum-topic service messages).
    """
    cited = _cited_reply_text(message)
    if cited is None:
        return media_prompt
    quoted = "\n".join(f"> {line}" for line in cited.splitlines())
    label = _reply_attachment_label(message)
    return (
        f"The user is replying to this quoted message:\n{quoted}\n\n"
        f"Their reply is {label} (the attached file below).\n\n{media_prompt}"
    )


def _cited_reply_text(message: Message) -> str | None:
    """Return the cited text of a Telegram reply, or ``None`` when absent.

    Prefers the user-selected quote fragment over the full replied-to body
    (text or caption); returns ``None`` for non-replies and text-less cited
    messages (forum-topic service messages, media-only replies).
    """
    quote = message.quote
    cited: str | None
    if quote is not None and quote.text:
        cited = quote.text
    else:
        replied = message.reply_to_message
        cited = (replied.text or replied.caption) if replied is not None else None
    if not cited or not cited.strip():
        return None
    return cited.strip()


def _reply_attachment_label(message: Message) -> str:
    """Human-readable label for the attachment type, matching ``_resolve_media``."""
    labels = (
        (message.photo, "an image"),
        (message.document, "a document"),
        (message.voice, "a voice message"),
        (message.audio, "an audio file"),
        (message.video, "a video"),
        (message.video_note, "a video note"),
        (message.sticker, "a sticker"),
    )
    return next((label for value, label in labels if value), "a file")
