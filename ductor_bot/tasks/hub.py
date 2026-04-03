"""TaskHub: central coordinator for background task delegation."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from ductor_bot.tasks.models import TaskEntry, TaskInFlight, TaskResult, TaskSubmit

if TYPE_CHECKING:
    from pathlib import Path

    from ductor_bot.cli.service import CLIService
    from ductor_bot.config import TasksConfig
    from ductor_bot.tasks.registry import TaskRegistry
    from ductor_bot.workspace.paths import DuctorPaths

logger = logging.getLogger(__name__)

_FINISHED = frozenset({"done", "failed", "cancelled"})
_RESUMABLE = frozenset({"done", "failed", "cancelled", "waiting"})
_MAINTENANCE_INTERVAL = 5 * 3600  # 5 hours

TaskResultCallback = Callable[[TaskResult], Awaitable[None]]
QuestionHandler = Callable[[str, str, str, str, str | None], Awaitable[None]]
# QuestionHandler(task_id, question, prompt_preview, chat_id, thread_id) -> None

TASK_PROMPT_SUFFIX = """

---
TASK RULES (MANDATORY):
You are a background task agent. You have NO direct user access.

IMPORTANT — If you need ANY information to complete this task (missing details,
clarifications, preferences), you MUST use this tool:
```
python3 tools/task_tools/ask_parent.py "your question here"
```
Do NOT include questions in your response text. The tool forwards your question
to the parent agent who will resume you with the answer.

After finishing, update your task memory: {taskmemory_path}
"""

_RESUME_REMINDER = """

---
REMINDER: You are a background task agent with NO direct user access.
- Need more info? Use: python3 tools/task_tools/ask_parent.py "question"
- Do NOT put questions in your response — the user cannot see them.
- When done, write your final results to: {taskmemory_path}
"""


class TaskHub:
    """Central coordinator for background task delegation.

    Combines ``BackgroundObserver`` execution pattern with ``InterAgentBus``
    result-delivery pattern. Manages the full lifecycle: submit → execute →
    question handling → result delivery.
    """

    def __init__(
        self,
        registry: TaskRegistry,
        paths: DuctorPaths,
        *,
        cli_service: CLIService | None = None,
        config: TasksConfig,
    ) -> None:
        self._registry = registry
        self._paths = paths
        self._cli_service = cli_service
        self._cli_services: dict[str, CLIService] = {}
        self._agent_tasks_dirs: dict[str, Path] = {}
        self._config = config
        self._in_flight: dict[str, TaskInFlight] = {}
        self._result_handlers: dict[str, TaskResultCallback] = {}
        self._question_handlers: dict[str, QuestionHandler] = {}
        self._agent_chat_ids: dict[str, str] = {}
        self._maintenance_task: asyncio.Task[None] | None = None

    def start_maintenance(self) -> None:
        """Start periodic orphan cleanup (call once after bot startup)."""
        if self._maintenance_task is None:
            self._maintenance_task = asyncio.create_task(
                self._maintenance_loop(), name="task-maintenance"
            )

    @property
    def registry(self) -> TaskRegistry:
        return self._registry

    def set_result_handler(self, agent_name: str, handler: TaskResultCallback) -> None:
        """Register callback for delivering results to a parent agent."""
        self._result_handlers[agent_name] = handler

    def set_question_handler(self, agent_name: str, handler: QuestionHandler) -> None:
        """Register handler for task-agent questions (ask_parent)."""
        self._question_handlers[agent_name] = handler

    def set_cli_service(self, agent_name: str, cli: CLIService) -> None:
        """Register a per-agent CLI service for task execution."""
        self._cli_services[agent_name] = cli

    def set_agent_paths(self, agent_name: str, paths: DuctorPaths) -> None:
        """Register per-agent paths for task folder isolation."""
        self._agent_tasks_dirs[agent_name] = paths.tasks_dir

    def set_agent_chat_id(self, agent_name: str, chat_id: str) -> None:
        """Register the primary chat_id for an agent (for resolving CLI-submitted tasks)."""
        self._agent_chat_ids[agent_name] = chat_id

    def _check_enabled(self) -> None:
        if not self._config.enabled:
            msg = "Task system is disabled"
            raise ValueError(msg)
        if self._cli_service is None and not self._cli_services:
            msg = "CLIService not available"
            raise ValueError(msg)

    def submit(self, submit: TaskSubmit) -> str:
        """Create a task, spawn CLI subprocess. Returns task_id."""
        self._check_enabled()

        # Resolve chat_id: CLI subprocess doesn't know it, look up from agent name
        if not submit.chat_id:
            resolved = self._agent_chat_ids.get(submit.parent_agent, "")
            if resolved:
                submit.chat_id = resolved

        active = sum(
            1
            for t in self._in_flight.values()
            if t.entry.chat_id == submit.chat_id and t.asyncio_task and not t.asyncio_task.done()
        )
        if active >= self._config.max_parallel:
            msg = f"Too many background tasks ({self._config.max_parallel} max)"
            raise ValueError(msg)

        provider = submit.provider_override or ""
        model = submit.model_override or ""
        thinking = submit.thinking_override or ""

        # Resolve per-agent tasks_dir for folder isolation
        agent_tasks_dir = self._agent_tasks_dirs.get(submit.parent_agent)
        entry = self._registry.create(
            submit, provider, model, thinking=thinking, tasks_dir=agent_tasks_dir
        )

        # Build prompt with mandatory suffix
        taskmemory = self._registry.taskmemory_path(entry.task_id)
        full_prompt = submit.prompt + TASK_PROMPT_SUFFIX.format(taskmemory_path=taskmemory)

        self._spawn(entry, full_prompt, thinking)

        logger.info(
            "Task submitted id=%s name='%s' parent=%s provider=%s",
            entry.task_id,
            entry.name,
            submit.parent_agent,
            entry.provider or "(parent default)",
        )
        return entry.task_id

    def resume(self, task_id: str, follow_up: str, *, parent_agent: str = "") -> str:
        """Resume a completed task's CLI session with a follow-up. Returns task_id."""
        self._check_enabled()

        entry = self._registry.get(task_id)
        if entry is None:
            msg = f"Task '{task_id}' not found"
            raise ValueError(msg)
        if entry.status not in _RESUMABLE:
            msg = f"Task '{task_id}' is still {entry.status}"
            raise ValueError(msg)
        if not entry.session_id:
            msg = f"Task '{task_id}' has no resumable session"
            raise ValueError(msg)
        if not entry.provider:
            msg = f"Task '{task_id}' has no provider recorded"
            raise ValueError(msg)

        # Reset to running — same entry, same folder, same task_id
        self._registry.update_status(
            task_id,
            "running",
            completed_at=0.0,
            error="",
            result_preview="",
            last_question="",
        )

        # Append a short system reminder so the task agent remembers how to
        # communicate (ask_parent, TASKMEMORY, no direct user access).
        taskmemory = self._registry.taskmemory_path(entry.task_id)
        full_prompt = follow_up + _RESUME_REMINDER.format(taskmemory_path=taskmemory)
        self._spawn(entry, full_prompt, entry.thinking, resume_session=entry.session_id)

        logger.info(
            "Task resumed id=%s name='%s' provider=%s",
            task_id,
            entry.name,
            entry.provider,
        )
        return task_id

    def _spawn(
        self,
        entry: TaskEntry,
        prompt: str,
        thinking: str,
        *,
        resume_session: str | None = None,
    ) -> None:
        """Create the asyncio task and register it in-flight."""
        inflight = TaskInFlight(entry=entry)
        atask = asyncio.create_task(
            self._run(entry, prompt, thinking, resume_session=resume_session),
            name=f"task:{entry.task_id}",
        )
        inflight.asyncio_task = atask
        atask.add_done_callback(lambda _: self._in_flight.pop(entry.task_id, None))
        self._in_flight[entry.task_id] = inflight

    async def forward_question(self, task_id: str, question: str) -> str:
        """Forward a task agent's question to the parent. Returns immediately.

        The question is delivered asynchronously to the parent agent's Telegram
        chat. The parent answers by resuming the task with ``resume_task.py``.
        """
        entry = self._registry.get(task_id)
        if entry is None:
            return "Error: Task not found"

        handler = self._question_handlers.get(entry.parent_agent)
        if handler is None:
            return f"Error: No question handler for agent '{entry.parent_agent}'"

        logger.info(
            "Task %s forwarding question to '%s': %s",
            task_id,
            entry.parent_agent,
            question[:80],
        )

        self._registry.update_status(
            task_id,
            entry.status,
            question_count=entry.question_count + 1,
            last_question=question[:200],
        )

        # Mark in-flight task so _run() uses "waiting" instead of "done"
        inflight = self._in_flight.get(task_id)
        if inflight:
            inflight.has_pending_question = True

        # Fire-and-forget: deliver to parent's Telegram chat
        task = asyncio.create_task(
            self._deliver_question(handler, entry, question),
            name=f"task-question:{task_id}",
        )
        task.add_done_callback(lambda _: None)  # prevent GC of fire-and-forget task

        return (
            "Question forwarded to parent agent. "
            "Finish your current work — you will be resumed with the answer."
        )

    async def _deliver_question(
        self,
        handler: QuestionHandler,
        entry: TaskEntry,
        question: str,
    ) -> None:
        """Deliver question to parent agent (background coroutine)."""
        try:
            await handler(
                entry.task_id,
                question,
                entry.prompt_preview,
                entry.chat_id,
                str(entry.thread_id) if entry.thread_id is not None else None,
            )
        except Exception:
            logger.exception("Question delivery failed for task %s", entry.task_id)

    async def cancel(self, task_id: str) -> bool:
        """Cancel a running task. Returns True if cancelled."""
        inflight = self._in_flight.get(task_id)
        if inflight is None or inflight.asyncio_task is None or inflight.asyncio_task.done():
            return False
        inflight.asyncio_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await inflight.asyncio_task
        return True

    async def cancel_all(self, chat_id: str) -> int:
        """Cancel all running tasks for a chat."""
        count = 0
        cancelled: list[asyncio.Task[None]] = []
        for inflight in list(self._in_flight.values()):
            if (
                inflight.entry.chat_id == chat_id
                and inflight.asyncio_task
                and not inflight.asyncio_task.done()
            ):
                inflight.asyncio_task.cancel()
                cancelled.append(inflight.asyncio_task)
                count += 1
        if cancelled:
            await asyncio.gather(*cancelled, return_exceptions=True)
        return count

    def active_tasks(self, chat_id: str | None = None) -> list[TaskEntry]:
        """Return in-flight task entries."""
        entries = [
            t.entry
            for t in self._in_flight.values()
            if t.asyncio_task and not t.asyncio_task.done()
        ]
        if chat_id is not None:
            entries = [e for e in entries if e.chat_id == chat_id]
        return entries

    async def shutdown(self) -> None:
        """Cancel all in-flight tasks and clean up."""
        if self._maintenance_task and not self._maintenance_task.done():
            self._maintenance_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._maintenance_task
            self._maintenance_task = None

        cancelled: list[asyncio.Task[None]] = []
        for inflight in list(self._in_flight.values()):
            if inflight.asyncio_task and not inflight.asyncio_task.done():
                inflight.asyncio_task.cancel()
                cancelled.append(inflight.asyncio_task)
        if cancelled:
            await asyncio.gather(*cancelled, return_exceptions=True)
        self._in_flight.clear()

    async def _maintenance_loop(self) -> None:
        """Periodically clean orphaned task entries/folders (every 5 hours)."""
        try:
            while True:
                await asyncio.sleep(_MAINTENANCE_INTERVAL)
                try:
                    removed = self._registry.cleanup_orphans()
                    if removed:
                        logger.info("Task maintenance: removed %d orphan(s)", removed)
                except Exception:
                    logger.exception("Task maintenance failed (continuing)")
        except asyncio.CancelledError:
            pass

    async def _run(
        self,
        entry: TaskEntry,
        prompt: str,
        thinking: str,
        *,
        resume_session: str | None = None,
    ) -> None:
        """Execute task as CLI subprocess."""
        from ductor_bot.cli.types import AgentRequest

        cli = self._cli_services.get(entry.parent_agent) or self._cli_service
        assert cli is not None

        t0 = time.monotonic()
        try:
            timeout = self._config.timeout_seconds

            request = AgentRequest(
                prompt=prompt,
                model_override=entry.model or None,
                provider_override=entry.provider or None,
                chat_id=entry.chat_id,
                process_label=f"task:{entry.task_id}",
                timeout_seconds=timeout,
                resume_session=resume_session,
            )

            # Pre-resolve effective provider/model so the entry is never empty
            eff_provider, eff_model = cli.resolve_provider(request)
            if eff_provider and not entry.provider:
                self._registry.update_status(
                    entry.task_id, "running", provider=eff_provider, model=eff_model
                )
                entry.provider = eff_provider
                entry.model = eff_model

            response = await cli.execute(request)

            elapsed = time.monotonic() - t0
            if response.timed_out:
                status = "failed"
                error = f"Timeout after {timeout:.0f}s"
            elif response.is_error:
                status = "failed"
                error = response.result or "CLI error"
            else:
                # If the task asked a question during this run, mark as waiting
                inflight = self._in_flight.get(entry.task_id)
                status = "waiting" if inflight and inflight.has_pending_question else "done"
                error = ""

            # Accumulate turns (resume adds to previous count)
            total_turns = entry.num_turns + response.num_turns

            self._registry.update_status(
                entry.task_id,
                status,
                session_id=response.session_id or "",
                completed_at=time.time(),
                elapsed_seconds=elapsed,
                error=error,
                result_preview=(response.result or "")[:_RESULT_PREVIEW_LEN],
                num_turns=total_turns,
            )

            result_text = response.result or ""
            session_id = response.session_id or ""

            # Append TASKMEMORY.md content so the parent gets the full picture
            if status == "done":
                taskmemory = self._registry.taskmemory_path(entry.task_id)
                result_text = _append_taskmemory(result_text, taskmemory)

            # Append resume hint so the parent agent knows it can follow up
            if status == "done" and session_id:
                result_text += (
                    f"\n\n---\nTo continue this task's conversation, use:\n"
                    f'python3 tools/task_tools/resume_task.py {entry.task_id} "your follow-up"'
                )

            await self._deliver(
                TaskResult(
                    task_id=entry.task_id,
                    chat_id=entry.chat_id,
                    parent_agent=entry.parent_agent,
                    name=entry.name,
                    prompt_preview=entry.prompt_preview,
                    result_text=result_text,
                    status=status,
                    elapsed_seconds=elapsed,
                    provider=entry.provider,
                    model=entry.model,
                    session_id=session_id,
                    error=error,
                    task_folder=str(self._registry.task_folder(entry.task_id)),
                    original_prompt=entry.original_prompt,
                    thread_id=entry.thread_id,
                )
            )

        except asyncio.CancelledError:
            elapsed = time.monotonic() - t0
            self._registry.update_status(
                entry.task_id,
                "cancelled",
                completed_at=time.time(),
                elapsed_seconds=elapsed,
            )
            with contextlib.suppress(Exception):
                await self._deliver(
                    TaskResult(
                        task_id=entry.task_id,
                        chat_id=entry.chat_id,
                        parent_agent=entry.parent_agent,
                        name=entry.name,
                        prompt_preview=entry.prompt_preview,
                        result_text="",
                        status="cancelled",
                        elapsed_seconds=elapsed,
                        provider=entry.provider,
                        model=entry.model,
                        original_prompt=entry.original_prompt,
                        thread_id=entry.thread_id,
                    )
                )
            raise

        except Exception:
            logger.exception("Task failed id=%s name='%s'", entry.task_id, entry.name)
            elapsed = time.monotonic() - t0
            error_msg = "Internal error (check logs)"
            self._registry.update_status(
                entry.task_id,
                "failed",
                completed_at=time.time(),
                elapsed_seconds=elapsed,
                error=error_msg,
            )
            with contextlib.suppress(Exception):
                await self._deliver(
                    TaskResult(
                        task_id=entry.task_id,
                        chat_id=entry.chat_id,
                        parent_agent=entry.parent_agent,
                        name=entry.name,
                        prompt_preview=entry.prompt_preview,
                        result_text="",
                        status="failed",
                        elapsed_seconds=elapsed,
                        provider=entry.provider,
                        model=entry.model,
                        error=error_msg,
                        original_prompt=entry.original_prompt,
                        thread_id=entry.thread_id,
                    )
                )

    async def _deliver(self, result: TaskResult) -> None:
        """Deliver result to the parent agent's registered callback."""
        handler = self._result_handlers.get(result.parent_agent)
        if handler is None:
            logger.warning(
                "No result handler for parent '%s' task=%s — result lost",
                result.parent_agent,
                result.task_id,
            )
            return
        try:
            await handler(result)
        except Exception:
            logger.exception(
                "Error delivering task result id=%s to '%s'",
                result.task_id,
                result.parent_agent,
            )


_RESULT_PREVIEW_LEN = 200
_TASKMEMORY_MAX_LEN = 4000


def _append_taskmemory(result_text: str, taskmemory_path: Path) -> str:
    """Append TASKMEMORY.md content to the result so the parent gets the full context."""
    try:
        if not taskmemory_path.is_file():
            return result_text
        content = taskmemory_path.read_text(encoding="utf-8").strip()
        if not content:
            return result_text
    except OSError:
        logger.debug("Could not read TASKMEMORY.md at %s", taskmemory_path)
        return result_text

    if len(content) > _TASKMEMORY_MAX_LEN:
        content = content[:_TASKMEMORY_MAX_LEN] + "\n[... truncated]"

    return f"{result_text}\n\n---\nCONTENT FROM TASKMEMORY.MD ({taskmemory_path}):\n\n{content}"
