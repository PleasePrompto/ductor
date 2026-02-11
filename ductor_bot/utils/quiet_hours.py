"""Shared quiet hour utilities for heartbeat, cron, and webhooks."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

from ductor_bot.config import resolve_user_timezone


def is_quiet_hour(now_hour: int, quiet_start: int, quiet_end: int) -> bool:
    """Check if *now_hour* falls within the quiet window.

    Handles wrap-around: quiet_start=21, quiet_end=8 means 21-23 and 0-7
    are quiet.  If quiet_start == quiet_end, never quiet.
    """
    if quiet_start == quiet_end:
        return False
    if quiet_start <= quiet_end:
        return quiet_start <= now_hour < quiet_end
    return now_hour >= quiet_start or now_hour < quiet_end


def check_quiet_hour(
    *,
    quiet_start: int | None,
    quiet_end: int | None,
    user_timezone: str,
    global_quiet_start: int = 21,
    global_quiet_end: int = 8,
) -> tuple[bool, int, ZoneInfo]:
    """Check if current time is in quiet hours, with global fallback.

    Returns ``(is_quiet, current_hour, timezone)``.
    """
    start = quiet_start if quiet_start is not None else global_quiet_start
    end = quiet_end if quiet_end is not None else global_quiet_end

    tz = resolve_user_timezone(user_timezone)
    now_hour = datetime.now(tz).hour

    return is_quiet_hour(now_hour, start, end), now_hour, tz
