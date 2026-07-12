"""Send utilities: rich text with file references, file sending."""

from __future__ import annotations

import asyncio
import html as html_mod
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.types import FSInputFile, InlineKeyboardMarkup, ReplyParameters

from ductor_bot.files.tags import FILE_PATH_RE, extract_file_paths, guess_mime, path_from_file_tag
from ductor_bot.messenger.send_opts import BaseSendOpts
from ductor_bot.messenger.telegram.buttons import extract_buttons
from ductor_bot.messenger.telegram.formatting import (
    markdown_to_telegram_html,
    split_html_message,
)
from ductor_bot.security.paths import is_path_safe

if TYPE_CHECKING:
    from aiogram import Bot
    from aiogram.types import Message


@dataclass(slots=True)
class SendRichOpts(BaseSendOpts):
    """Optional parameters for :func:`send_rich`."""

    reply_to_message_id: int | None = None
    reply_markup: InlineKeyboardMarkup | None = None
    thread_id: int | None = None


logger = logging.getLogger(__name__)

_PHOTO_SUFFIXES = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"})
_VIDEO_SUFFIXES = frozenset({".mp4"})
_AUDIO_SUFFIXES = frozenset({".mp3", ".m4a"})


def _select_telegram_upload_mode(path: Path, mime: str) -> str:
    """Return best Telegram upload mode for this file.

    Non-matching or unsupported formats are sent as document.
    """
    suffix = path.suffix.lower()
    if mime.startswith("image/") and suffix in _PHOTO_SUFFIXES:
        return "photo"
    if mime.startswith("video/") and suffix in _VIDEO_SUFFIXES:
        return "video"
    if mime.startswith("audio/") and suffix in _AUDIO_SUFFIXES:
        return "audio"
    return "document"


async def _send_document(bot: Bot, chat_id: int, path: Path, thread_id: int | None) -> None:
    await bot.send_document(
        chat_id=chat_id,
        document=FSInputFile(path),
        message_thread_id=thread_id,
    )


async def _send_by_mode(
    bot: Bot,
    chat_id: int,
    path: Path,
    *,
    upload_mode: str,
    thread_id: int | None,
) -> None:
    if upload_mode == "document":
        await _send_document(bot, chat_id, path, thread_id)
        return

    input_file = FSInputFile(path)

    try:
        if upload_mode == "photo":
            await bot.send_photo(chat_id=chat_id, photo=input_file, message_thread_id=thread_id)
        elif upload_mode == "video":
            await bot.send_video(chat_id=chat_id, video=input_file, message_thread_id=thread_id)
        elif upload_mode == "audio":
            await bot.send_audio(chat_id=chat_id, audio=input_file, message_thread_id=thread_id)
        else:
            await _send_document(bot, chat_id, path, thread_id)
            return
    except TelegramBadRequest:
        logger.info(
            "%s upload rejected, retrying as document: %s",
            upload_mode.capitalize(),
            path.name,
        )
        await _send_document(bot, chat_id, path, thread_id)


async def send_files_from_text(
    bot: Bot,
    chat_id: int,
    text: str,
    *,
    allowed_roots: Sequence[Path] | None = None,
    thread_id: int | None = None,
) -> None:
    """Extract ``<file:/path>`` tags from *text* and send each file.

    Use after streaming, where text was already sent but file tags need
    separate handling.
    """
    for fp in extract_file_paths(text):
        await send_file(
            bot,
            chat_id,
            path_from_file_tag(fp),
            allowed_roots=allowed_roots,
            thread_id=thread_id,
        )


async def _send_text_chunks(
    bot: Bot,
    chat_id: int,
    clean_text: str,
    *,
    reply_to_message_id: int | None = None,
    thread_id: int | None = None,
) -> tuple[Message | None, bool]:
    """Send *clean_text* as HTML chunks, falling back to plain text on error.

    Returns ``(last_message, delivered)`` — *delivered* is ``False`` when a
    network error cut the send short (#160).
    """
    last_msg: Message | None = None
    html_text = markdown_to_telegram_html(clean_text)
    chunks = split_html_message(html_text)
    for i, chunk in enumerate(chunks):
        try:
            if reply_to_message_id and i == 0:
                last_msg = await bot.send_message(
                    chat_id=chat_id,
                    text=chunk,
                    parse_mode=ParseMode.HTML,
                    reply_parameters=ReplyParameters(
                        message_id=reply_to_message_id,
                        allow_sending_without_reply=True,
                    ),
                    message_thread_id=thread_id,
                )
            else:
                last_msg = await bot.send_message(
                    chat_id=chat_id,
                    text=chunk,
                    parse_mode=ParseMode.HTML,
                    message_thread_id=thread_id,
                )
        except TelegramNetworkError:
            logger.debug("Network error sending message (likely shutdown), skipping")
            return last_msg, False
        except TelegramBadRequest:
            logger.warning(
                "HTML send failed at chunk %d/%d, falling back to plain text", i, len(chunks)
            )
            # Only resend unsent chunks (i onwards) to avoid duplicating
            # content that was already delivered as HTML.
            remaining = "\n\n".join(chunks[i:])
            plain = html_mod.unescape(re.sub(r"<[^>]+>", "", remaining))
            for pc in split_html_message(plain):
                last_msg = await bot.send_message(
                    chat_id=chat_id,
                    text=pc,
                    parse_mode=None,
                    message_thread_id=thread_id,
                )
            break
    return last_msg, True


async def send_rich(
    bot: Bot,
    chat_id: int,
    text: str,
    opts: SendRichOpts | None = None,
) -> bool:
    """Parse <file:/path> tags, send text first, then files.

    When *opts.reply_markup* is provided it is used directly; otherwise buttons
    are extracted from ``[button:...]`` markers in the text.

    Returns ``True`` when the text was fully delivered, ``False`` when a
    network error cut it short (#160). File/button failures do not affect
    the return value.
    """
    o = opts or SendRichOpts()
    file_paths = FILE_PATH_RE.findall(text)
    clean_text = FILE_PATH_RE.sub("", text).strip()
    logger.debug("Sending rich text chars=%d files=%d", len(clean_text), len(file_paths))

    button_markup = o.reply_markup if o.reply_markup is not None else extract_buttons(clean_text)[1]
    last_msg: Message | None = None
    delivered = True

    if clean_text:
        last_msg, delivered = await _send_text_chunks(
            bot,
            chat_id,
            clean_text,
            reply_to_message_id=o.reply_to_message_id,
            thread_id=o.thread_id,
        )

    if button_markup is not None and last_msg is not None:
        try:
            await bot.edit_message_reply_markup(
                chat_id=chat_id,
                message_id=last_msg.message_id,
                reply_markup=button_markup,
            )
        except TelegramNetworkError:
            logger.debug("Network error attaching keyboard (likely shutdown)")
        except TelegramBadRequest:
            logger.warning("Failed to attach button keyboard in send_rich")

    for fp in file_paths:
        await send_file(
            bot,
            chat_id,
            path_from_file_tag(fp),
            allowed_roots=o.allowed_roots,
            thread_id=o.thread_id,
        )

    return delivered


async def send_file(
    bot: Bot,
    chat_id: int,
    path: Path,
    *,
    allowed_roots: Sequence[Path] | None = None,
    thread_id: int | None = None,
) -> None:
    """Send a local file with Telegram media/document routing."""
    if allowed_roots is not None and not is_path_safe(path, allowed_roots):
        logger.warning("File path blocked (outside allowed roots): %s", path)
        await bot.send_message(
            chat_id=chat_id,
            text=(
                f"Could not send <code>{path.name}</code> — "
                f"file is outside the allowed directory.\n\n"
                f'Fix: set <code>"file_access": "all"</code> in '
                f"<code>config.json</code>, then <b>/restart</b>."
            ),
            parse_mode="HTML",
            message_thread_id=thread_id,
        )
        return

    if not await asyncio.to_thread(path.exists):
        logger.warning("File not found, skipping: %s", path)
        await bot.send_message(
            chat_id=chat_id,
            text=f"[File not found: {path.name} (Tried host path: {path})]",
            parse_mode=None,
            message_thread_id=thread_id,
        )
        return

    try:
        mime = guess_mime(path)
        upload_mode = _select_telegram_upload_mode(path, mime)
        await _send_by_mode(
            bot,
            chat_id,
            path,
            upload_mode=upload_mode,
            thread_id=thread_id,
        )

        logger.info("Sent file: %s (%s)", path.name, mime)
    except TelegramNetworkError:
        logger.debug("Network error sending file (likely shutdown), skipping: %s", path)
    except OSError:
        logger.exception("Failed to send file: %s", path)
        await bot.send_message(
            chat_id=chat_id,
            text=f"[Failed to send: {path.name}]",
            parse_mode=None,
            message_thread_id=thread_id,
        )
    except TelegramBadRequest:
        logger.exception("Telegram rejected file upload: %s", path)
        await bot.send_message(
            chat_id=chat_id,
            text=f"[Failed to send: {path.name}]",
            parse_mode=None,
            message_thread_id=thread_id,
        )
