"""Integration tests: CronObserver quiet hour behaviour during job execution."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import time_machine

from ductor_bot.cli.codex_cache import CodexModelCache
from ductor_bot.config import AgentConfig, HeartbeatConfig, ModelRegistry
from ductor_bot.cron.manager import CronJob, CronManager
from ductor_bot.cron.observer import CronObserver
from ductor_bot.workspace.paths import DuctorPaths

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_paths(tmp_path: Path) -> DuctorPaths:
    fw = tmp_path / "fw"
    paths = DuctorPaths(
        ductor_home=tmp_path / "home",
        home_defaults=fw / "workspace",
        framework_root=fw,
    )
    paths.cron_tasks_dir.mkdir(parents=True)
    return paths


def _make_manager(paths: DuctorPaths) -> CronManager:
    return CronManager(jobs_path=paths.cron_jobs_path)


def _make_config(**overrides: Any) -> AgentConfig:
    return AgentConfig(**overrides)


def _make_models() -> ModelRegistry:
    return ModelRegistry()


def _make_codex_cache() -> CodexModelCache:
    return CodexModelCache(last_updated=datetime.now(UTC).isoformat(), models=[])


def _make_observer(
    paths: DuctorPaths,
    mgr: CronManager,
    **config_overrides: Any,
) -> CronObserver:
    return CronObserver(
        paths,
        mgr,
        config=_make_config(**config_overrides),
        models=_make_models(),
        codex_cache=_make_codex_cache(),
    )


def _add_job(mgr: CronManager, paths: DuctorPaths, **overrides: Any) -> CronJob:
    defaults: dict[str, Any] = {
        "id": "test-job",
        "title": "Test Job",
        "description": "A test job",
        "schedule": "* * * * *",
        "task_folder": "test_task",
        "agent_instruction": "do something",
    }
    defaults.update(overrides)
    job = CronJob(**defaults)
    mgr.add_job(job)
    (paths.cron_tasks_dir / defaults["task_folder"]).mkdir(exist_ok=True)
    return job


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@time_machine.travel("2025-06-15T23:30:00+00:00")
async def test_cron_skips_during_global_quiet_hours(tmp_path: Path) -> None:
    """Job is skipped when current hour falls within global quiet hours (21-8)."""
    paths = _make_paths(tmp_path)
    mgr = _make_manager(paths)
    obs = _make_observer(
        paths,
        mgr,
        user_timezone="UTC",
        heartbeat=HeartbeatConfig(quiet_start=21, quiet_end=8),
    )
    _add_job(mgr, paths)

    result_handler = AsyncMock()
    obs.set_result_handler(result_handler)

    await obs._execute_job("test-job", "do something", "test_task")

    # Result handler NOT called because job was skipped
    result_handler.assert_not_awaited()


@time_machine.travel("2025-06-15T14:00:00+00:00")
async def test_cron_runs_during_active_hours(tmp_path: Path) -> None:
    """Job runs when current hour is outside global quiet hours."""
    paths = _make_paths(tmp_path)
    mgr = _make_manager(paths)
    obs = _make_observer(
        paths,
        mgr,
        user_timezone="UTC",
        heartbeat=HeartbeatConfig(quiet_start=21, quiet_end=8),
    )
    _add_job(mgr, paths)

    result_handler = AsyncMock()
    obs.set_result_handler(result_handler)

    with patch("ductor_bot.cron.observer.build_cmd", return_value=None):
        await obs._execute_job("test-job", "do something", "test_task")

    # Job reached execution (build_cmd returned None -> error:cli_not_found)
    # but was NOT skipped by quiet hours
    job = mgr.get_job("test-job")
    assert job is not None
    assert job.last_run_status is not None
    assert "cli_not_found" in job.last_run_status


@time_machine.travel("2025-06-15T14:00:00+00:00")
async def test_cron_respects_task_specific_quiet_hours(tmp_path: Path) -> None:
    """Job uses its own quiet_start/quiet_end, overriding global config."""
    paths = _make_paths(tmp_path)
    mgr = _make_manager(paths)
    obs = _make_observer(
        paths,
        mgr,
        user_timezone="UTC",
        heartbeat=HeartbeatConfig(quiet_start=21, quiet_end=8),
    )
    # Task-specific quiet hours: 10-16 (14:00 is quiet)
    _add_job(mgr, paths, quiet_start=10, quiet_end=16)

    result_handler = AsyncMock()
    obs.set_result_handler(result_handler)

    await obs._execute_job("test-job", "do something", "test_task")

    # Skipped because 14:00 is within task-specific quiet hours 10-16
    result_handler.assert_not_awaited()


@time_machine.travel("2025-06-15T21:00:00+00:00")
async def test_cron_quiet_hours_boundary_start(tmp_path: Path) -> None:
    """Start hour is inclusive: hour 21 IS quiet for global 21-8."""
    paths = _make_paths(tmp_path)
    mgr = _make_manager(paths)
    obs = _make_observer(
        paths,
        mgr,
        user_timezone="UTC",
        heartbeat=HeartbeatConfig(quiet_start=21, quiet_end=8),
    )
    _add_job(mgr, paths)

    result_handler = AsyncMock()
    obs.set_result_handler(result_handler)

    await obs._execute_job("test-job", "do something", "test_task")

    # Skipped because hour 21 is the inclusive start boundary
    result_handler.assert_not_awaited()


@time_machine.travel("2025-06-15T08:00:00+00:00")
async def test_cron_quiet_hours_boundary_end(tmp_path: Path) -> None:
    """End hour is exclusive: hour 8 is NOT quiet for global 21-8."""
    paths = _make_paths(tmp_path)
    mgr = _make_manager(paths)
    obs = _make_observer(
        paths,
        mgr,
        user_timezone="UTC",
        heartbeat=HeartbeatConfig(quiet_start=21, quiet_end=8),
    )
    _add_job(mgr, paths)

    result_handler = AsyncMock()
    obs.set_result_handler(result_handler)

    with patch("ductor_bot.cron.observer.build_cmd", return_value=None):
        await obs._execute_job("test-job", "do something", "test_task")

    # NOT skipped because hour 8 is the exclusive end boundary
    job = mgr.get_job("test-job")
    assert job is not None
    assert job.last_run_status is not None
    assert "cli_not_found" in job.last_run_status


@time_machine.travel("2025-06-15T14:00:00+00:00")
async def test_cron_quiet_hours_disabled(tmp_path: Path) -> None:
    """Quiet hours disabled (start==end) means the job always runs."""
    paths = _make_paths(tmp_path)
    mgr = _make_manager(paths)
    obs = _make_observer(
        paths,
        mgr,
        user_timezone="UTC",
        heartbeat=HeartbeatConfig(quiet_start=0, quiet_end=0),
    )
    _add_job(mgr, paths)

    result_handler = AsyncMock()
    obs.set_result_handler(result_handler)

    with patch("ductor_bot.cron.observer.build_cmd", return_value=None):
        await obs._execute_job("test-job", "do something", "test_task")

    # Job proceeded past quiet hours check (build_cmd returned None -> error)
    job = mgr.get_job("test-job")
    assert job is not None
    assert job.last_run_status is not None
    assert "cli_not_found" in job.last_run_status
