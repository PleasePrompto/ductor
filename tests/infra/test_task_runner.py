"""Tests for run_oneshot_task .env forwarding into one_shot.env_overrides."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, patch

from ductor_bot.cli.param_resolver import CLIRunConfig
from ductor_bot.cron.execution import OneShotCommand, OneShotExecutionResult
from ductor_bot.infra.env_secrets import clear_cache
from ductor_bot.infra.oneshot_runner import RunOptions, run_oneshot_task

if TYPE_CHECKING:
    import pytest


def _exec_config() -> CLIRunConfig:
    return CLIRunConfig(
        provider="claude",
        model="opus",
        reasoning_effort="",
        cli_parameters=[],
        permission_mode="bypassPermissions",
        working_dir="/tmp",
        file_access="all",
    )


def _exec_result() -> OneShotExecutionResult:
    return OneShotExecutionResult(
        status="success",
        result_text="ok",
        stdout=b"",
        stderr=b"",
        returncode=0,
        timed_out=False,
    )


async def _run_and_capture(
    tmp_path: Path,
) -> dict[str, str]:
    """Drive run_oneshot_task with a fixed OneShotCommand and capture env_overrides."""
    captured: list[dict[str, str]] = []
    cmd = OneShotCommand(cmd=["/usr/bin/claude", "-p", "--", "hi"])

    async def fake_exec(one_shot: OneShotCommand, **_: object) -> OneShotExecutionResult:
        captured.append(dict(one_shot.env_overrides))
        return _exec_result()

    with (
        patch("ductor_bot.cron.execution.build_cmd", return_value=cmd),
        patch(
            "ductor_bot.cron.execution.execute_one_shot",
            new=AsyncMock(side_effect=fake_exec),
        ),
    ):
        await run_oneshot_task(
            _exec_config(),
            "hi",
            RunOptions(
                cwd=tmp_path,
                timeout_seconds=60,
                timeout_label="test",
                ductor_home=tmp_path,
            ),
        )

    assert len(captured) == 1
    return captured[0]


class TestDotenvForwarding:
    """run_oneshot_task merges ~/.ductor/.env into one_shot.env_overrides."""

    async def test_forwards_keys_not_in_environ(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / ".env").write_text("DUCTOR_PR_TEST_DOTENV_KEY=from-file\n")
        monkeypatch.delenv("DUCTOR_PR_TEST_DOTENV_KEY", raising=False)
        clear_cache()

        overrides = await _run_and_capture(tmp_path)

        assert overrides["DUCTOR_PR_TEST_DOTENV_KEY"] == "from-file"
        assert overrides["DUCTOR_HOME"] == str(tmp_path)

    async def test_existing_environ_key_not_overridden(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / ".env").write_text("PRESET=from-file\n")
        monkeypatch.setenv("PRESET", "from-process")
        clear_cache()

        overrides = await _run_and_capture(tmp_path)

        assert "PRESET" not in overrides
        assert overrides["DUCTOR_HOME"] == str(tmp_path)

    async def test_ductor_home_not_overridden_by_dotenv(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        (tmp_path / ".env").write_text("DUCTOR_HOME=/wrong\n")
        monkeypatch.delenv("DUCTOR_HOME", raising=False)
        clear_cache()

        overrides = await _run_and_capture(tmp_path)

        assert overrides["DUCTOR_HOME"] == str(tmp_path)
