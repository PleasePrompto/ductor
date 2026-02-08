"""Handle incoming Telegram media: download, index, and prompt injection."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import mimetypes
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml
from aiogram.exceptions import TelegramAPIError

if TYPE_CHECKING:
    from aiogram import Bot
    from aiogram.types import Message

logger = logging.getLogger(__name__)

_INDEX_SKIP = frozenset({"_index.yaml", "CLAUDE.md", "AGENTS.md"})


@dataclass(frozen=True, slots=True)
class MediaInfo:
    """Metadata for a downloaded Telegram media file."""

    path: Path
    media_type: str
    file_name: str
    caption: str | None
    original_type: str


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def has_media(message: Message) -> bool:
    """True if *message* contains a downloadable media attachment."""
    return bool(
        message.photo
        or message.document
        or message.voice
        or message.video
        or message.audio
        or message.sticker
        or message.video_note
    )


def is_media_addressed(
    message: Message,
    bot_id: int | None,
    bot_username: str | None,
) -> bool:
    """True if a media message in a group chat is addressed to the bot."""
    if (
        message.reply_to_message
        and message.reply_to_message.from_user
        and bot_id is not None
        and message.reply_to_message.from_user.id == bot_id
    ):
        return True
    if message.caption_entities and message.caption and bot_username:
        return any(
            e.type == "mention"
            and message.caption[e.offset : e.offset + e.length].lower() == f"@{bot_username}"
            for e in message.caption_entities
        )
    return False


async def resolve_media_text(
    bot: Bot,
    message: Message,
    telegram_files_dir: Path,
    workspace: Path,
) -> str | None:
    """Download media from *message*, update index, return agent prompt.

    Returns ``None`` if the download fails or the message has no media.
    """
    await asyncio.to_thread(telegram_files_dir.mkdir, parents=True, exist_ok=True)

    try:
        info = await download_media(bot, message, telegram_files_dir)
    except (TelegramAPIError, OSError):
        logger.exception("Failed to download media from chat=%d", message.chat.id)
        await message.answer("Could not download that file.")
        return None

    if info is None:
        return None

    try:
        await asyncio.to_thread(update_index, telegram_files_dir)
    except (OSError, yaml.YAMLError):
        logger.warning("Index update failed", exc_info=True)

    return build_media_prompt(info, workspace)


# ---------------------------------------------------------------------------
# Download
# ---------------------------------------------------------------------------

_MediaTuple = tuple[str | None, Any, str, str]


async def download_media(bot: Bot, message: Message, base_dir: Path) -> MediaInfo | None:
    """Download the first media attachment into *base_dir*/YYYY-MM-DD/.

    Returns ``None`` when the message contains no supported media.
    """
    kind, file_obj, file_name, mime = _resolve_media(message)
    if kind is None or file_obj is None:
        return None

    dest = await asyncio.to_thread(_prepare_destination, base_dir, file_name)
    await bot.download(file_obj, destination=dest)
    logger.info("Downloaded %s -> %s (%s)", kind, dest, mime)

    return MediaInfo(
        path=dest,
        media_type=mime,
        file_name=dest.name,
        caption=message.caption,
        original_type=kind,
    )


def _prepare_destination(base_dir: Path, file_name: str) -> Path:
    """Create date directory and return a non-colliding destination path."""
    day_dir = base_dir / datetime.now(tz=UTC).strftime("%Y-%m-%d")
    day_dir.mkdir(parents=True, exist_ok=True)

    dest = day_dir / file_name
    if dest.exists():
        stem, suffix = dest.stem, dest.suffix
        counter = 1
        while dest.exists():
            dest = day_dir / f"{stem}_{counter}{suffix}"
            counter += 1
    return dest


# ---------------------------------------------------------------------------
# Media extractors
# ---------------------------------------------------------------------------


def _resolve_media(message: Message) -> _MediaTuple:
    """Inspect *message* and return ``(kind, downloadable, filename, mime)``."""
    for extractor in (
        _extract_photo,
        _extract_document,
        _extract_voice,
        _extract_audio,
        _extract_video,
        _extract_video_note,
        _extract_sticker,
    ):
        result = extractor(message)
        if result is not None:
            return result
    return None, None, "", ""


def _extract_photo(msg: Message) -> _MediaTuple | None:
    if not msg.photo:
        return None
    photo = msg.photo[-1]
    return "photo", photo, f"photo_{photo.file_unique_id}.jpg", "image/jpeg"


def _extract_document(msg: Message) -> _MediaTuple | None:
    if not msg.document:
        return None
    doc = msg.document
    name = doc.file_name or f"doc_{doc.file_unique_id}"
    mime = doc.mime_type or mimetypes.guess_type(name)[0] or "application/octet-stream"
    return "document", doc, _sanitize_filename(name), mime


def _extract_voice(msg: Message) -> _MediaTuple | None:
    if not msg.voice:
        return None
    v = msg.voice
    return "voice", v, f"voice_{v.file_unique_id}.ogg", v.mime_type or "audio/ogg"


def _extract_audio(msg: Message) -> _MediaTuple | None:
    if not msg.audio:
        return None
    a = msg.audio
    mime = a.mime_type or "audio/mpeg"
    ext = mimetypes.guess_extension(mime) or ".mp3"
    name = a.file_name or f"audio_{a.file_unique_id}{ext}"
    return "audio", a, _sanitize_filename(name), mime


def _extract_video(msg: Message) -> _MediaTuple | None:
    if not msg.video:
        return None
    v = msg.video
    mime = v.mime_type or "video/mp4"
    name = v.file_name or f"video_{v.file_unique_id}.mp4"
    return "video", v, _sanitize_filename(name), mime


def _extract_video_note(msg: Message) -> _MediaTuple | None:
    if not msg.video_note:
        return None
    vn = msg.video_note
    return "video_note", vn, f"videonote_{vn.file_unique_id}.mp4", "video/mp4"


def _extract_sticker(msg: Message) -> _MediaTuple | None:
    if not msg.sticker:
        return None
    s = msg.sticker
    uid = s.file_unique_id
    if s.is_animated:
        return "sticker", s, f"sticker_{uid}.tgs", "application/x-tgsticker"
    if s.is_video:
        return "sticker", s, f"sticker_{uid}.webm", "video/webm"
    return "sticker", s, f"sticker_{uid}.webp", "image/webp"


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------


def update_index(base_dir: Path) -> None:
    """Rebuild ``_index.yaml`` by scanning all date subdirectories."""
    tree: dict[str, list[dict[str, object]]] = {}
    total = 0

    for entry in sorted(base_dir.iterdir()):
        if not entry.is_dir() or len(entry.name) != 10 or entry.name[4] != "-":
            continue
        files: list[dict[str, object]] = []
        for f in sorted(entry.iterdir()):
            if not f.is_file() or f.name in _INDEX_SKIP:
                continue
            stat = f.stat()
            mime = mimetypes.guess_type(f.name)[0] or "application/octet-stream"
            files.append(
                {
                    "name": f.name,
                    "type": mime,
                    "size": stat.st_size,
                    "received": datetime.fromtimestamp(stat.st_mtime, tz=UTC).isoformat(),
                }
            )
            total += 1
        if files:
            tree[entry.name] = files

    index = {
        "last_updated": datetime.now(tz=UTC).isoformat(),
        "total_files": total,
        "tree": tree,
    }
    index_path = base_dir / "_index.yaml"
    index_path.write_text(
        yaml.safe_dump(index, default_flow_style=False, allow_unicode=True, sort_keys=False),
        encoding="utf-8",
    )
    logger.debug("Index updated: %d files across %d days", total, len(tree))


# ---------------------------------------------------------------------------
# Prompt injection
# ---------------------------------------------------------------------------


def build_media_prompt(info: MediaInfo, workspace: Path) -> str:
    """Build the prompt injected into the orchestrator for a received file.

    Paths are relative to *workspace* so they work in both host and Docker.
    """
    rel_path: Path | str = info.path
    with contextlib.suppress(ValueError):
        rel_path = info.path.relative_to(workspace)

    lines = [
        "[INCOMING FILE]",
        "The user sent you a file via Telegram.",
        f"Path: {rel_path}",
        f"Type: {info.media_type}",
        f"Original filename: {info.file_name}",
        "",
        "Check tools/telegram_tools/CLAUDE.md for file handling instructions.",
    ]

    if info.original_type in ("voice", "audio"):
        lines.append(
            "This is an audio/voice message. Use "
            f"tools/telegram_tools/transcribe_audio.py --file {rel_path} "
            "to transcribe it, then respond to the content."
        )

    if info.original_type in ("video", "video_note"):
        lines.append(
            "This is a video file. Use "
            f"tools/telegram_tools/process_video.py --file {rel_path} "
            "to extract keyframes and transcribe audio, then respond to the content."
        )

    if info.caption:
        lines.append("")
        lines.append(f"User message: {info.caption}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sanitize_filename(name: str) -> str:
    """Remove path separators and null bytes, collapse underscores."""
    name = name.replace("/", "_").replace("\\", "_").replace("\x00", "")
    while "__" in name:
        name = name.replace("__", "_")
    return name.strip("_. ")[:120] or "file"
