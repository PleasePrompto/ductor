"""Cron job management: JSON-based persistence.

Jobs are stored in a JSON file. The CronObserver watches the file
for changes and schedules jobs in-process.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ductor_bot.infra.json_store import atomic_json_save, load_json

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

    # Delivery tracking (#160): execution success and delivery success are
    # separate. On delivery failure the full result text is kept for resend.
    last_delivery_status: str | None = None  # "ok" | "failed" | "skipped" | None
    last_delivery_error: str = ""
    last_result_text: str | None = None
    delivery_retry_attempts: int = 0
    next_delivery_retry_at: str | None = None

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

    # Routing: deliver results to the chat/topic where the job was created
    chat_id: int = 0
    topic_id: int | None = None
    transport: str = "tg"

    # Mute delivery on the success path; errors are still delivered (#133)
    silent_on_success: bool = False

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
            "chat_id": self.chat_id,
            "topic_id": self.topic_id,
            "transport": self.transport,
            "silent_on_success": self.silent_on_success,
        }
        if self.timezone:
            result["timezone"] = self.timezone
        if self.last_delivery_status is not None:
            result["last_delivery_status"] = self.last_delivery_status
        if self.last_delivery_error:
            result["last_delivery_error"] = self.last_delivery_error
        if self.last_result_text is not None:
            result["last_result_text"] = self.last_result_text
        if self.delivery_retry_attempts:
            result["delivery_retry_attempts"] = self.delivery_retry_attempts
        if self.next_delivery_retry_at is not None:
            result["next_delivery_retry_at"] = self.next_delivery_retry_at
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
            last_delivery_status=data.get("last_delivery_status"),
            last_delivery_error=data.get("last_delivery_error", ""),
            last_result_text=data.get("last_result_text"),
            delivery_retry_attempts=data.get("delivery_retry_attempts", 0),
            next_delivery_retry_at=data.get("next_delivery_retry_at"),
            provider=data.get("provider"),
            model=data.get("model"),
            reasoning_effort=data.get("reasoning_effort"),
            cli_parameters=data.get("cli_parameters", []),
            quiet_start=data.get("quiet_start"),
            quiet_end=data.get("quiet_end"),
            dependency=data.get("dependency"),
            chat_id=data.get("chat_id", 0),
            topic_id=data.get("topic_id"),
            transport=data.get("transport", "tg"),
            silent_on_success=data.get("silent_on_success", False),
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

    def set_enabled(self, job_id: str, *, enabled: bool) -> bool:
        """Set ``enabled`` for one job. Returns True if state changed."""
        job = self.get_job(job_id)
        if job is None:
            return False
        if job.enabled == enabled:
            return False
        job.enabled = enabled
        self._save()
        logger.info("Cron job %s: enabled=%s", job_id, enabled)
        return True

    def set_all_enabled(self, *, enabled: bool) -> int:
        """Set ``enabled`` for all jobs. Returns number of changed jobs."""
        changed = 0
        for job in self._jobs:
            if job.enabled != enabled:
                job.enabled = enabled
                changed += 1
        if changed:
            self._save()
            logger.info("Cron jobs bulk update: enabled=%s changed=%d", enabled, changed)
        return changed

    def update_run_status(
        self,
        job_id: str,
        *,
        status: str,
        delivery_status: str | None = None,
        delivery_error: str = "",
        result_text: str | None = None,
    ) -> None:
        """Update last run and delivery tracking for a job (#160).

        *result_text* is only passed on delivery failure so the original
        output can be resent without re-running the job.
        """
        job = self.get_job(job_id)
        if job is None:
            return
        job.last_run_at = datetime.now(UTC).isoformat()
        job.last_run_status = status
        job.last_delivery_status = delivery_status
        job.last_delivery_error = delivery_error
        job.last_result_text = result_text
        job.delivery_retry_attempts = 0
        job.next_delivery_retry_at = None
        self._save()

    def update_delivery_retry(
        self,
        job_id: str,
        *,
        delivered: bool,
        delivery_error: str = "",
        next_attempt_at: str | None = None,
        expected_text: str | None = None,
    ) -> None:
        """Persist one delivery retry without changing the job execution status.

        When *expected_text* is given, a successful retry only clears the
        preserved result if it still matches — a newer failed result that
        landed while the retry was in flight is kept for the next sweep.
        """
        job = self.get_job(job_id)
        if job is None:
            return
        if delivered:
            if expected_text is not None and job.last_result_text != expected_text:
                return
            job.last_delivery_status = "ok"
            job.last_delivery_error = ""
            job.last_result_text = None
            job.delivery_retry_attempts = 0
            job.next_delivery_retry_at = None
        else:
            job.last_delivery_status = "failed"
            job.last_delivery_error = delivery_error
            job.delivery_retry_attempts += 1
            job.next_delivery_retry_at = next_attempt_at
        self._save()

    def reload(self) -> None:
        """Re-read jobs from disk (called by CronObserver on file change)."""
        self._jobs = self._load()

    # -- Persistence --

    def _load(self) -> list[CronJob]:
        """Load jobs from JSON file."""
        data = load_json(self._jobs_path)
        if data is None:
            return []
        try:
            jobs = [CronJob.from_dict(j) for j in data.get("jobs", [])]
        except (KeyError, TypeError):
            logger.warning("Corrupt cron jobs file: %s", self._jobs_path)
            return []
        for j in jobs:
            logger.debug("Job loaded id=%s title=%s enabled=%s", j.id, j.title, j.enabled)
        return jobs

    def _save(self) -> None:
        """Save jobs to JSON file atomically (temp write + rename)."""
        atomic_json_save(self._jobs_path, {"jobs": [j.to_dict() for j in self._jobs]})
