"""In-process cron job scheduler: watches cron_jobs.json, schedules and executes jobs."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import TYPE_CHECKING

from cronsim import CronSim, CronSimError

from ductor_bot.cli.param_resolver import TaskOverrides, resolve_cli_config
from ductor_bot.config import resolve_user_timezone
from ductor_bot.cron.execution import (
    build_cmd,
    enrich_instruction,
    parse_claude_result,
    parse_codex_result,
    parse_gemini_result,
)
from ductor_bot.cron.manager import CronManager
from ductor_bot.log_context import set_log_context
from ductor_bot.utils.quiet_hours import check_quiet_hour

if TYPE_CHECKING:
    from ductor_bot.cli.codex_cache import CodexModelCache
    from ductor_bot.cli.param_resolver import TaskExecutionConfig
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
        codex_cache: CodexModelCache,
    ) -> None:
        self._paths = paths
        self._manager = manager
        self._config = config
        self._models = models
        self._codex_cache = codex_cache
        self._on_result: CronResultCallback | None = None
        self._scheduled: dict[str, asyncio.Task[None]] = {}
        self._watcher_task: asyncio.Task[None] | None = None
        self._reschedule_lock = asyncio.Lock()
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
            with contextlib.suppress(asyncio.CancelledError):
                await self._watcher_task
            self._watcher_task = None
        tasks = list(self._scheduled.values())
        for task in tasks:
            task.cancel()
        self._scheduled.clear()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        logger.info("CronObserver stopped")

    async def reschedule_now(self) -> None:
        """Reschedule all jobs immediately (used by interactive cron toggles)."""
        if not self._running:
            return
        await self._update_mtime()
        await self._reschedule_locked()

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
                await self._reschedule_locked()

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
        """Cancel existing schedules, await their termination, then reschedule.

        Awaiting cancellation prevents a race where the old task (executing a
        subprocess via asyncio.to_thread) is not yet interrupted and runs
        concurrently with the newly created replacement task.
        """
        tasks = list(self._scheduled.values())
        for task in tasks:
            task.cancel()
        self._scheduled.clear()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        await self._schedule_all()
        logger.info("Rescheduled %d jobs", len(self._scheduled))

    async def _reschedule_locked(self) -> None:
        """Serialize reschedules from watcher and interactive updates."""
        async with self._reschedule_lock:
            await self._reschedule_all()

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
            # Re-attach the timezone using fold=0 (prefer pre-DST interpretation
            # for ambiguous times).  For non-existent times (DST spring-forward
            # gap) the delay becomes negative; in that case advance to the next
            # cron slot so the job fires at the correct wall-clock time.
            next_aware = next_naive.replace(tzinfo=tz)  # fold=0 default
            delay = (next_aware - datetime.now(tz)).total_seconds()
            if delay < 0:
                next_naive = next(it)
                next_aware = next_naive.replace(tzinfo=tz)
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

    def _resolve_execution_config(
        self,
        task_overrides: TaskOverrides,
    ) -> TaskExecutionConfig:
        """Use param_resolver to get final config for this task."""
        return resolve_cli_config(
            self._config,
            self._codex_cache,
            task_overrides=task_overrides,
        )

    async def _execute_job(  # noqa: C901, PLR0912, PLR0915
        self,
        job_id: str,
        instruction: str,
        task_folder: str,
    ) -> None:
        """Spawn a fresh CLI session in the cron_task folder."""
        set_log_context(operation="cron")
        job = self._manager.get_job(job_id)
        job_title = job.title if job else job_id

        # Acquire dependency lock (if needed)
        from ductor_bot.cron.dependency_queue import get_dependency_queue

        dep_queue = get_dependency_queue()
        dependency = job.dependency if job else None

        async with dep_queue.acquire(job_id, job_title, dependency):
            logger.info("Cron job starting job=%s", job_title)

            # Check quiet hours (use job-specific or global defaults)
            is_quiet, now_hour, tz = check_quiet_hour(
                quiet_start=job.quiet_start if job else None,
                quiet_end=job.quiet_end if job else None,
                user_timezone=self._config.user_timezone,
                global_quiet_start=self._config.heartbeat.quiet_start,
                global_quiet_end=self._config.heartbeat.quiet_end,
            )
            if is_quiet:
                logger.debug(
                    "Cron job skipped: quiet hours (%d:00 %s) job=%s",
                    now_hour,
                    tz.key,
                    job_title,
                )
                return

            t0 = time.monotonic()
            folder = self._paths.cron_tasks_dir / task_folder
            if not await asyncio.to_thread(folder.is_dir):
                logger.error("Cron task folder missing: %s", folder)
                self._manager.update_run_status(job_id, status="error:folder_missing")
                return

            # Build TaskOverrides from job
            overrides = TaskOverrides(
                provider=job.provider if job else None,
                model=job.model if job else None,
                reasoning_effort=job.reasoning_effort if job else None,
                cli_parameters=job.cli_parameters if job else [],
            )

            exec_config = self._resolve_execution_config(overrides)
            enriched = enrich_instruction(instruction, task_folder)
            cmd = build_cmd(exec_config, enriched)

            if cmd is None:
                logger.error("%s CLI not found for cron job %s", exec_config.provider, job_id)
                self._manager.update_run_status(
                    job_id,
                    status=f"error:cli_not_found_{exec_config.provider}",
                )
                return

            timeout = self._config.cli_timeout
            logger.debug(
                "Cron subprocess cmd=%s cwd=%s provider=%s model=%s timeout=%.0fs",
                " ".join(cmd[:3]),
                folder,
                exec_config.provider,
                exec_config.model,
                timeout,
            )

            stdin_mode = (
                asyncio.subprocess.PIPE
                if exec_config.provider == "gemini"
                else asyncio.subprocess.DEVNULL
            )
            stdin_input = enriched.encode() if exec_config.provider == "gemini" else None

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=str(folder),
                stdin=stdin_mode,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            timed_out = False
            try:
                async with asyncio.timeout(timeout):
                    if exec_config.provider == "gemini":
                        stdout, stderr = await proc.communicate(input=stdin_input)
                    else:
                        stdout, stderr = await proc.communicate()
            except TimeoutError:
                timed_out = True
                logger.warning(
                    "Cron job %s timed out after %.0fs, killing process", job_id, timeout
                )
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
                if exec_config.provider == "codex":
                    result_text = parse_codex_result(stdout)
                elif exec_config.provider == "gemini":
                    result_text = parse_gemini_result(stdout)
                else:
                    result_text = parse_claude_result(stdout)
                status = "success" if proc.returncode == 0 else f"error:exit_{proc.returncode}"

            self._manager.update_run_status(job_id, status=status)
            # Refresh our mtime baseline so the file-watcher doesn't treat the
            # run-status write as a user-initiated change and trigger a full
            # reschedule of all other jobs.
            await self._update_mtime()
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
