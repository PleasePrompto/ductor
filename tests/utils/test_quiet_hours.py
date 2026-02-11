"""Tests for quiet hour utilities."""

from __future__ import annotations

from unittest.mock import patch
from zoneinfo import ZoneInfo

import time_machine

from ductor_bot.utils.quiet_hours import check_quiet_hour, is_quiet_hour

# ---------------------------------------------------------------------------
# is_quiet_hour: wrap-around (21-8)
# ---------------------------------------------------------------------------


async def test_is_quiet_hour_wrap_around_before_midnight() -> None:
    """Hour 22 is within 21-8 (wraps around midnight)."""
    assert is_quiet_hour(22, 21, 8) is True


async def test_is_quiet_hour_wrap_around_after_midnight() -> None:
    """Hour 3 is within 21-8 (wraps around midnight)."""
    assert is_quiet_hour(3, 21, 8) is True


async def test_is_quiet_hour_wrap_around_outside() -> None:
    """Hour 12 is outside 21-8."""
    assert is_quiet_hour(12, 21, 8) is False


async def test_is_quiet_hour_wrap_around_midnight_exact() -> None:
    """Hour 0 is within 21-8."""
    assert is_quiet_hour(0, 21, 8) is True


# ---------------------------------------------------------------------------
# is_quiet_hour: no wrap (2-6)
# ---------------------------------------------------------------------------


async def test_is_quiet_hour_no_wrap_inside() -> None:
    """Hour 4 is within 2-6."""
    assert is_quiet_hour(4, 2, 6) is True


async def test_is_quiet_hour_no_wrap_outside_before() -> None:
    """Hour 1 is outside 2-6."""
    assert is_quiet_hour(1, 2, 6) is False


async def test_is_quiet_hour_no_wrap_outside_after() -> None:
    """Hour 10 is outside 2-6."""
    assert is_quiet_hour(10, 2, 6) is False


# ---------------------------------------------------------------------------
# is_quiet_hour: boundaries (start inclusive, end exclusive)
# ---------------------------------------------------------------------------


async def test_is_quiet_hour_start_inclusive() -> None:
    """Start hour is inclusive: hour 21 IS quiet for 21-8."""
    assert is_quiet_hour(21, 21, 8) is True


async def test_is_quiet_hour_end_exclusive() -> None:
    """End hour is exclusive: hour 8 is NOT quiet for 21-8."""
    assert is_quiet_hour(8, 21, 8) is False


async def test_is_quiet_hour_start_inclusive_no_wrap() -> None:
    """Start hour is inclusive for non-wrapping: hour 2 IS quiet for 2-6."""
    assert is_quiet_hour(2, 2, 6) is True


async def test_is_quiet_hour_end_exclusive_no_wrap() -> None:
    """End hour is exclusive for non-wrapping: hour 6 is NOT quiet for 2-6."""
    assert is_quiet_hour(6, 2, 6) is False


# ---------------------------------------------------------------------------
# is_quiet_hour: disabled (start == end)
# ---------------------------------------------------------------------------


async def test_is_quiet_hour_disabled_zero() -> None:
    """quiet_start == quiet_end (0, 0) -> never quiet."""
    assert is_quiet_hour(0, 0, 0) is False
    assert is_quiet_hour(12, 0, 0) is False
    assert is_quiet_hour(23, 0, 0) is False


async def test_is_quiet_hour_disabled_nonzero() -> None:
    """quiet_start == quiet_end (10, 10) -> never quiet."""
    assert is_quiet_hour(10, 10, 10) is False
    assert is_quiet_hour(0, 10, 10) is False


# ---------------------------------------------------------------------------
# check_quiet_hour: task-specific values override globals
# ---------------------------------------------------------------------------


@time_machine.travel("2025-06-15T14:00:00+00:00")
async def test_check_quiet_hour_task_specific_overrides_global() -> None:
    """Task-specific quiet_start/quiet_end override global defaults."""
    # Global: 21-8 (not quiet at 14:00 UTC).
    # Task-specific: 10-16 (quiet at 14:00 UTC).
    is_quiet, hour, _tz = check_quiet_hour(
        quiet_start=10,
        quiet_end=16,
        user_timezone="UTC",
        global_quiet_start=21,
        global_quiet_end=8,
    )
    assert is_quiet is True
    assert hour == 14


@time_machine.travel("2025-06-15T14:00:00+00:00")
async def test_check_quiet_hour_task_specific_active() -> None:
    """Task-specific values make time active when global would be quiet."""
    is_quiet, hour, _tz = check_quiet_hour(
        quiet_start=0,
        quiet_end=6,
        user_timezone="UTC",
        global_quiet_start=10,
        global_quiet_end=16,
    )
    assert is_quiet is False
    assert hour == 14


# ---------------------------------------------------------------------------
# check_quiet_hour: None falls back to global
# ---------------------------------------------------------------------------


@time_machine.travel("2025-06-15T23:00:00+00:00")
async def test_check_quiet_hour_none_uses_global_quiet() -> None:
    """None quiet_start/quiet_end falls back to global defaults."""
    is_quiet, hour, _tz = check_quiet_hour(
        quiet_start=None,
        quiet_end=None,
        user_timezone="UTC",
        global_quiet_start=21,
        global_quiet_end=8,
    )
    assert is_quiet is True
    assert hour == 23


@time_machine.travel("2025-06-15T12:00:00+00:00")
async def test_check_quiet_hour_none_uses_global_active() -> None:
    """Global defaults allow active hours when task-specific is None."""
    is_quiet, hour, _tz = check_quiet_hour(
        quiet_start=None,
        quiet_end=None,
        user_timezone="UTC",
        global_quiet_start=21,
        global_quiet_end=8,
    )
    assert is_quiet is False
    assert hour == 12


@time_machine.travel("2025-06-15T14:00:00+00:00")
async def test_check_quiet_hour_partial_none_start() -> None:
    """Only quiet_start is None, falls back to global_quiet_start."""
    is_quiet, _hour, _tz = check_quiet_hour(
        quiet_start=None,
        quiet_end=16,
        user_timezone="UTC",
        global_quiet_start=10,
        global_quiet_end=8,
    )
    # start=10 (from global), end=16 (from task) -> 10-16 range, hour=14 -> quiet
    assert is_quiet is True


@time_machine.travel("2025-06-15T14:00:00+00:00")
async def test_check_quiet_hour_partial_none_end() -> None:
    """Only quiet_end is None, falls back to global_quiet_end."""
    is_quiet, _hour, _tz = check_quiet_hour(
        quiet_start=10,
        quiet_end=None,
        user_timezone="UTC",
        global_quiet_start=21,
        global_quiet_end=16,
    )
    # start=10 (from task), end=16 (from global) -> 10-16 range, hour=14 -> quiet
    assert is_quiet is True


# ---------------------------------------------------------------------------
# check_quiet_hour: timezone handling
# ---------------------------------------------------------------------------


@time_machine.travel("2025-06-15T22:00:00+00:00")
async def test_check_quiet_hour_timezone_europe() -> None:
    """Time in Europe/Berlin is UTC+2 in summer (CEST). 22:00 UTC = 00:00 Berlin."""
    is_quiet, hour, tz = check_quiet_hour(
        quiet_start=21,
        quiet_end=8,
        user_timezone="Europe/Berlin",
        global_quiet_start=21,
        global_quiet_end=8,
    )
    assert hour == 0  # midnight in Berlin
    assert is_quiet is True
    assert tz == ZoneInfo("Europe/Berlin")


@time_machine.travel("2025-06-15T06:00:00+00:00")
async def test_check_quiet_hour_timezone_makes_active() -> None:
    """06:00 UTC is 15:00 in Asia/Tokyo (UTC+9), which is active hours."""
    is_quiet, hour, tz = check_quiet_hour(
        quiet_start=21,
        quiet_end=8,
        user_timezone="Asia/Tokyo",
        global_quiet_start=21,
        global_quiet_end=8,
    )
    assert hour == 15
    assert is_quiet is False
    assert tz == ZoneInfo("Asia/Tokyo")


@time_machine.travel("2025-06-15T10:00:00+00:00")
async def test_check_quiet_hour_returns_zoneinfo() -> None:
    """check_quiet_hour returns a ZoneInfo object as third element."""
    _, _, tz = check_quiet_hour(
        quiet_start=None,
        quiet_end=None,
        user_timezone="America/New_York",
        global_quiet_start=21,
        global_quiet_end=8,
    )
    assert isinstance(tz, ZoneInfo)
    assert str(tz) == "America/New_York"


@time_machine.travel("2025-06-15T12:00:00+00:00")
async def test_check_quiet_hour_invalid_timezone_fallback() -> None:
    """Invalid timezone falls back to host timezone or UTC."""
    with patch(
        "ductor_bot.utils.quiet_hours.resolve_user_timezone",
        return_value=ZoneInfo("UTC"),
    ):
        is_quiet, hour, _tz = check_quiet_hour(
            quiet_start=None,
            quiet_end=None,
            user_timezone="Invalid/Zone",
            global_quiet_start=21,
            global_quiet_end=8,
        )
    assert hour == 12
    assert is_quiet is False
