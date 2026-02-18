"""Tests for CronManager: JSON-based job storage (no crontab sync)."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest

from ductor_bot.cron.manager import CronJob, CronManager


def _make_manager(tmp_path: Path) -> CronManager:
    jobs_path = tmp_path / "cron_jobs.json"
    return CronManager(jobs_path=jobs_path)


def _make_job(job_id: str = "daily", **overrides: Any) -> CronJob:
    defaults: dict[str, Any] = {
        "id": job_id,
        "title": "Daily Report",
        "description": "Generate report",
        "schedule": "0 9 * * *",
        "task_folder": f"{job_id}-task",
        "agent_instruction": "Do the daily work",
    }
    defaults.update(overrides)
    return CronJob(**defaults)


# -- CronJob model --


class TestCronJob:
    def test_to_dict(self) -> None:
        job = _make_job()
        d = job.to_dict()
        assert d["id"] == "daily"
        assert d["schedule"] == "0 9 * * *"
        assert d["enabled"] is True

    def test_from_dict(self) -> None:
        data = {
            "id": "test",
            "title": "Test",
            "description": "desc",
            "schedule": "*/5 * * * *",
            "task_folder": "test-task",
            "agent_instruction": "do stuff",
            "enabled": False,
            "created_at": "2025-01-01T00:00:00Z",
        }
        job = CronJob.from_dict(data)
        assert job.id == "test"
        assert job.enabled is False
        assert job.schedule == "*/5 * * * *"

    def test_from_dict_defaults(self) -> None:
        data = {
            "id": "min",
            "title": "Min",
            "description": "",
            "schedule": "0 * * * *",
            "task_folder": "min",
            "agent_instruction": "go",
        }
        job = CronJob.from_dict(data)
        assert job.enabled is True
        assert job.last_run_at is None

    def test_auto_created_at(self) -> None:
        job = _make_job()
        assert job.created_at != ""


# -- CronManager CRUD --


class TestCronManagerCRUD:
    def test_add_job_saves_to_json(self, tmp_path: Path) -> None:
        mgr = _make_manager(tmp_path)
        mgr.add_job(_make_job())

        data = json.loads(mgr._jobs_path.read_text())
        assert len(data["jobs"]) == 1
        assert data["jobs"][0]["id"] == "daily"

    def test_add_duplicate_raises(self, tmp_path: Path) -> None:
        mgr = _make_manager(tmp_path)
        mgr.add_job(_make_job())
        with pytest.raises(ValueError, match="already exists"):
            mgr.add_job(_make_job())

    def test_remove_job(self, tmp_path: Path) -> None:
        mgr = _make_manager(tmp_path)
        mgr.add_job(_make_job())
        removed = mgr.remove_job("daily")

        assert removed is True
        data = json.loads(mgr._jobs_path.read_text())
        assert len(data["jobs"]) == 0

    def test_remove_nonexistent_returns_false(self, tmp_path: Path) -> None:
        mgr = _make_manager(tmp_path)
        assert mgr.remove_job("nope") is False

    def test_list_jobs(self, tmp_path: Path) -> None:
        mgr = _make_manager(tmp_path)
        for i in range(3):
            mgr.add_job(_make_job(f"job-{i}"))

        jobs = mgr.list_jobs()
        assert len(jobs) == 3
        assert [j.id for j in jobs] == ["job-0", "job-1", "job-2"]

    def test_get_job(self, tmp_path: Path) -> None:
        mgr = _make_manager(tmp_path)
        mgr.add_job(_make_job())

        found = mgr.get_job("daily")
        assert found is not None
        assert found.title == "Daily Report"

    def test_get_nonexistent_returns_none(self, tmp_path: Path) -> None:
        mgr = _make_manager(tmp_path)
        assert mgr.get_job("nope") is None

    def test_update_run_status(self, tmp_path: Path) -> None:
        mgr = _make_manager(tmp_path)
        mgr.add_job(_make_job())

        mgr.update_run_status("daily", status="success")
        updated = mgr.get_job("daily")
        assert updated is not None
        assert updated.last_run_status == "success"
        assert updated.last_run_at is not None

    def test_set_enabled_updates_single_job(self, tmp_path: Path) -> None:
        mgr = _make_manager(tmp_path)
        mgr.add_job(_make_job())

        changed = mgr.set_enabled("daily", enabled=False)

        assert changed is True
        job = mgr.get_job("daily")
        assert job is not None
        assert job.enabled is False

    def test_set_enabled_no_change_returns_false(self, tmp_path: Path) -> None:
        mgr = _make_manager(tmp_path)
        mgr.add_job(_make_job())

        changed = mgr.set_enabled("daily", enabled=True)
        assert changed is False

    def test_set_all_enabled_updates_multiple_jobs(self, tmp_path: Path) -> None:
        mgr = _make_manager(tmp_path)
        mgr.add_job(_make_job("job-1", enabled=True))
        mgr.add_job(_make_job("job-2", enabled=False))

        changed = mgr.set_all_enabled(enabled=False)

        assert changed == 1
        jobs = mgr.list_jobs()
        assert all(not job.enabled for job in jobs)

    def test_reload_picks_up_external_changes(self, tmp_path: Path) -> None:
        mgr = _make_manager(tmp_path)
        mgr.add_job(_make_job("original"))
        assert len(mgr.list_jobs()) == 1

        # Simulate external write (e.g., from cron_add.py tool)
        data = {
            "jobs": [
                _make_job("original").to_dict(),
                _make_job("external").to_dict(),
            ],
        }
        mgr._jobs_path.write_text(json.dumps(data), encoding="utf-8")

        mgr.reload()
        assert len(mgr.list_jobs()) == 2
        assert mgr.get_job("external") is not None


# -- No subprocess calls --


class TestNoSubprocessCalls:
    def test_no_crontab_calls_on_add(self, tmp_path: Path) -> None:
        mgr = _make_manager(tmp_path)
        with patch("subprocess.run") as mock_run:
            mgr.add_job(_make_job())
        mock_run.assert_not_called()

    def test_no_crontab_calls_on_remove(self, tmp_path: Path) -> None:
        mgr = _make_manager(tmp_path)
        mgr.add_job(_make_job())
        with patch("subprocess.run") as mock_run:
            mgr.remove_job("daily")
        mock_run.assert_not_called()


# -- Persistence --


class TestPersistence:
    def test_loads_from_existing_json(self, tmp_path: Path) -> None:
        jobs_path = tmp_path / "cron_jobs.json"
        data = {
            "jobs": [
                {
                    "id": "existing",
                    "title": "Existing",
                    "description": "Was saved before",
                    "schedule": "0 * * * *",
                    "task_folder": "existing-task",
                    "agent_instruction": "do stuff",
                    "enabled": True,
                },
            ],
        }
        jobs_path.write_text(json.dumps(data))

        mgr = CronManager(jobs_path=jobs_path)
        jobs = mgr.list_jobs()
        assert len(jobs) == 1
        assert jobs[0].id == "existing"

    def test_handles_missing_json_file(self, tmp_path: Path) -> None:
        mgr = _make_manager(tmp_path)
        assert mgr.list_jobs() == []

    def test_handles_corrupt_json_file(self, tmp_path: Path) -> None:
        jobs_path = tmp_path / "cron_jobs.json"
        jobs_path.write_text("not valid json{{{")

        mgr = CronManager(jobs_path=jobs_path)
        assert mgr.list_jobs() == []
