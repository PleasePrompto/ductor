"""Background task observer: fire-and-forget CLI execution with notification."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from ductor_bot.background.models import BackgroundResult, BackgroundSubmit, BackgroundTask
from ductor_bot.i18n import t
from ductor_bot.infra.task_runner import TaskRunOptions, run_oneshot_task
from ductor_bot.workspace.loader import build_appended_files_block

if TYPE_CHECKING:
    from ductor_bot.cli.param_resolver import TaskExecutionConfig
    from ductor_bot.cli.service import CLIService
    from ductor_bot.config import AgentConfig
    from ductor_bot.workspace.paths import DuctorPaths

logger = logging.getLogger(__name__)

BgResultCallback = Callable[[BackgroundResult], Awaitable[None]]

MAX_TASKS_PER_CHAT = 5


def _make_result(  # noqa: PLR0913  -- result fields have natural multi-arity
    bg_task: BackgroundTask,
    t0: float,
    *,
    result_text: str,
    status: str,
    session_name: str = "",
    session_id: str = "",
) -> BackgroundResult:
    """Build a :class:`BackgroundResult` carrying the task's routing fields."""
    return BackgroundResult(
        task_id=bg_task.task_id,
        chat_id=bg_task.chat_id,
        message_id=bg_task.message_id,
        thread_id=bg_task.thread_id,
        transport=bg_task.transport,
        prompt_preview=bg_task.prompt[:60],
        result_text=result_text,
        status=status,
        elapsed_seconds=time.monotonic() - t0,
        provider=bg_task.provider,
        model=bg_task.model,
        session_name=session_name,
        session_id=session_id,
    )


class BackgroundObserver:
    """Manages fire-and-forget background CLI tasks."""

    def __init__(
        self,
        paths: DuctorPaths,
        *,
        timeout_seconds: float,
        cli_service: CLIService | None = None,
        config: AgentConfig,
    ) -> None:
        self._paths = paths
        self._timeout_seconds = timeout_seconds
        self._cli_service = cli_service
        self._config = config
        self._on_result: BgResultCallback | None = None
        self._tasks: dict[str, BackgroundTask] = {}

    def set_result_handler(self, handler: BgResultCallback) -> None:
        self._on_result = handler

    def submit(
        self,
        sub: BackgroundSubmit,
        exec_config: TaskExecutionConfig,
    ) -> str:
        """Submit a background task. Returns task_id."""
        active = sum(
            1
            for t in self._tasks.values()
            if t.chat_id == sub.chat_id and t.asyncio_task and not t.asyncio_task.done()
        )
        if active >= MAX_TASKS_PER_CHAT:
            msg = t("tasks.too_many", max=MAX_TASKS_PER_CHAT)
            raise ValueError(msg)

        task_id = secrets.token_hex(4)
        has_session_override = bool(sub.provider_override)
        bg_task = BackgroundTask(
            task_id=task_id,
            chat_id=sub.chat_id,
            prompt=sub.prompt,
            message_id=sub.message_id,
            thread_id=sub.thread_id,
            transport=sub.transport,
            provider=sub.provider_override if has_session_override else exec_config.provider,
            model=sub.model_override if has_session_override else exec_config.model,
            reasoning_effort=(
                sub.reasoning_effort_override
                if has_session_override
                else exec_config.reasoning_effort
            ),
            submitted_at=time.monotonic(),
            session_name=sub.session_name,
            resume_session_id=sub.resume_session_id,
        )
        atask = asyncio.create_task(self._run(bg_task, exec_config))
        bg_task.asyncio_task = atask
        atask.add_done_callback(lambda _t: self._tasks.pop(task_id, None))
        self._tasks[task_id] = bg_task
        logger.info(
            "Background task submitted id=%s chat=%d provider=%s session=%s",
            task_id,
            sub.chat_id,
            bg_task.provider,
            sub.session_name or "<stateless>",
        )
        return task_id

    def active_tasks(self, chat_id: int | None = None) -> list[BackgroundTask]:
        tasks = [t for t in self._tasks.values() if t.asyncio_task and not t.asyncio_task.done()]
        if chat_id is not None:
            tasks = [t for t in tasks if t.chat_id == chat_id]
        return tasks

    async def cancel_all(self, chat_id: int) -> int:
        count = 0
        cancelled: list[asyncio.Task[None]] = []
        for task in list(self._tasks.values()):
            if task.chat_id == chat_id and task.asyncio_task and not task.asyncio_task.done():
                task.asyncio_task.cancel()
                cancelled.append(task.asyncio_task)
                count += 1
        if cancelled:
            await asyncio.gather(*cancelled, return_exceptions=True)
        return count

    async def shutdown(self) -> None:
        cancelled: list[asyncio.Task[None]] = []
        for task in list(self._tasks.values()):
            if task.asyncio_task and not task.asyncio_task.done():
                task.asyncio_task.cancel()
                cancelled.append(task.asyncio_task)
        if cancelled:
            await asyncio.gather(*cancelled, return_exceptions=True)
        self._tasks.clear()

    async def _run(self, bg_task: BackgroundTask, exec_config: TaskExecutionConfig) -> None:
        if bg_task.session_name and self._cli_service:
            await self._run_with_session(bg_task)
        else:
            await self._run_oneshot(bg_task, exec_config)

    async def _run_oneshot(self, bg_task: BackgroundTask, exec_config: TaskExecutionConfig) -> None:
        """Legacy stateless execution via run_oneshot_task."""
        t0 = time.monotonic()
        try:
            result = await run_oneshot_task(
                exec_config,
                bg_task.prompt,
                TaskRunOptions(
                    cwd=self._paths.workspace,
                    timeout_seconds=self._timeout_seconds,
                    timeout_label="Background task",
                    ductor_home=self._paths.ductor_home,
                ),
            )

            await self._deliver(
                _make_result(
                    bg_task,
                    t0,
                    result_text=result.result_text,
                    status="error:cli_not_found" if result.execution is None else result.status,
                )
            )
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await self._deliver(_make_result(bg_task, t0, result_text="", status="aborted"))
            raise
        except Exception:
            logger.exception("Background task failed id=%s", bg_task.task_id)
            with contextlib.suppress(Exception):
                await self._deliver(
                    _make_result(
                        bg_task,
                        t0,
                        result_text=t("tasks.internal_error"),
                        status="error:internal",
                    )
                )

    async def _run_with_session(self, bg_task: BackgroundTask) -> None:
        """Named session execution via CLIService with resume support."""
        from ductor_bot.cli.types import AgentRequest

        assert self._cli_service is not None

        t0 = time.monotonic()
        process_label = f"ns:{bg_task.session_name}"
        try:
            files_block = await build_appended_files_block(
                self._paths, self._config.append_system_prompt_files
            )
            request = AgentRequest(
                prompt=bg_task.prompt,
                append_system_prompt=files_block,
                model_override=bg_task.model or None,
                provider_override=bg_task.provider or None,
                effort_override=bg_task.reasoning_effort or None,
                chat_id=bg_task.chat_id,
                process_label=process_label,
                resume_session=bg_task.resume_session_id or None,
                timeout_seconds=self._timeout_seconds,
            )
            response = await self._cli_service.execute(request)

            status = "ok"
            if response.is_error:
                status = "error:cli"
                if response.timed_out:
                    status = "error:timeout"

            await self._deliver(
                _make_result(
                    bg_task,
                    t0,
                    result_text=response.result or "",
                    status=status,
                    session_name=bg_task.session_name,
                    session_id=response.session_id or "",
                )
            )
        except asyncio.CancelledError:
            with contextlib.suppress(Exception):
                await self._deliver(
                    _make_result(
                        bg_task,
                        t0,
                        result_text="",
                        status="aborted",
                        session_name=bg_task.session_name,
                    )
                )
            raise
        except Exception:
            logger.exception(
                "Named session task failed id=%s name=%s", bg_task.task_id, bg_task.session_name
            )
            with contextlib.suppress(Exception):
                await self._deliver(
                    _make_result(
                        bg_task,
                        t0,
                        result_text=t("tasks.internal_error"),
                        status="error:internal",
                        session_name=bg_task.session_name,
                    )
                )

    async def _deliver(self, result: BackgroundResult) -> None:
        if self._on_result is None:
            logger.warning("No result handler set for background task %s", result.task_id)
            return
        try:
            await self._on_result(result)
        except Exception:
            logger.exception("Error delivering background result id=%s", result.task_id)
