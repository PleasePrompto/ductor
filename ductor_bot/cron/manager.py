"""Cron job management: JSON-based persistence.

Jobs are stored in a JSON file. The CronObserver watches the file
for changes and schedules jobs in-process.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


@dataclass
class CronJob:
    """A scheduled job definition."""

    id: str
    title: str
    description: str
    schedule: str
    task_folder: str
    agent_instruction: str
    enabled: bool = True
    timezone: str = ""
    created_at: str = ""
    last_run_at: str | None = None
    last_run_status: str | None = None

    # Per-task execution overrides
    provider: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    cli_parameters: list[str] = field(default_factory=list)

    # Quiet hours (None = use global config defaults)
    quiet_start: int | None = None
    quiet_end: int | None = None

    # Optional dependency for sequential execution
    dependency: str | None = None

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = datetime.now(UTC).isoformat()

    def to_dict(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "schedule": self.schedule,
            "task_folder": self.task_folder,
            "agent_instruction": self.agent_instruction,
            "enabled": self.enabled,
            "created_at": self.created_at,
            "last_run_at": self.last_run_at,
            "last_run_status": self.last_run_status,
            "provider": self.provider,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "cli_parameters": self.cli_parameters,
            "quiet_start": self.quiet_start,
            "quiet_end": self.quiet_end,
            "dependency": self.dependency,
        }
        if self.timezone:
            result["timezone"] = self.timezone
        return result

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> CronJob:
        return cls(
            id=data["id"],
            title=data["title"],
            description=data.get("description", ""),
            schedule=data["schedule"],
            task_folder=data["task_folder"],
            agent_instruction=data["agent_instruction"],
            enabled=data.get("enabled", True),
            timezone=data.get("timezone", ""),
            created_at=data.get("created_at", ""),
            last_run_at=data.get("last_run_at"),
            last_run_status=data.get("last_run_status"),
            provider=data.get("provider"),
            model=data.get("model"),
            reasoning_effort=data.get("reasoning_effort"),
            cli_parameters=data.get("cli_parameters", []),
            quiet_start=data.get("quiet_start"),
            quiet_end=data.get("quiet_end"),
            dependency=data.get("dependency"),
        )


class CronManager:
    """Manages cron jobs: JSON persistence.

    The CronObserver watches the JSON file for changes and handles
    scheduling. This class is responsible for data only.
    """

    def __init__(self, *, jobs_path: Path) -> None:
        self._jobs_path = jobs_path
        self._jobs: list[CronJob] = self._load()

    # -- CRUD --

    def add_job(self, job: CronJob) -> None:
        """Add a new job. Raises ValueError if ID already exists."""
        if any(j.id == job.id for j in self._jobs):
            msg = f"Job '{job.id}' already exists"
            raise ValueError(msg)
        self._jobs.append(job)
        self._save()
        logger.info("Cron job added: %s (%s)", job.id, job.schedule)

    def remove_job(self, job_id: str) -> bool:
        """Remove a job by ID. Returns False if not found."""
        before = len(self._jobs)
        self._jobs = [j for j in self._jobs if j.id != job_id]
        if len(self._jobs) == before:
            return False
        self._save()
        logger.info("Cron job removed: %s", job_id)
        return True

    def list_jobs(self) -> list[CronJob]:
        """Return all jobs."""
        return list(self._jobs)

    def get_job(self, job_id: str) -> CronJob | None:
        """Return a job by ID, or None."""
        return next((j for j in self._jobs if j.id == job_id), None)

    def update_run_status(self, job_id: str, *, status: str) -> None:
        """Update last_run_at and last_run_status for a job."""
        job = self.get_job(job_id)
        if job is None:
            return
        job.last_run_at = datetime.now(UTC).isoformat()
        job.last_run_status = status
        self._save()

    def reload(self) -> None:
        """Re-read jobs from disk (called by CronObserver on file change)."""
        self._jobs = self._load()

    # -- Persistence --

    def _load(self) -> list[CronJob]:
        """Load jobs from JSON file."""
        if not self._jobs_path.exists():
            return []
        try:
            data = json.loads(self._jobs_path.read_text(encoding="utf-8"))
            jobs = [CronJob.from_dict(j) for j in data.get("jobs", [])]
        except (json.JSONDecodeError, KeyError, TypeError):
            logger.warning("Corrupt cron jobs file: %s", self._jobs_path)
            return []
        for j in jobs:
            logger.debug("Job loaded id=%s title=%s enabled=%s", j.id, j.title, j.enabled)
        return jobs

    def _save(self) -> None:
        """Save jobs to JSON file atomically (temp write + rename)."""
        self._jobs_path.parent.mkdir(parents=True, exist_ok=True)
        data = {"jobs": [j.to_dict() for j in self._jobs]}
        content = json.dumps(data, indent=2, ensure_ascii=False) + "\n"
        fd, tmp_path = tempfile.mkstemp(dir=str(self._jobs_path.parent), suffix=".tmp")
        tmp = Path(tmp_path)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(content)
            tmp.replace(self._jobs_path)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
