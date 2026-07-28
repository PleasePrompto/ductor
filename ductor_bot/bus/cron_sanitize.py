"""Shared cron result sanitisation logic.

Used by Telegram / Matrix / Slack transport adapters to clean cron output
before delivery:

* strip transport-level acknowledgement lines;
* treat trailing ``HEARTBEAT_OK`` as silent even when scratch narration precedes it;
* drop process monologue before the first high-confidence user-facing anchor
  (models sometimes put "Gathering… Gate delivered…" into final ``text``).
"""

from __future__ import annotations

import re

_CRON_ACK_MARKERS = ("message sent successfully", "delivered to telegram")
_CRON_SILENT_ACKS = frozenset({"HEARTBEAT_OK"})

# High-confidence starts of user-facing cron bodies. May appear mid-string
# when monologue is glued to the real message without a newline.
_USER_FACING_ANCHOR_RE = re.compile(
    r"(?:"
    r"🌙\s*\d{1,2}[./]\d{2}"  # e.g. evening recap header 🌙 28.07
    r"|^\s*HEARTBEAT_OK\b"
    r"|^\s*[✅📊📅⚠️💊🏋️💪😴🍽️💰🎮🔔🧠☀️🌙]"
    r")",
    re.MULTILINE,
)

_NARRATION_LINE_RE = re.compile(
    r"(?is)^\s*(?:"
    r"TASK\s*:"
    r"|Gathering\b"
    r"|Checking\b"
    r"|Updating memory\b"
    r"|Gate delivered\b"
    r"|Pulling\b"
    r"|Found\b"
    r"|Memory shows\b"
    r"|Returning\b"
    r"|No memory\b"
    r"|Writing the proactive\b"
    r"|then sending\b"
    r"|health-only recap\b"
    r"|proactive trace\b"
    r"|Read through TASK_DESCRIPTION\b"
    r"|sources fresh\b"
    r").*$"
)


def is_cron_transport_ack_line(line: str) -> bool:
    """True if *line* is a transport-level ack (not user-facing)."""
    normalized = " ".join(line.lower().split())
    return all(marker in normalized for marker in _CRON_ACK_MARKERS)


def _last_meaningful_line(text: str) -> str:
    """Return last non-empty stripped line, or empty string."""
    for line in reversed(text.splitlines()):
        s = line.strip()
        if s:
            return s
    return ""


def _strip_preamble(text: str) -> str:
    """Drop process monologue before the first user-facing anchor."""
    if not text:
        return ""

    match = _USER_FACING_ANCHOR_RE.search(text)
    if match is not None and match.start() > 0:
        return text[match.start() :].lstrip()

    kept: list[str] = []
    for line in text.splitlines():
        if _NARRATION_LINE_RE.match(line):
            continue
        kept.append(line)
    return "\n".join(kept).strip()


def _is_silent_ack(text: str) -> bool:
    """True if text is empty or a (possibly narrated) HEARTBEAT_OK."""
    if not text:
        return True
    if text.strip() in _CRON_SILENT_ACKS:
        return True
    return _last_meaningful_line(text) in _CRON_SILENT_ACKS


def sanitize_cron_result_text(result: str) -> str:
    """Strip transport acks, silent HEARTBEAT, and process preambles."""
    if _is_silent_ack(result):
        return ""
    lines = [line for line in result.splitlines() if not is_cron_transport_ack_line(line)]
    cleaned = "\n".join(lines).strip()
    if _is_silent_ack(cleaned):
        return ""
    return _strip_preamble(cleaned)
