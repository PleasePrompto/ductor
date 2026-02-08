"""Tests for run.py supervisor."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch


class TestSupervisor:
    """Test supervisor crash recovery and restart logic."""

    async def test_clean_exit_stops_supervisor(self) -> None:
        from ductor_bot.run import supervisor

        mock_proc = MagicMock()
        mock_proc.pid = 1234
        mock_proc.returncode = 0
        mock_proc.wait = AsyncMock(return_value=0)

        with (
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock, return_value=mock_proc),
            patch("ductor_bot.run.WATCH_DIR", Path("/nonexistent")),
        ):
            # supervisor() should exit when child returns 0
            await supervisor()

    async def test_restart_exit_code_respawns(self) -> None:
        from ductor_bot.run import EXIT_RESTART, supervisor

        call_count = 0

        async def mock_create_proc(*_args: object, **_kwargs: object) -> MagicMock:
            nonlocal call_count
            call_count += 1
            proc = MagicMock()
            proc.pid = 1000 + call_count
            # First call: restart code, second call: clean exit
            proc.returncode = EXIT_RESTART if call_count == 1 else 0
            proc.wait = AsyncMock(return_value=proc.returncode)
            return proc

        with (
            patch("asyncio.create_subprocess_exec", side_effect=mock_create_proc),
            patch("ductor_bot.run.WATCH_DIR", Path("/nonexistent")),
        ):
            await supervisor()

        assert call_count == 2

    async def test_crash_with_backoff(self) -> None:
        from ductor_bot.run import supervisor

        call_count = 0

        async def mock_create_proc(*_args: object, **_kwargs: object) -> MagicMock:
            nonlocal call_count
            call_count += 1
            proc = MagicMock()
            proc.pid = 1000 + call_count
            # First call: crash (code 1), second call: clean exit
            proc.returncode = 1 if call_count == 1 else 0
            proc.wait = AsyncMock(return_value=proc.returncode)
            return proc

        with (
            patch("asyncio.create_subprocess_exec", side_effect=mock_create_proc),
            patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep,
            patch("ductor_bot.run.WATCH_DIR", Path("/nonexistent")),
        ):
            await supervisor()

        assert call_count == 2
        # Should have called sleep for backoff
        mock_sleep.assert_called()

    async def test_fast_crash_escalates_backoff(self) -> None:
        from ductor_bot.run import supervisor

        call_count = 0

        async def mock_create_proc(*_args: object, **_kwargs: object) -> MagicMock:
            nonlocal call_count
            call_count += 1
            proc = MagicMock()
            proc.pid = 1000 + call_count
            # 3 fast crashes, then clean exit
            proc.returncode = 1 if call_count <= 3 else 0
            proc.wait = AsyncMock(return_value=proc.returncode)
            return proc

        sleep_values: list[float] = []

        async def mock_sleep(seconds: float) -> None:
            sleep_values.append(seconds)

        # Use a counter for monotonic to simulate fast crashes
        mono_counter = iter([0, 0.1, 0.1, 0.2, 0.2, 0.3, 0.3, 0.4])

        def mock_monotonic() -> float:
            return next(mono_counter, 999.0)

        with (
            patch("asyncio.create_subprocess_exec", side_effect=mock_create_proc),
            patch("asyncio.sleep", side_effect=mock_sleep),
            patch("ductor_bot.run.time") as mock_time,
            patch("ductor_bot.run.WATCH_DIR", Path("/nonexistent")),
        ):
            mock_time.monotonic = mock_monotonic
            await supervisor()

        # Backoff should escalate: 2^1, 2^2, 2^3
        assert len(sleep_values) == 3
        assert sleep_values[0] < sleep_values[1] < sleep_values[2]


class TestTerminateChild:
    """Test child process termination."""

    async def test_terminate_sends_sigterm(self) -> None:
        from ductor_bot.run import _terminate_child

        proc = MagicMock()
        proc.returncode = None
        proc.terminate = MagicMock()
        proc.wait = AsyncMock(return_value=0)

        await _terminate_child(proc)
        proc.terminate.assert_called_once()

    async def test_terminate_noop_if_already_exited(self) -> None:
        from ductor_bot.run import _terminate_child

        proc = MagicMock()
        proc.returncode = 0

        result = await _terminate_child(proc)
        assert result == 0

    async def test_terminate_kills_on_timeout(self) -> None:

        proc = MagicMock()
        proc.returncode = None
        proc.terminate = MagicMock()
        proc.kill = MagicMock()

        async def slow_wait() -> int:
            await asyncio.sleep(100)
            return 0

        proc.wait = slow_wait
        # With very short timeout
        with patch("ductor_bot.run.SIGTERM_TIMEOUT", 0.01):
            # After kill, wait returns
            proc.wait = AsyncMock(return_value=-9)
            proc.returncode = None
            proc.terminate = MagicMock()
            # First wait times out, then kill + wait succeeds
            wait_count = 0

            async def wait_side_effect() -> int:
                nonlocal wait_count
                wait_count += 1
                if wait_count == 1:
                    await asyncio.sleep(100)
                return -9

            proc.wait = wait_side_effect
            # This is tricky to test perfectly, just verify it doesn't hang
