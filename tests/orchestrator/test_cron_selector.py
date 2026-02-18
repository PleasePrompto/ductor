"""Tests for the interactive cron selector wizard."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from ductor_bot.cron.manager import CronJob
from ductor_bot.orchestrator.core import Orchestrator
from ductor_bot.orchestrator.cron_selector import (
    cron_selector_start,
    handle_cron_callback,
    is_cron_selector_callback,
)


def _add_job(
    orch: Orchestrator,
    *,
    job_id: str,
    title: str,
    schedule: str = "0 9 * * *",
    enabled: bool = True,
) -> None:
    orch._cron_manager.add_job(
        CronJob(
            id=job_id,
            title=title,
            description=f"{title} description",
            schedule=schedule,
            task_folder=job_id,
            agent_instruction="run task",
            enabled=enabled,
        )
    )


def test_is_cron_selector_callback() -> None:
    assert is_cron_selector_callback("crn:r:0") is True
    assert is_cron_selector_callback("crn:t:0:0:abcd1234") is True
    assert is_cron_selector_callback("ms:p:claude") is False


async def test_start_no_jobs(orch: Orchestrator) -> None:
    text, keyboard = await cron_selector_start(orch)
    assert "No cron jobs configured" in text
    assert keyboard is None


async def test_start_lists_jobs_with_keyboard(orch: Orchestrator) -> None:
    _add_job(orch, job_id="daily", title="Daily Report")

    text, keyboard = await cron_selector_start(orch)

    assert "Daily Report" in text
    assert "0 9 * * *" in text
    assert keyboard is not None


async def test_toggle_job_updates_enabled_flag(orch: Orchestrator) -> None:
    _add_job(orch, job_id="daily", title="Daily Report", enabled=True)
    observer = MagicMock()
    observer.reschedule_now = AsyncMock()
    object.__setattr__(orch, "_cron_observer", observer)

    _text, keyboard = await cron_selector_start(orch)
    assert keyboard is not None
    callback_data = keyboard.inline_keyboard[0][0].callback_data
    assert callback_data is not None

    text, _ = await handle_cron_callback(orch, callback_data)

    job = orch._cron_manager.get_job("daily")
    assert job is not None
    assert job.enabled is False
    observer.reschedule_now.assert_awaited_once()
    assert "disabled" in text


async def test_toggle_with_stale_fingerprint_is_ignored(orch: Orchestrator) -> None:
    _add_job(orch, job_id="daily", title="Daily Report", enabled=True)

    text, _ = await handle_cron_callback(orch, "crn:t:0:0:deadbeef")

    job = orch._cron_manager.get_job("daily")
    assert job is not None
    assert job.enabled is True
    assert "Cron list changed" in text


async def test_bulk_enable_disable(orch: Orchestrator) -> None:
    _add_job(orch, job_id="job-1", title="Job One", enabled=True)
    _add_job(orch, job_id="job-2", title="Job Two", enabled=False)

    await handle_cron_callback(orch, "crn:af:0")
    assert all(not j.enabled for j in orch._cron_manager.list_jobs())

    await handle_cron_callback(orch, "crn:ao:0")
    assert all(j.enabled for j in orch._cron_manager.list_jobs())
