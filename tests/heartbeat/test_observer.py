"""Tests for the heartbeat observer."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
import time_machine

from ductor_bot.config import AgentConfig, HeartbeatConfig
from ductor_bot.heartbeat.observer import HeartbeatObserver
from ductor_bot.orchestrator.flows import _strip_ack_token
from ductor_bot.utils.quiet_hours import is_quiet_hour

# ---------------------------------------------------------------------------
# Quiet hour logic
# ---------------------------------------------------------------------------


class TestIsQuietHour:
    def test_within_evening_quiet(self) -> None:
        # quiet 21-08: 22 is quiet
        assert is_quiet_hour(22, 21, 8) is True

    def test_within_morning_quiet(self) -> None:
        # quiet 21-08: 3 is quiet
        assert is_quiet_hour(3, 21, 8) is True

    def test_boundary_start_is_quiet(self) -> None:
        assert is_quiet_hour(21, 21, 8) is True

    def test_boundary_end_is_not_quiet(self) -> None:
        # end is exclusive
        assert is_quiet_hour(8, 21, 8) is False

    def test_daytime_is_not_quiet(self) -> None:
        assert is_quiet_hour(14, 21, 8) is False

    def test_no_wrap_quiet_window(self) -> None:
        # quiet 2-6: 4 is quiet, 1 is not
        assert is_quiet_hour(4, 2, 6) is True
        assert is_quiet_hour(1, 2, 6) is False
        assert is_quiet_hour(7, 2, 6) is False

    def test_midnight_in_wrap_window(self) -> None:
        assert is_quiet_hour(0, 21, 8) is True

    def test_same_start_end_means_never_quiet(self) -> None:
        assert is_quiet_hour(12, 8, 8) is False
        assert is_quiet_hour(8, 8, 8) is False


# ---------------------------------------------------------------------------
# ACK token stripping
# ---------------------------------------------------------------------------


class TestStripAckToken:
    def test_exact_token(self) -> None:
        assert _strip_ack_token("HEARTBEAT_OK", "HEARTBEAT_OK") == ""

    def test_token_with_whitespace(self) -> None:
        assert _strip_ack_token("  HEARTBEAT_OK  ", "HEARTBEAT_OK") == ""

    def test_leading_token(self) -> None:
        assert _strip_ack_token("HEARTBEAT_OK Some extra text", "HEARTBEAT_OK") == "Some extra text"

    def test_trailing_token(self) -> None:
        assert _strip_ack_token("Some text HEARTBEAT_OK", "HEARTBEAT_OK") == "Some text"

    def test_no_token(self) -> None:
        assert _strip_ack_token("Hello world", "HEARTBEAT_OK") == "Hello world"

    def test_empty_input(self) -> None:
        assert _strip_ack_token("", "HEARTBEAT_OK") == ""

    def test_token_in_middle_not_stripped(self) -> None:
        result = _strip_ack_token("Before HEARTBEAT_OK After", "HEARTBEAT_OK")
        # Leading strip removes HEARTBEAT_OK, leaving trailing intact
        assert "Before" not in result or "After" in result


# ---------------------------------------------------------------------------
# Observer lifecycle
# ---------------------------------------------------------------------------


def _make_config(*, enabled: bool = True, interval: int = 30) -> AgentConfig:
    return AgentConfig(
        heartbeat=HeartbeatConfig(enabled=enabled, interval_minutes=interval),
        allowed_user_ids=[100, 200],
    )


class TestHeartbeatObserverSetup:
    async def test_disabled_does_not_start_task(self) -> None:
        config = _make_config(enabled=False)
        obs = HeartbeatObserver(config)
        obs.set_heartbeat_handler(AsyncMock())
        await obs.start()
        assert obs._task is None
        await obs.stop()

    async def test_enabled_starts_task(self) -> None:
        config = _make_config(enabled=True)
        obs = HeartbeatObserver(config)
        obs.set_heartbeat_handler(AsyncMock())
        await obs.start()
        assert obs._task is not None
        await obs.stop()
        assert obs._task is None

    async def test_no_handler_does_not_start(self) -> None:
        config = _make_config(enabled=True)
        obs = HeartbeatObserver(config)
        await obs.start()
        assert obs._task is None


class TestHeartbeatObserverTick:
    async def test_tick_calls_handler_for_each_user(self) -> None:
        config = _make_config()
        obs = HeartbeatObserver(config)
        handler = AsyncMock(return_value=None)
        obs.set_heartbeat_handler(handler)

        with time_machine.travel(datetime(2026, 1, 15, 14, 0, tzinfo=UTC)):
            await obs._tick()

        assert handler.call_count == 2
        handler.assert_any_await(100)
        handler.assert_any_await(200)

    async def test_tick_skips_busy_chat(self) -> None:
        config = _make_config()
        obs = HeartbeatObserver(config)
        handler = AsyncMock(return_value=None)
        obs.set_heartbeat_handler(handler)
        obs.set_busy_check(lambda cid: cid == 100)

        with time_machine.travel(datetime(2026, 1, 15, 14, 0, tzinfo=UTC)):
            await obs._tick()

        handler.assert_awaited_once_with(200)

    async def test_tick_delivers_alert(self) -> None:
        config = _make_config()
        obs = HeartbeatObserver(config)
        obs.set_heartbeat_handler(AsyncMock(return_value="Hey, check this out!"))
        result_handler = AsyncMock()
        obs.set_result_handler(result_handler)

        with time_machine.travel(datetime(2026, 1, 15, 14, 0, tzinfo=UTC)):
            await obs._tick()

        assert result_handler.call_count == 2
        result_handler.assert_any_await(100, "Hey, check this out!")
        result_handler.assert_any_await(200, "Hey, check this out!")

    async def test_tick_suppresses_none_result(self) -> None:
        config = _make_config()
        obs = HeartbeatObserver(config)
        obs.set_heartbeat_handler(AsyncMock(return_value=None))
        result_handler = AsyncMock()
        obs.set_result_handler(result_handler)

        with time_machine.travel(datetime(2026, 1, 15, 14, 0, tzinfo=UTC)):
            await obs._tick()

        result_handler.assert_not_awaited()

    @pytest.mark.parametrize("hour", [21, 22, 23, 0, 1, 7])
    async def test_tick_skips_during_quiet_hours(self, hour: int) -> None:
        config = _make_config()
        obs = HeartbeatObserver(config)
        handler = AsyncMock(return_value=None)
        obs.set_heartbeat_handler(handler)

        with time_machine.travel(datetime(2026, 1, 15, hour, 30, tzinfo=UTC)):
            await obs._tick()

        handler.assert_not_awaited()

    async def test_tick_runs_during_active_hours(self) -> None:
        config = _make_config()
        obs = HeartbeatObserver(config)
        handler = AsyncMock(return_value=None)
        obs.set_heartbeat_handler(handler)

        with time_machine.travel(datetime(2026, 1, 15, 14, 0, tzinfo=UTC)):
            await obs._tick()

        assert handler.call_count == 2

    async def test_handler_exception_does_not_crash(self) -> None:
        config = _make_config()
        obs = HeartbeatObserver(config)
        obs.set_heartbeat_handler(AsyncMock(side_effect=RuntimeError("boom")))

        with time_machine.travel(datetime(2026, 1, 15, 14, 0, tzinfo=UTC)):
            await obs._tick()
