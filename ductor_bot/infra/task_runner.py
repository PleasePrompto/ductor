"""Shared one-shot CLI task execution for cron, webhook, and background observers."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ductor_bot.cli.param_resolver import TaskExecutionConfig, TaskOverrides
    from ductor_bot.cron.execution import OneShotExecutionResult
    from ductor_bot.infra.base_task_observer import BaseTaskObserver

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class TaskResult:
    """Normalized outcome of a one-shot task run."""

    status: str
    result_text: str
    execution: OneShotExecutionResult | None


@dataclass(frozen=True, slots=True)
class TaskRunOptions:
    """Execution options shared by cron, webhook, and background one-shot runs."""

    cwd: Path
    timeout_seconds: float
    timeout_label: str
    ductor_home: Path | None = None


async def run_oneshot_task(
    exec_config: TaskExecutionConfig,
    prompt: str,
    options: TaskRunOptions,
) -> TaskResult:
    """Build the CLI command and execute it, returning a normalized result.

    Returns a ``cli_not_found`` result instead of raising when the provider
    binary is missing.  All other execution details (timeout, stderr, status
    mapping) are delegated to ``execute_one_shot``.
    """
    from ductor_bot.cron.execution import build_cmd, execute_one_shot

    one_shot = build_cmd(exec_config, prompt)
    if one_shot is None:
        return TaskResult(
            status=f"error:cli_not_found_{exec_config.provider}",
            result_text=f"[{exec_config.provider} CLI not found]",
            execution=None,
        )

    if options.ductor_home is not None:
        one_shot.env_overrides["DUCTOR_HOME"] = str(options.ductor_home)
        import os

        from ductor_bot.infra.env_secrets import load_env_secrets

        for k, v in load_env_secrets(options.ductor_home / ".env").items():
            if k not in os.environ and k not in one_shot.env_overrides:
                one_shot.env_overrides[k] = v
    execution = await execute_one_shot(
        one_shot,
        cwd=options.cwd,
        provider=exec_config.provider,
        timeout_seconds=options.timeout_seconds,
        timeout_label=options.timeout_label,
    )

    return TaskResult(
        status=execution.status,
        result_text=execution.result_text,
        execution=execution,
    )


async def execute_in_task_folder(  # noqa: PLR0913
    observer: BaseTaskObserver,
    *,
    cron_tasks_dir: Path,
    task_folder: str,
    instruction: str,
    overrides: TaskOverrides,
    dependency: str | None,
    task_id: str,
    task_label: str,
    timeout_seconds: float,
) -> TaskResult:
    """Execute a one-shot CLI task inside a ``cron_tasks`` subfolder.

    Shared core for :class:`CronObserver` and :class:`WebhookObserver`.
    Handles dependency locking, folder validation, config resolution,
    instruction enrichment, subprocess execution, and result logging.

    Caller-specific concerns (result delivery, status persistence,
    quiet-hour checks) remain with the caller.
    """
    from ductor_bot.cron.dependency_queue import get_dependency_queue
    from ductor_bot.cron.execution import enrich_instruction

    dep_queue = get_dependency_queue()

    async with dep_queue.acquire(task_id, task_label, dependency):
        folder = cron_tasks_dir / task_folder
        if not await asyncio.to_thread(folder.is_dir):
            return TaskResult(
                status="error:folder_missing",
                result_text="",
                execution=None,
            )

        exec_config = observer.resolve_execution_config(overrides)
        enriched = enrich_instruction(instruction, task_folder)

        logger.debug(
            "%s cwd=%s provider=%s model=%s timeout=%.0fs",
            task_label,
            folder,
            exec_config.provider,
            exec_config.model,
            timeout_seconds,
        )

        result = await run_oneshot_task(
            exec_config,
            enriched,
            TaskRunOptions(
                cwd=folder,
                timeout_seconds=timeout_seconds,
                timeout_label=task_label,
                ductor_home=observer._paths.ductor_home,
            ),
        )

        if result.execution is not None:
            observer.log_execution_result(result, task_label, task_id)

        return result
