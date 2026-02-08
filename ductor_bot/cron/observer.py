"""In-process cron job scheduler: watches cron_jobs.json, schedules and executes jobs."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import TYPE_CHECKING

from cronsim import CronSim, CronSimError

from ductor_bot.config import resolve_user_timezone
from ductor_bot.cron.execution import (
    build_cmd,
    enrich_instruction,
    parse_claude_result,
    parse_codex_result,
)
from ductor_bot.cron.manager import CronManager
from ductor_bot.log_context import set_log_context

if TYPE_CHECKING:
    from ductor_bot.config import AgentConfig, ModelRegistry
    from ductor_bot.workspace.paths import DuctorPaths

logger = logging.getLogger(__name__)

# Callback signature: (job_title, result_text, status)
CronResultCallback = Callable[[str, str, str], Awaitable[None]]


class CronObserver:
    """Watches cron_jobs.json and schedules jobs in-process.

    On start: reads all jobs, calculates next run times via cronsim,
    and schedules asyncio tasks. A background watcher polls the JSON
    file's mtime every 5 seconds; on change it reloads and reschedules.
    """

    def __init__(
        self,
        paths: DuctorPaths,
        manager: CronManager,
        *,
        config: AgentConfig,
        models: ModelRegistry,
    ) -> None:
        self._paths = paths
        self._manager = manager
        self._config = config
        self._models = models
        self._on_result: CronResultCallback | None = None
        self._scheduled: dict[str, asyncio.Task[None]] = {}
        self._watcher_task: asyncio.Task[None] | None = None
        self._last_mtime: float = 0.0
        self._running = False

    def set_result_handler(self, handler: CronResultCallback) -> None:
        """Set callback for job results (called after each execution)."""
        self._on_result = handler

    async def start(self) -> None:
        """Start the observer: schedule all jobs and begin watching."""
        self._running = True
        await self._schedule_all()
        self._watcher_task = asyncio.create_task(self._watch_loop())
        logger.info("CronObserver started (%d jobs scheduled)", len(self._scheduled))

    async def stop(self) -> None:
        """Stop the observer: cancel all scheduled jobs and the watcher."""
        self._running = False
        if self._watcher_task:
            self._watcher_task.cancel()
            self._watcher_task = None
        for task in self._scheduled.values():
            task.cancel()
        self._scheduled.clear()
        logger.info("CronObserver stopped")

    # -- File watcher --

    async def _watch_loop(self) -> None:
        """Poll cron_jobs.json mtime every 5 seconds, reschedule on change."""
        while self._running:
            await asyncio.sleep(5)
            try:
                current_mtime = await asyncio.to_thread(
                    lambda: self._paths.cron_jobs_path.stat().st_mtime,
                )
            except FileNotFoundError:
                continue
            if current_mtime != self._last_mtime:
                self._last_mtime = current_mtime
                await asyncio.to_thread(self._manager.reload)
                await self._reschedule_all()

    # -- Scheduling --

    async def _schedule_all(self) -> None:
        """Schedule asyncio tasks for all enabled jobs."""
        await self._update_mtime()
        for job in self._manager.list_jobs():
            if job.enabled:
                self._schedule_job(
                    job.id,
                    job.schedule,
                    job.agent_instruction,
                    job.task_folder,
                    job.timezone,
                )

    async def _reschedule_all(self) -> None:
        """Cancel existing schedules and reschedule from current JSON state."""
        for task in self._scheduled.values():
            task.cancel()
        self._scheduled.clear()
        await self._schedule_all()
        logger.info("Rescheduled %d jobs", len(self._scheduled))

    def _schedule_job(
        self,
        job_id: str,
        schedule: str,
        instruction: str,
        task_folder: str,
        job_timezone: str = "",
    ) -> None:
        """Calculate next run time and schedule an asyncio task.

        Uses the job's timezone (if set), then the global ``user_timezone``
        config, then the host OS timezone, and finally UTC as last resort.
        CronSim iterates in the resolved local timezone so that ``0 9 * * *``
        means 09:00 in the user's wall-clock time.
        """
        try:
            tz = resolve_user_timezone(job_timezone or self._config.user_timezone)
            now_local = datetime.now(tz)
            # CronSim works on time components; feed it the local time
            # so hour fields match the user's wall clock.
            now_naive = now_local.replace(tzinfo=None)
            it = CronSim(schedule, now_naive)
            next_naive: datetime = next(it)
            # Re-attach the timezone and compute delay against real UTC clock.
            next_aware = next_naive.replace(fold=1, tzinfo=tz)
            delay = (next_aware - datetime.now(tz)).total_seconds()
            delay = max(delay, 0)
            task = asyncio.create_task(
                self._run_at(delay, job_id, instruction, task_folder, schedule, job_timezone),
            )
            self._scheduled[job_id] = task
            logger.debug(
                "Scheduled %s: next run %s (%s), delay %.0fs",
                job_id,
                next_naive.isoformat(),
                tz.key,
                delay,
            )
        except (CronSimError, StopIteration):
            logger.warning("Invalid cron expression for job %s: %s", job_id, schedule)

    async def _run_at(  # noqa: PLR0913
        self,
        delay: float,
        job_id: str,
        instruction: str,
        task_folder: str,
        schedule: str,
        job_timezone: str = "",
    ) -> None:
        """Wait for delay, execute the job, then reschedule for next occurrence."""
        try:
            await asyncio.sleep(delay)
            await self._execute_job(job_id, instruction, task_folder)
        except asyncio.CancelledError:
            logger.debug("Cron job %s cancelled during execution", job_id)
            return
        if self._running:
            self._schedule_job(job_id, schedule, instruction, task_folder, job_timezone)

    # -- Execution --

    def _snapshot_config(self) -> tuple[str, str, str]:
        """Snapshot model, provider, and permission_mode atomically.

        Reads the shared AgentConfig once so a concurrent ``/model`` switch
        cannot produce an inconsistent (model, provider) pair.
        """
        model = self._config.model
        provider = self._models.provider_for(model)
        return model, provider, self._config.permission_mode

    async def _execute_job(
        self,
        job_id: str,
        instruction: str,
        task_folder: str,
    ) -> None:
        """Spawn a fresh CLI session in the cron_task folder."""
        set_log_context(operation="cron")
        job = self._manager.get_job(job_id)
        job_title = job.title if job else job_id
        logger.info("Cron job starting job=%s", job_title)
        t0 = time.monotonic()
        folder = self._paths.cron_tasks_dir / task_folder
        if not await asyncio.to_thread(folder.is_dir):
            logger.error("Cron task folder missing: %s", folder)
            self._manager.update_run_status(job_id, status="error:folder_missing")
            return

        model, provider, permission_mode = self._snapshot_config()
        enriched = enrich_instruction(instruction, task_folder)
        cmd = build_cmd(provider, model, enriched, permission_mode)

        if cmd is None:
            logger.error("%s CLI not found for cron job %s", provider, job_id)
            self._manager.update_run_status(job_id, status=f"error:cli_not_found_{provider}")
            return

        timeout = self._config.cli_timeout
        logger.debug(
            "Cron subprocess cmd=%s cwd=%s provider=%s model=%s timeout=%.0fs",
            " ".join(cmd[:3]),
            folder,
            provider,
            model,
            timeout,
        )

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=str(folder),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        timed_out = False
        try:
            async with asyncio.timeout(timeout):
                stdout, stderr = await proc.communicate()
        except TimeoutError:
            timed_out = True
            logger.warning("Cron job %s timed out after %.0fs, killing process", job_id, timeout)
            proc.kill()
            stdout, stderr = await proc.communicate()
        except asyncio.CancelledError:
            logger.debug("Cron job %s cancelled, killing subprocess", job_id)
            proc.kill()
            await proc.wait()
            raise

        if stderr:
            logger.debug("Cron stderr (%s): %s", job_id, stderr.decode(errors="replace")[:500])

        if timed_out:
            status = "error:timeout"
            result_text = f"[Cron job timed out after {timeout:.0f}s]"
        else:
            result_text = (
                parse_codex_result(stdout) if provider == "codex" else parse_claude_result(stdout)
            )
            status = "success" if proc.returncode == 0 else f"error:exit_{proc.returncode}"

        self._manager.update_run_status(job_id, status=status)
        elapsed_ms = (time.monotonic() - t0) * 1000
        logger.info(
            "Cron job completed job=%s status=%s duration_ms=%.0f stdout=%d result=%d",
            job_title,
            status,
            elapsed_ms,
            len(stdout),
            len(result_text),
        )

        if self._on_result and job:
            try:
                await self._on_result(job.title, result_text, status)
            except Exception:
                logger.exception("Error in cron result handler for job %s", job_id)

    async def _update_mtime(self) -> None:
        """Cache the current mtime of the jobs file."""
        try:
            self._last_mtime = await asyncio.to_thread(
                lambda: self._paths.cron_jobs_path.stat().st_mtime,
            )
        except FileNotFoundError:
            self._last_mtime = 0.0
