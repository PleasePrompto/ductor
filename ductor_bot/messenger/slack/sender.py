"""Slack message sender."""

from __future__ import annotations

import logging
import re
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ductor_bot.files.tags import path_from_file_tag
from ductor_bot.messenger.send_opts import BaseSendOpts

if TYPE_CHECKING:
    from typing import Any

logger = logging.getLogger(__name__)

_MAX_MESSAGE_LENGTH = 39_000
_FILE_TAG_RE = re.compile(r"<file:(.*?)>")


@dataclass(slots=True)
class SlackSendOpts(BaseSendOpts):
    """Options for sending Slack messages."""

    thread_ts: str | None = None


async def send_rich(
    client: Any,
    channel_id: str,
    text: str,
    opts: SlackSendOpts | None = None,
) -> str | None:
    """Send a Slack message, splitting long text and uploading tagged files."""
    opts = opts or SlackSendOpts()
    files = _FILE_TAG_RE.findall(text)
    cleaned = _FILE_TAG_RE.sub("", text).strip()
    last_ts: str | None = None

    for chunk in _split_text(cleaned):
        if not chunk:
            continue
        response = await client.chat_postMessage(
            channel=channel_id,
            text=_to_slack_mrkdwn(chunk),
            mrkdwn=True,
            thread_ts=opts.thread_ts,
        )
        last_ts = _response_value(response, "ts")

    for file_path_str in files:
        file_path = path_from_file_tag(file_path_str)
        if not _file_accessible(file_path, opts.allowed_roots):
            continue
        response = await client.files_upload_v2(
            channel=channel_id,
            file=str(file_path),
            filename=file_path.name,
            thread_ts=opts.thread_ts,
        )
        last_ts = _response_value(response, "ts") or last_ts

    return last_ts


async def update_message(
    client: Any,
    channel_id: str,
    message_ts: str,
    text: str,
    *,
    thread_ts: str | None = None,
) -> None:
    """Update an existing Slack message."""
    await client.chat_update(
        channel=channel_id,
        ts=message_ts,
        text=_to_slack_mrkdwn(text),
        mrkdwn=True,
        thread_ts=thread_ts,
    )


def _response_value(response: object, key: str) -> str | None:
    if isinstance(response, dict):
        value = response.get(key)
        return value if isinstance(value, str) else None
    data = getattr(response, "data", None)
    if isinstance(data, dict):
        value = data.get(key)
        return value if isinstance(value, str) else None
    value = getattr(response, key, None)
    return value if isinstance(value, str) else None


def _file_accessible(file_path: Path, allowed_roots: Sequence[Path] | None) -> bool:
    if not file_path.exists():
        logger.warning("File not found: %s", file_path)
        return False
    if allowed_roots is not None and not any(
        file_path.resolve().is_relative_to(root.resolve()) for root in allowed_roots
    ):
        logger.warning("File outside allowed roots: %s", file_path)
        return False
    return True


def _split_text(text: str) -> list[str]:
    """Split long Slack messages into reasonably sized chunks."""
    if not text:
        return [""]
    if len(text) <= _MAX_MESSAGE_LENGTH:
        return [text]

    chunks: list[str] = []
    remaining = text
    while remaining:
        if len(remaining) <= _MAX_MESSAGE_LENGTH:
            chunks.append(remaining)
            break
        split_at = remaining.rfind("\n", 0, _MAX_MESSAGE_LENGTH)
        if split_at <= 0:
            split_at = _MAX_MESSAGE_LENGTH
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip()
    return chunks


def _to_slack_mrkdwn(text: str) -> str:
    """Best-effort Markdown -> Slack mrkdwn normalization."""
    return re.sub(r"__(.+?)__", r"_\1_", re.sub(r"\*\*(.+?)\*\*", r"*\1*", text))
