"""Tests for per-target heartbeat settings, resolution, and validation."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock

import time_machine

from ductor_bot.config import AgentConfig, HeartbeatConfig, HeartbeatTarget
from ductor_bot.heartbeat.observer import HeartbeatObserver

# ---------------------------------------------------------------------------
# HeartbeatTarget config model
# ---------------------------------------------------------------------------


class TestHeartbeatTargetConfig:
    def test_target_with_own_prompt(self) -> None:
        target = HeartbeatTarget(chat_id=123, prompt="Custom check")
        assert target.prompt == "Custom check"

    def test_target_falls_back_to_none(self) -> None:
        target = HeartbeatTarget(chat_id=123)
        assert target.transport == "tg"
        assert target.prompt is None
        assert target.ack_token is None
        assert target.interval_minutes is None

    def test_target_with_all_overrides(self) -> None:
        target = HeartbeatTarget(
            chat_id=123,
            topic_id=5,
            prompt="Check servers",
            ack_token="OK",
            interval_minutes=60,
            quiet_start=22,
            quiet_end=7,
        )
        assert target.interval_minutes == 60
        assert target.quiet_start == 22
        assert target.quiet_end == 7
        assert target.prompt == "Check servers"
        assert target.ack_token == "OK"

    def test_target_quiet_hours_default_to_none(self) -> None:
        target = HeartbeatTarget(chat_id=123)
        assert target.quiet_start is None
        assert target.quiet_end is None


# ---------------------------------------------------------------------------
# Settings resolution
# ---------------------------------------------------------------------------


def _make_observer(
    *,
    global_prompt: str = "Global prompt",
    global_ack: str = "HEARTBEAT_OK",
    global_quiet_start: int = 21,
    global_quiet_end: int = 8,
    targets: list[HeartbeatTarget] | None = None,
) -> HeartbeatObserver:
    config = AgentConfig(
        heartbeat=HeartbeatConfig(
            enabled=True,
            prompt=global_prompt,
            ack_token=global_ack,
            quiet_start=global_quiet_start,
            quiet_end=global_quiet_end,
            group_targets=targets or [],
        ),
        allowed_user_ids=[100],
    )
    obs = HeartbeatObserver(config)
    obs.set_heartbeat_handler(AsyncMock(return_value=None))
    return obs


class TestHeartbeatSettingsResolution:
    def test_resolve_prompt_from_target(self) -> None:
        target = HeartbeatTarget(chat_id=123, prompt="Custom check")
        obs = _make_observer(global_prompt="Global prompt", targets=[target])
        prompt, _ack, _qs, _qe = obs._resolve_target_settings(target)
        assert prompt == "Custom check"

    def test_resolve_prompt_fallback_to_global(self) -> None:
        target = HeartbeatTarget(chat_id=123)
        obs = _make_observer(global_prompt="Global prompt", targets=[target])
        prompt, _ack, _qs, _qe = obs._resolve_target_settings(target)
        assert prompt == "Global prompt"

    def test_resolve_ack_token_from_target(self) -> None:
        target = HeartbeatTarget(chat_id=123, ack_token="CUSTOM_OK")
        obs = _make_observer(global_ack="HEARTBEAT_OK", targets=[target])
        _prompt, ack, _qs, _qe = obs._resolve_target_settings(target)
        assert ack == "CUSTOM_OK"

    def test_resolve_ack_token_fallback_to_global(self) -> None:
        target = HeartbeatTarget(chat_id=123)
        obs = _make_observer(global_ack="HEARTBEAT_OK", targets=[target])
        _prompt, ack, _qs, _qe = obs._resolve_target_settings(target)
        assert ack == "HEARTBEAT_OK"

    def test_resolve_quiet_hours_from_target(self) -> None:
        target = HeartbeatTarget(chat_id=123, quiet_start=22, quiet_end=7)
        obs = _make_observer(global_quiet_start=21, global_quiet_end=8, targets=[target])
        _prompt, _ack, qs, qe = obs._resolve_target_settings(target)
        assert qs == 22
        assert qe == 7

    def test_resolve_quiet_hours_fallback_to_global(self) -> None:
        target = HeartbeatTarget(chat_id=123)
        obs = _make_observer(global_quiet_start=21, global_quiet_end=8, targets=[target])
        _prompt, _ack, qs, qe = obs._resolve_target_settings(target)
        assert qs == 21
        assert qe == 8

    def test_resolve_partial_quiet_hours_override(self) -> None:
        target = HeartbeatTarget(chat_id=123, quiet_start=23)
        obs = _make_observer(global_quiet_start=21, global_quiet_end=8, targets=[target])
        _prompt, _ack, qs, qe = obs._resolve_target_settings(target)
        assert qs == 23
        assert qe == 8


# ---------------------------------------------------------------------------
# Per-target quiet hours in _run_for_chat
# ---------------------------------------------------------------------------


class TestPerTargetQuietHours:
    async def test_target_quiet_hours_suppress_heartbeat(self) -> None:
        """A target with quiet_start=10, quiet_end=16 skips heartbeat at 14:00."""
        target = HeartbeatTarget(chat_id=-1001, quiet_start=10, quiet_end=16)
        config = AgentConfig(
            heartbeat=HeartbeatConfig(
                enabled=True,
                quiet_start=21,
                quiet_end=8,
                group_targets=[target],
            ),
            allowed_user_ids=[],
        )
        obs = HeartbeatObserver(config)
        handler = AsyncMock(return_value="alert")
        obs.set_heartbeat_handler(handler)

        with time_machine.travel(datetime(2026, 1, 15, 14, 0, tzinfo=UTC)):
            await obs._run_for_chat(
                -1001,
                quiet_start=10,
                quiet_end=16,
            )

        handler.assert_not_awaited()

    async def test_target_not_in_quiet_hours_runs(self) -> None:
        """A target with quiet_start=22, quiet_end=6 runs at 14:00."""
        target = HeartbeatTarget(chat_id=-1001, quiet_start=22, quiet_end=6)
        config = AgentConfig(
            heartbeat=HeartbeatConfig(
                enabled=True,
                quiet_start=21,
                quiet_end=8,
                group_targets=[target],
            ),
            allowed_user_ids=[],
        )
        obs = HeartbeatObserver(config)
        handler = AsyncMock(return_value=None)
        obs.set_heartbeat_handler(handler)

        with time_machine.travel(datetime(2026, 1, 15, 14, 0, tzinfo=UTC)):
            await obs._run_for_chat(
                -1001,
                quiet_start=22,
                quiet_end=6,
            )

        handler.assert_awaited_once()


# ---------------------------------------------------------------------------
# Per-target prompt/ack_token flow through _tick
# ---------------------------------------------------------------------------


class TestPerTargetPromptAckInTick:
    async def test_tick_passes_target_prompt_and_ack(self) -> None:
        """Group target with per-target prompt/ack should pass them to handler."""
        target = HeartbeatTarget(
            chat_id=-1001,
            transport="sl",
            prompt="Check servers",
            ack_token="SERVER_OK",
        )
        config = AgentConfig(
            heartbeat=HeartbeatConfig(
                enabled=True,
                prompt="Global prompt",
                ack_token="HEARTBEAT_OK",
                group_targets=[target],
            ),
            allowed_user_ids=[],
        )
        obs = HeartbeatObserver(config)
        handler = AsyncMock(return_value=None)
        obs.set_heartbeat_handler(handler)

        with time_machine.travel(datetime(2026, 1, 15, 14, 0, tzinfo=UTC)):
            await obs._tick()

        handler.assert_awaited_once_with(-1001, None, "Check servers", "SERVER_OK", "sl")

    async def test_tick_passes_none_for_default_user_targets(self) -> None:
        """User targets (allowed_user_ids) use None prompt/ack (global fallback)."""
        config = AgentConfig(
            heartbeat=HeartbeatConfig(
                enabled=True,
                prompt="Global prompt",
                ack_token="HEARTBEAT_OK",
            ),
            allowed_user_ids=[100],
        )
        obs = HeartbeatObserver(config)
        handler = AsyncMock(return_value=None)
        obs.set_heartbeat_handler(handler)

        with time_machine.travel(datetime(2026, 1, 15, 14, 0, tzinfo=UTC)):
            await obs._tick()

        handler.assert_awaited_once_with(100, None, None, None, "tg")

    async def test_run_for_chat_passes_transport_to_result_handler(self) -> None:
        obs = _make_observer(targets=[])
        obs.set_heartbeat_handler(AsyncMock(return_value="alert"))
        result_handler = AsyncMock()
        obs.set_result_handler(result_handler)

        await obs._run_for_chat(-1001, topic_id=7, transport="sl")

        result_handler.assert_awaited_once_with(-1001, "alert", 7, "sl")


# ---------------------------------------------------------------------------
# Chat validation
# ---------------------------------------------------------------------------


class TestHeartbeatValidation:
    async def test_validated_target_is_cached(self) -> None:
        validator = AsyncMock(return_value=True)
        obs = _make_observer(targets=[HeartbeatTarget(chat_id=-1001)])
        obs.set_chat_validator(validator)

        with time_machine.travel(datetime(2026, 1, 15, 14, 0, tzinfo=UTC)):
            await obs._tick()
            await obs._tick()

        assert validator.await_count == 1

    async def test_invalid_target_is_skipped(self) -> None:
        validator = AsyncMock(return_value=False)
        handler = AsyncMock(return_value=None)
        obs = _make_observer(targets=[HeartbeatTarget(chat_id=-1001)])
        obs.set_heartbeat_handler(handler)
        obs.set_chat_validator(validator)

        with time_machine.travel(datetime(2026, 1, 15, 14, 0, tzinfo=UTC)):
            await obs._tick()

        handler.assert_awaited_once_with(100, None, None, None, "tg")

    async def test_cache_expires_after_one_hour(self) -> None:
        validator = AsyncMock(return_value=True)
        obs = _make_observer(targets=[HeartbeatTarget(chat_id=-1001)])
        obs.set_chat_validator(validator)

        t0 = datetime(2026, 1, 15, 14, 0, tzinfo=UTC)
        with time_machine.travel(t0):
            await obs._tick()
        assert validator.await_count == 1

        t1 = datetime(2026, 1, 15, 15, 1, tzinfo=UTC)
        with time_machine.travel(t1):
            await obs._tick()
        assert validator.await_count == 2

    async def test_user_targets_not_validated(self) -> None:
        """Validation only applies to group_targets, not allowed_user_ids."""
        validator = AsyncMock(return_value=True)
        obs = _make_observer(targets=[])
        obs.set_chat_validator(validator)
        handler = AsyncMock(return_value=None)
        obs.set_heartbeat_handler(handler)

        with time_machine.travel(datetime(2026, 1, 15, 14, 0, tzinfo=UTC)):
            await obs._tick()

        validator.assert_not_awaited()
        handler.assert_awaited_once()

    async def test_no_validator_skips_validation(self) -> None:
        """When no validator is set, group targets run without validation."""
        obs = _make_observer(targets=[HeartbeatTarget(chat_id=-1001)])
        handler = AsyncMock(return_value=None)
        obs.set_heartbeat_handler(handler)

        with time_machine.travel(datetime(2026, 1, 15, 14, 0, tzinfo=UTC)):
            await obs._tick()

        assert handler.await_count == 2


# ---------------------------------------------------------------------------
# Per-target interval
# ---------------------------------------------------------------------------


class TestPerTargetInterval:
    async def test_target_with_custom_interval_gets_own_loop(self) -> None:
        """A target with interval_minutes runs independently, not in global _tick."""
        target = HeartbeatTarget(chat_id=-1001, interval_minutes=60)
        config = AgentConfig(
            heartbeat=HeartbeatConfig(
                enabled=True,
                interval_minutes=30,
                group_targets=[target],
            ),
            allowed_user_ids=[],
        )
        obs = HeartbeatObserver(config)
        handler = AsyncMock(return_value=None)
        obs.set_heartbeat_handler(handler)
        obs._start_target_loops()

        assert ("tg", -1001, None) in obs._target_tasks

        with time_machine.travel(datetime(2026, 1, 15, 14, 0, tzinfo=UTC)):
            await obs._tick()
        handler.assert_not_awaited()

    async def test_target_without_interval_runs_every_tick(self) -> None:
        """A target without a custom interval runs on every tick."""
        target = HeartbeatTarget(chat_id=-1001)
        config = AgentConfig(
            heartbeat=HeartbeatConfig(
                enabled=True,
                group_targets=[target],
            ),
            allowed_user_ids=[],
        )
        obs = HeartbeatObserver(config)
        handler = AsyncMock(return_value=None)
        obs.set_heartbeat_handler(handler)

        with time_machine.travel(datetime(2026, 1, 15, 14, 0, tzinfo=UTC)):
            await obs._tick()
        assert handler.await_count == 1

        with time_machine.travel(datetime(2026, 1, 15, 14, 30, tzinfo=UTC)):
            handler.reset_mock()
            await obs._tick()
        assert handler.await_count == 1
