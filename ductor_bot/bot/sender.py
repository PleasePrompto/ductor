"""Send utilities: rich text with file references, file sending."""

from __future__ import annotations

import logging
import mimetypes
import re
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING

from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramNetworkError
from aiogram.types import FSInputFile, InlineKeyboardMarkup

from ductor_bot.bot.buttons import extract_buttons
from ductor_bot.bot.formatting import markdown_to_telegram_html, split_html_message
from ductor_bot.security.paths import is_path_safe

if TYPE_CHECKING:
    from aiogram import Bot
    from aiogram.types import Message

logger = logging.getLogger(__name__)

_IMAGE_EXTENSIONS = frozenset({".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"})
_FILE_PATH_RE = re.compile(r"<file:([^>]+)>")


def extract_file_paths(text: str) -> list[str]:
    """Return all <file:/path> references from *text*."""
    return _FILE_PATH_RE.findall(text)


async def send_files_from_text(
    bot: Bot,
    chat_id: int,
    text: str,
    *,
    allowed_roots: Sequence[Path] | None = None,
) -> None:
    """Extract ``<file:/path>`` tags from *text* and send each file.

    Use after streaming, where text was already sent but file tags need
    separate handling.
    """
    for fp in extract_file_paths(text):
        await send_file(bot, chat_id, Path(fp), allowed_roots=allowed_roots)


async def _send_text_chunks(
    bot: Bot,
    chat_id: int,
    clean_text: str,
    *,
    reply_to: Message | None = None,
) -> Message | None:
    """Send *clean_text* as HTML chunks, falling back to plain text on error."""
    last_msg: Message | None = None
    html_text = markdown_to_telegram_html(clean_text)
    chunks = split_html_message(html_text)
    for i, chunk in enumerate(chunks):
        try:
            if reply_to and i == 0:
                last_msg = await reply_to.answer(chunk, parse_mode=ParseMode.HTML)
            else:
                last_msg = await bot.send_message(
                    chat_id=chat_id, text=chunk, parse_mode=ParseMode.HTML
                )
        except TelegramNetworkError:
            logger.debug("Network error sending message (likely shutdown), skipping")
            return last_msg
        except TelegramBadRequest:
            logger.warning("HTML send failed, falling back to plain text")
            plain_chunks = split_html_message(clean_text)
            for j, pc in enumerate(plain_chunks):
                if reply_to and i == 0 and j == 0:
                    last_msg = await reply_to.answer(pc, parse_mode=None)
                else:
                    last_msg = await bot.send_message(chat_id=chat_id, text=pc, parse_mode=None)
            break
    return last_msg


async def send_rich(  # noqa: PLR0913
    bot: Bot,
    chat_id: int,
    text: str,
    *,
    reply_to: Message | None = None,
    allowed_roots: Sequence[Path] | None = None,
    reply_markup: InlineKeyboardMarkup | None = None,
) -> None:
    """Parse <file:/path> tags, send text first, then files.

    When *reply_markup* is provided it is used directly; otherwise buttons
    are extracted from ``[button:...]`` markers in the text.
    """
    file_paths = _FILE_PATH_RE.findall(text)
    clean_text = _FILE_PATH_RE.sub("", text).strip()
    logger.debug("Sending rich text chars=%d files=%d", len(clean_text), len(file_paths))

    button_markup = reply_markup if reply_markup is not None else extract_buttons(clean_text)[1]
    last_msg: Message | None = None

    if clean_text:
        last_msg = await _send_text_chunks(bot, chat_id, clean_text, reply_to=reply_to)

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
        await send_file(bot, chat_id, Path(fp), allowed_roots=allowed_roots)


async def send_file(
    bot: Bot,
    chat_id: int,
    path: Path,
    *,
    allowed_roots: Sequence[Path] | None = None,
) -> None:
    """Send a local file as photo (images) or document (everything else)."""
    if allowed_roots is not None and not is_path_safe(path, allowed_roots):
        logger.warning("File path blocked (outside allowed roots): %s", path)
        root_list = ", ".join(str(r) for r in allowed_roots)
        await bot.send_message(
            chat_id=chat_id,
            text=(
                f"[File blocked: {path.name}]\n"
                f"Path '{path}' is outside the allowed directories ({root_list})."
            ),
            parse_mode=None,
        )
        return

    if not path.exists():  # noqa: ASYNC240
        logger.warning("File not found, skipping: %s", path)
        await bot.send_message(
            chat_id=chat_id, text=f"[File not found: {path.name}]", parse_mode=None
        )
        return

    try:
        input_file = FSInputFile(path)
        ext = path.suffix.lower()
        mime = mimetypes.guess_type(str(path))[0] or ""

        is_raster_image = ext in _IMAGE_EXTENSIONS or (
            mime.startswith("image/") and ext not in {".svg", ".svgz"}
        )
        if is_raster_image:
            await bot.send_photo(chat_id=chat_id, photo=input_file)
        else:
            await bot.send_document(chat_id=chat_id, document=input_file)

        logger.info("Sent file: %s (%s)", path.name, mime or ext)
    except TelegramNetworkError:
        logger.debug("Network error sending file (likely shutdown), skipping: %s", path)
    except (TelegramBadRequest, OSError):
        logger.exception("Failed to send file: %s", path)
        await bot.send_message(
            chat_id=chat_id, text=f"[Failed to send: {path.name}]", parse_mode=None
        )
