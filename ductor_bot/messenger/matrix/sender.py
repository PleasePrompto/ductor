"""Message sending for Matrix rooms.

Handles formatted messages, file uploads, and message splitting.
"""

from __future__ import annotations

import asyncio
import io
import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ductor_bot.files.tags import guess_mime, path_from_file_tag
from ductor_bot.messenger.matrix.formatting import markdown_to_matrix_html
from ductor_bot.messenger.send_opts import BaseSendOpts

if TYPE_CHECKING:
    from nio import AsyncClient

logger = logging.getLogger(__name__)

# Matrix spec allows 65536 bytes per event body.
# Reserve ~5.5 KB for JSON framing overhead (event metadata,
# content keys, formatting tags).
_MAX_EVENT_SIZE = 60_000
_FILE_TAG_RE = re.compile(r"<file:(.*?)>")


@dataclass
class MatrixSendOpts(BaseSendOpts):
    """Options for sending a Matrix message."""

    reply_to_event_id: str | None = None
    thread_event_id: str | None = None


async def send_rich(
    client: AsyncClient,
    room_id: str,
    text: str,
    opts: MatrixSendOpts | None = None,
) -> str | None:
    """Send formatted message to Matrix room. Returns event_id of last sent message."""
    opts = opts or MatrixSendOpts()

    # 1. Extract file tags
    files: list[str] = _FILE_TAG_RE.findall(text)
    cleaned = _FILE_TAG_RE.sub("", text).strip()

    # 2. Convert markdown → (plain, html)
    plain, html_body = markdown_to_matrix_html(cleaned)

    # 3. Build message content
    last_event_id: str | None = None

    if cleaned:
        # Split if too large (rare for Matrix)
        chunks = (
            _split_text(plain, html_body)
            if len(html_body.encode()) > _MAX_EVENT_SIZE
            else [(plain, html_body)]
        )

        for p, h in chunks:
            content: dict[str, object] = {
                "msgtype": "m.text",
                "body": p,
                "format": "org.matrix.custom.html",
                "formatted_body": h,
            }

            # Thread support
            if opts.thread_event_id:
                content["m.relates_to"] = {
                    "rel_type": "m.thread",
                    "event_id": opts.thread_event_id,
                }
            elif opts.reply_to_event_id:
                content["m.relates_to"] = {
                    "m.in_reply_to": {"event_id": opts.reply_to_event_id},
                }

            resp = await client.room_send(room_id, "m.room.message", content)
            if hasattr(resp, "event_id"):
                last_event_id = resp.event_id
            else:
                logger.warning("room_send failed for %s: %s", room_id, resp)

    # 4. Upload and send files
    for file_path_str in files:
        file_path = path_from_file_tag(file_path_str)
        if not _file_accessible(file_path, opts.allowed_roots):
            continue

        event_id = await _upload_and_send_file(client, room_id, file_path)
        if event_id:
            last_event_id = event_id

    return last_event_id


def _file_accessible(
    file_path: Path,
    allowed_roots: Sequence[Path] | None,
) -> bool:
    """Check if *file_path* exists and is within *allowed_roots* (sync)."""
    if not file_path.exists():
        logger.warning("File not found: %s", file_path)
        return False
    if allowed_roots is not None and not any(
        file_path.resolve().is_relative_to(root.resolve()) for root in allowed_roots
    ):
        logger.warning("File outside allowed roots: %s", file_path)
        return False
    return True


def _read_file(file_path: Path) -> tuple[str, bytes, str]:
    """Read file data synchronously — returns (mime, data, name)."""
    mime = guess_mime(file_path)
    data = file_path.read_bytes()
    return mime, data, file_path.name


async def _upload_and_send_file(
    client: AsyncClient,
    room_id: str,
    file_path: Path,
) -> str | None:
    """Upload a file to the homeserver and send as m.file/m.image."""
    mime_type, file_data, file_name = await asyncio.to_thread(_read_file, file_path)
    file_size = len(file_data)

    # Upload — nio expects a file-like object, not raw bytes
    resp, _keys = await client.upload(
        io.BytesIO(file_data),
        content_type=mime_type,
        filename=file_name,
        filesize=file_size,
    )

    if not hasattr(resp, "content_uri"):
        logger.warning("File upload failed for %s", file_path)
        return None

    # Determine message type
    if mime_type.startswith("image/"):
        msgtype = "m.image"
    elif mime_type.startswith("audio/"):
        msgtype = "m.audio"
    elif mime_type.startswith("video/"):
        msgtype = "m.video"
    else:
        msgtype = "m.file"

    content: dict[str, object] = {
        "msgtype": msgtype,
        "body": file_name,
        "url": resp.content_uri,
        "info": {
            "mimetype": mime_type,
            "size": file_size,
        },
    }

    send_resp = await client.room_send(room_id, "m.room.message", content)
    if hasattr(send_resp, "event_id"):
        return str(send_resp.event_id)
    logger.warning("File send failed for %s in %s: %s", file_name, room_id, send_resp)
    return None


def _split_text(plain: str, _html_body: str) -> list[tuple[str, str]]:
    """Split text into chunks that fit within the Matrix event size limit.

    Splits raw plain text by lines and converts each chunk to HTML
    independently.  Formatting spanning chunk boundaries (e.g. a very
    long code block) may be disrupted — acceptable for the rare
    >60 KB case.

    *_html_body* is intentionally unused; each chunk is converted
    fresh to guarantee plain/HTML alignment.
    """
    plain_lines = plain.split("\n")

    raw_chunks: list[str] = []
    cur_lines: list[str] = []
    cur_size = 0

    for line in plain_lines:
        line_size = len(line.encode()) + 1
        if cur_size + line_size > _MAX_EVENT_SIZE and cur_lines:
            raw_chunks.append("\n".join(cur_lines))
            cur_lines = []
            cur_size = 0
        cur_lines.append(line)
        cur_size += line_size

    if cur_lines:
        raw_chunks.append("\n".join(cur_lines))

    if not raw_chunks:
        return [("", "")]

    return [markdown_to_matrix_html(chunk) for chunk in raw_chunks]
