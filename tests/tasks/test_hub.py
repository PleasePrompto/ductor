"""Tests for TaskHub."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from ductor_bot.cli.process_registry import ProcessRegistry
from ductor_bot.tasks.hub import TaskHub
from ductor_bot.tasks.models import TaskResult, TaskSubmit
from ductor_bot.tasks.registry import TaskRegistry
from ductor_bot.workspace.paths import DuctorPaths


@pytest.fixture
def registry(tmp_path: Path) -> TaskRegistry:
    return TaskRegistry(
        registry_path=tmp_path / "tasks.json",
        tasks_dir=tmp_path / "tasks",
    )


def _make_config(**overrides: object) -> MagicMock:
    config = MagicMock()
    config.enabled = True
    config.max_parallel = 5
    config.timeout_seconds = 60.0
    for k, v in overrides.items():
        setattr(config, k, v)
    return config


def _make_cli_service(
    result: str = "done", session_id: str = "sess-1", num_turns: int = 3
) -> MagicMock:
    cli = MagicMock()
    response = MagicMock()
    response.result = result
    response.session_id = session_id
    response.is_error = False
    response.timed_out = False
    response.num_turns = num_turns
    cli.execute = AsyncMock(return_value=response)
    cli.resolve_provider = MagicMock(return_value=("claude", "opus"))
    return cli


def _submit(prompt: str = "test", name: str = "Test Task") -> TaskSubmit:
    return TaskSubmit(
        chat_id=42,
        prompt=prompt,
        message_id=1,
        thread_id=None,
        parent_agent="main",
        name=name,
    )


class TestSubmit:
    async def test_creates_task_and_returns_id(
        self, registry: TaskRegistry, tmp_path: Path
    ) -> None:
        hub = TaskHub(
            registry,
            MagicMock(workspace=tmp_path),
            cli_service=_make_cli_service(),
            config=_make_config(),
        )
        task_id = hub.submit(_submit())
        assert isinstance(task_id, str)
        assert len(task_id) == 8  # hex(4)

        entry = registry.get(task_id)
        assert entry is not None
        assert entry.status == "running"

        await hub.shutdown()

    async def test_raises_when_disabled(self, registry: TaskRegistry, tmp_path: Path) -> None:
        hub = TaskHub(
            registry,
            MagicMock(workspace=tmp_path),
            cli_service=_make_cli_service(),
            config=_make_config(enabled=False),
        )
        with pytest.raises(ValueError, match="disabled"):
            hub.submit(_submit())

    async def test_raises_at_max_parallel(self, registry: TaskRegistry, tmp_path: Path) -> None:
        async def _hang(_: object) -> MagicMock:
            await asyncio.sleep(999)
            return MagicMock()  # never reached

        cli = _make_cli_service()
        # Make execute hang so tasks stay in-flight
        cli.execute = AsyncMock(side_effect=_hang)

        hub = TaskHub(
            registry,
            MagicMock(workspace=tmp_path),
            cli_service=cli,
            config=_make_config(max_parallel=1),
        )
        hub.submit(_submit(name="T1"))
        with pytest.raises(ValueError, match="Too many"):
            hub.submit(_submit(name="T2"))

        await hub.shutdown()


class TestRunAndDeliver:
    async def test_delivers_success_result(self, registry: TaskRegistry, tmp_path: Path) -> None:
        delivered: list[TaskResult] = []
        handler = AsyncMock(side_effect=delivered.append)

        hub = TaskHub(
            registry,
            MagicMock(workspace=tmp_path),
            cli_service=_make_cli_service("task output"),
            config=_make_config(),
        )
        hub.set_result_handler("main", handler)

        task_id = hub.submit(_submit())
        await asyncio.sleep(0.1)  # Let task run

        assert len(delivered) == 1
        assert delivered[0].task_id == task_id
        assert delivered[0].status == "done"
        assert delivered[0].result_text.startswith("task output")
        assert "resume_task.py" in delivered[0].result_text  # resume hint appended
        assert delivered[0].name == "Test Task"

        entry = registry.get(task_id)
        assert entry is not None
        assert entry.status == "done"

        await hub.shutdown()

    async def test_task_request_uses_task_label(
        self, registry: TaskRegistry, tmp_path: Path
    ) -> None:
        captured: list[object] = []
        cli = _make_cli_service("task output")

        async def _execute(request: object) -> object:
            captured.append(request)
            return _make_cli_service("task output").execute.return_value

        cli.execute = AsyncMock(side_effect=_execute)
        hub = TaskHub(
            registry,
            MagicMock(workspace=tmp_path),
            cli_service=cli,
            config=_make_config(),
        )

        task_id = hub.submit(_submit())
        await asyncio.sleep(0.1)

        assert captured
        assert captured[0].process_label == f"task:{task_id}"

        await hub.shutdown()

    async def test_delivers_error_on_cli_failure(
        self, registry: TaskRegistry, tmp_path: Path
    ) -> None:
        cli = _make_cli_service()
        cli.execute.return_value.is_error = True
        cli.execute.return_value.result = "API rate limit"

        delivered: list[TaskResult] = []
        hub = TaskHub(
            registry,
            MagicMock(workspace=tmp_path),
            cli_service=cli,
            config=_make_config(),
        )
        hub.set_result_handler("main", AsyncMock(side_effect=delivered.append))

        hub.submit(_submit())
        await asyncio.sleep(0.1)

        assert len(delivered) == 1
        assert delivered[0].status == "failed"
        assert "rate limit" in delivered[0].error.lower()

        await hub.shutdown()

    @pytest.mark.parametrize("returncode", [143, 137, -15, -9])
    async def test_sigterm_exit_is_classified_as_cancelled_not_failed(
        self, registry: TaskRegistry, tmp_path: Path, returncode: int
    ) -> None:
        """Exit 143/137 (= 128 + SIGTERM/SIGKILL) is user /stop, not a CLI error."""
        cli = _make_cli_service()
        cli.execute.return_value.is_error = True
        cli.execute.return_value.result = ""
        cli.execute.return_value.returncode = returncode

        delivered: list[TaskResult] = []
        hub = TaskHub(
            registry,
            MagicMock(workspace=tmp_path),
            cli_service=cli,
            config=_make_config(),
        )
        hub.set_result_handler("main", AsyncMock(side_effect=delivered.append))

        hub.submit(_submit())
        await asyncio.sleep(0.1)

        assert len(delivered) == 1
        assert delivered[0].status == "cancelled"
        assert delivered[0].error == ""

        await hub.shutdown()

    async def test_cancelled_task_delivers_partial_taskmemory(
        self, registry: TaskRegistry, tmp_path: Path
    ) -> None:
        """MED #1: partial TASKMEMORY.md written before SIGTERM must reach the parent."""
        cli = _make_cli_service()
        cli.execute.return_value.is_error = True
        cli.execute.return_value.result = ""
        cli.execute.return_value.returncode = 143  # SIGTERM -> status=cancelled

        delivered: list[TaskResult] = []
        hub = TaskHub(
            registry,
            MagicMock(workspace=tmp_path),
            cli_service=cli,
            config=_make_config(),
        )
        hub.set_result_handler("main", AsyncMock(side_effect=delivered.append))

        task_id = hub.submit(_submit())
        # Write partial memory BEFORE the task's execute mock returns — the
        # task folder is seeded synchronously on submit, so this is safe.
        memory = registry.taskmemory_path(task_id)
        memory.parent.mkdir(parents=True, exist_ok=True)
        memory.write_text("partial research findings\nline 2", encoding="utf-8")

        await asyncio.sleep(0.1)

        assert len(delivered) == 1
        assert delivered[0].status == "cancelled"
        assert "partial research findings" in delivered[0].result_text
        assert "CONTENT FROM TASKMEMORY.MD" in delivered[0].result_text

        await hub.shutdown()


class TestCancel:
    async def test_cancel_running_task(self, registry: TaskRegistry, tmp_path: Path) -> None:
        async def _hang(_: object) -> MagicMock:
            await asyncio.sleep(999)
            return MagicMock()  # never reached

        cli = _make_cli_service()
        cli.execute = AsyncMock(side_effect=_hang)

        delivered: list[TaskResult] = []
        hub = TaskHub(
            registry,
            MagicMock(workspace=tmp_path),
            cli_service=cli,
            config=_make_config(),
        )
        hub.set_result_handler("main", AsyncMock(side_effect=delivered.append))

        task_id = hub.submit(_submit())
        memory = registry.taskmemory_path(task_id)
        memory.write_text("partial cancellation notes\n", encoding="utf-8")
        await asyncio.sleep(0.05)

        success = await hub.cancel(task_id)
        assert success
        await asyncio.sleep(0.05)

        assert len(delivered) == 1
        assert delivered[0].status == "cancelled"
        assert "partial cancellation notes" in delivered[0].result_text
        assert "CONTENT FROM TASKMEMORY.MD" in delivered[0].result_text

        entry = registry.get(task_id)
        assert entry is not None
        assert entry.status == "cancelled"

    async def test_cancel_after_completion_preserves_result(
        self, registry: TaskRegistry, tmp_path: Path
    ) -> None:
        delivery_started = asyncio.Event()
        delivery_released = asyncio.Event()
        delivered_event = asyncio.Event()
        delivered: list[TaskResult] = []

        async def _blocked_handler(result: TaskResult) -> None:
            delivery_started.set()
            await delivery_released.wait()
            delivered.append(result)
            delivered_event.set()

        hub = TaskHub(
            registry,
            MagicMock(workspace=tmp_path),
            cli_service=_make_cli_service("complete result"),
            config=_make_config(),
        )
        hub.set_result_handler("main", _blocked_handler)

        task_id = hub.submit(_submit())
        await asyncio.wait_for(delivery_started.wait(), timeout=1)

        success = await hub.cancel(task_id)
        assert success

        delivery_released.set()
        await asyncio.wait_for(delivered_event.wait(), timeout=1)

        assert len(delivered) == 1
        assert delivered[0].status == "done"
        assert "complete result" in delivered[0].result_text

        entry = registry.get(task_id)
        assert entry is not None
        assert entry.status == "done"

        await hub.shutdown()

    async def test_cancel_all_after_completion_preserves_result(
        self, registry: TaskRegistry, tmp_path: Path
    ) -> None:
        delivery_started = asyncio.Event()
        delivery_released = asyncio.Event()
        delivered_event = asyncio.Event()
        delivered: list[TaskResult] = []

        async def _blocked_handler(result: TaskResult) -> None:
            delivery_started.set()
            await delivery_released.wait()
            delivered.append(result)
            delivered_event.set()

        hub = TaskHub(
            registry,
            MagicMock(workspace=tmp_path),
            cli_service=_make_cli_service("complete result"),
            config=_make_config(),
        )
        hub.set_result_handler("main", _blocked_handler)

        task_id = hub.submit(_submit())
        await asyncio.wait_for(delivery_started.wait(), timeout=1)

        count = await hub.cancel_all(42)
        assert count == 1

        delivery_released.set()
        await asyncio.wait_for(delivered_event.wait(), timeout=1)

        assert len(delivered) == 1
        assert delivered[0].status == "done"
        assert "complete result" in delivered[0].result_text

        entry = registry.get(task_id)
        assert entry is not None
        assert entry.status == "done"

        await hub.shutdown()

    async def test_cancel_nonexistent(self, registry: TaskRegistry, tmp_path: Path) -> None:
        hub = TaskHub(
            registry,
            MagicMock(workspace=tmp_path),
            cli_service=_make_cli_service(),
            config=_make_config(),
        )
        assert not await hub.cancel("nonexistent")

    async def test_cancel_logs(
        self,
        registry: TaskRegistry,
        tmp_path: Path,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        caplog.set_level("INFO", logger="ductor_bot.tasks.hub")

        async def _hang(_: object) -> MagicMock:
            await asyncio.sleep(999)
            return MagicMock()

        cli = _make_cli_service()
        cli.execute = AsyncMock(side_effect=_hang)
        hub = TaskHub(
            registry,
            MagicMock(workspace=tmp_path),
            cli_service=cli,
            config=_make_config(),
        )
        hub.set_result_handler("main", AsyncMock())

        task_id = hub.submit(_submit(name="Log Task"))
        await asyncio.sleep(0.05)
        assert await hub.cancel(task_id)
        assert not await hub.cancel("missing-task")

        hub.submit(_submit(name="A"))
        hub.submit(_submit(name="B"))
        await asyncio.sleep(0.05)
        assert await hub.cancel_all(42) == 2

        delivery_started = asyncio.Event()
        delivery_released = asyncio.Event()

        async def _blocked_handler(_: TaskResult) -> None:
            delivery_started.set()
            await delivery_released.wait()

        late_hub = TaskHub(
            registry,
            MagicMock(workspace=tmp_path),
            cli_service=_make_cli_service("late complete"),
            config=_make_config(),
        )
        late_hub.set_result_handler("main", _blocked_handler)
        late_task_id = late_hub.submit(_submit(name="Late Log Task"))
        await asyncio.wait_for(delivery_started.wait(), timeout=1)
        assert await late_hub.cancel(late_task_id)
        delivery_released.set()
        await late_hub.shutdown()

        messages = "\n".join(record.getMessage() for record in caplog.records)
        assert f"Task cancel accepted id={task_id} name='Log Task'" in messages
        assert "Cancel requested for non-running task id=missing-task" in messages
        assert "Task cancel accepted for 2 task(s) chat=42" in messages
        assert (
            f"Task {late_task_id} cancelled after completion — finished result preserved"
            in messages
        )

    async def test_shutdown_drains_pending_delivery(
        self, registry: TaskRegistry, tmp_path: Path
    ) -> None:
        delivery_started = asyncio.Event()
        delivery_released = asyncio.Event()
        delivered_event = asyncio.Event()
        delivered: list[TaskResult] = []

        async def _blocked_handler(result: TaskResult) -> None:
            delivery_started.set()
            await delivery_released.wait()
            delivered.append(result)
            delivered_event.set()

        hub = TaskHub(
            registry,
            MagicMock(workspace=tmp_path),
            cli_service=_make_cli_service("shutdown result"),
            config=_make_config(),
        )
        hub.set_result_handler("main", _blocked_handler)

        task_id = hub.submit(_submit())
        await asyncio.wait_for(delivery_started.wait(), timeout=1)
        assert await hub.cancel(task_id)

        shutdown_task = asyncio.create_task(hub.shutdown())
        await asyncio.sleep(0.05)
        assert not shutdown_task.done()

        delivery_released.set()
        await asyncio.wait_for(shutdown_task, timeout=1)
        await asyncio.wait_for(delivered_event.wait(), timeout=1)

        assert len(delivered) == 1
        assert delivered[0].status == "done"


class TestCancelWithProcessRegistry:
    """#92: cancel/cancel_all must kill the subprocess BEFORE the asyncio task
    (otherwise cli.execute's pipe stays open and CancelledError cannot propagate).
    """

    async def test_cancel_kills_subprocess_before_asyncio_cancel(
        self, registry: TaskRegistry, tmp_path: Path
    ) -> None:
        """cancel() must invoke process_registry.kill_for_task BEFORE asyncio_task.cancel()."""
        order: list[str] = []

        process_registry = AsyncMock(spec=ProcessRegistry)

        async def _record_kill(_task_id: str) -> int:
            order.append("kill_for_task")
            return 1

        process_registry.kill_for_task.side_effect = _record_kill

        # cli.execute hangs so the task stays in-flight until cancelled.
        async def _hang(_: object) -> MagicMock:
            try:
                await asyncio.sleep(999)
            except asyncio.CancelledError:
                order.append("asyncio_cancel")
                raise
            return MagicMock()  # never reached

        cli = _make_cli_service()
        cli.execute = AsyncMock(side_effect=_hang)

        hub = TaskHub(
            registry,
            MagicMock(workspace=tmp_path),
            cli_service=cli,
            config=_make_config(),
            process_registry=process_registry,
        )
        hub.set_result_handler("main", AsyncMock())

        task_id = hub.submit(_submit())
        await asyncio.sleep(0.05)  # let the task actually start awaiting the pipe

        success = await hub.cancel(task_id)
        assert success

        process_registry.kill_for_task.assert_awaited_once_with(task_id)
        # Kill-order invariant: subprocess kill strictly precedes asyncio cancel.
        assert order == ["kill_for_task", "asyncio_cancel"], (
            f"expected kill-before-cancel, got {order!r}"
        )

    async def test_cancel_all_kills_each_tasks_subprocess(
        self, registry: TaskRegistry, tmp_path: Path
    ) -> None:
        """cancel_all() must call kill_for_task once per in-flight task for the chat."""
        process_registry = AsyncMock(spec=ProcessRegistry)
        process_registry.kill_for_task.return_value = 1

        async def _hang(_: object) -> MagicMock:
            await asyncio.sleep(999)
            return MagicMock()

        cli = _make_cli_service()
        cli.execute = AsyncMock(side_effect=_hang)

        hub = TaskHub(
            registry,
            MagicMock(workspace=tmp_path),
            cli_service=cli,
            config=_make_config(),
            process_registry=process_registry,
        )
        hub.set_result_handler("main", AsyncMock())

        task_id_a = hub.submit(_submit(name="A"))
        task_id_b = hub.submit(_submit(name="B"))
        await asyncio.sleep(0.05)

        count = await hub.cancel_all(42)
        assert count == 2
        assert process_registry.kill_for_task.await_count == 2
        awaited_ids = {c.args[0] for c in process_registry.kill_for_task.await_args_list}
        assert awaited_ids == {task_id_a, task_id_b}

    async def test_cancel_is_noop_when_process_registry_is_none(
        self, registry: TaskRegistry, tmp_path: Path
    ) -> None:
        """Without a process_registry the old behavior is preserved (asyncio cancel only)."""
        cancel_recorded = asyncio.Event()

        async def _hang(_: object) -> MagicMock:
            try:
                await asyncio.sleep(999)
            except asyncio.CancelledError:
                cancel_recorded.set()
                raise
            return MagicMock()

        cli = _make_cli_service()
        cli.execute = AsyncMock(side_effect=_hang)

        hub = TaskHub(
            registry,
            MagicMock(workspace=tmp_path),
            cli_service=cli,
            config=_make_config(),
            # NOTE: no process_registry kwarg — backward compat path.
        )
        hub.set_result_handler("main", AsyncMock())

        task_id = hub.submit(_submit())
        await asyncio.sleep(0.05)

        success = await hub.cancel(task_id)
        assert success
        assert cancel_recorded.is_set(), "asyncio_task.cancel() must still fire with no registry"


class TestForwardQuestion:
    async def test_forwards_and_returns_immediately(
        self, registry: TaskRegistry, tmp_path: Path
    ) -> None:
        hub = TaskHub(
            registry,
            MagicMock(workspace=tmp_path),
            cli_service=_make_cli_service(),
            config=_make_config(),
        )

        entry = registry.create(_submit("build a website"), "claude", "opus")

        question_handler = AsyncMock()
        hub.set_question_handler("main", question_handler)

        result = await hub.forward_question(entry.task_id, "Which framework?")
        assert "forwarded" in result.lower()

        # Handler is called asynchronously (fire-and-forget)
        await asyncio.sleep(0.05)
        question_handler.assert_called_once()

    async def test_increments_question_count(self, registry: TaskRegistry, tmp_path: Path) -> None:
        hub = TaskHub(
            registry,
            MagicMock(workspace=tmp_path),
            cli_service=_make_cli_service(),
            config=_make_config(),
        )
        entry = registry.create(_submit(), "claude", "opus")
        hub.set_question_handler("main", AsyncMock())

        await hub.forward_question(entry.task_id, "question 1")
        await hub.forward_question(entry.task_id, "question 2")

        updated = registry.get(entry.task_id)
        assert updated is not None
        assert updated.question_count == 2

    async def test_unknown_task(self, registry: TaskRegistry, tmp_path: Path) -> None:
        hub = TaskHub(
            registry,
            MagicMock(workspace=tmp_path),
            cli_service=_make_cli_service(),
            config=_make_config(),
        )
        result = await hub.forward_question("nonexistent", "question?")
        assert "not found" in result.lower()

    async def test_no_handler(self, registry: TaskRegistry, tmp_path: Path) -> None:
        hub = TaskHub(
            registry,
            MagicMock(workspace=tmp_path),
            cli_service=_make_cli_service(),
            config=_make_config(),
        )
        entry = registry.create(_submit(), "claude", "opus")
        result = await hub.forward_question(entry.task_id, "question?")
        assert "no question handler" in result.lower()


class TestWaitingStatus:
    async def test_task_with_question_gets_waiting_status(
        self, registry: TaskRegistry, tmp_path: Path
    ) -> None:
        """Task that asks a question should end as 'waiting', not 'done'."""
        cli = _make_cli_service()
        hub = TaskHub(
            registry,
            MagicMock(workspace=tmp_path),
            cli_service=cli,
            config=_make_config(),
        )
        hub.set_result_handler("main", AsyncMock())
        hub.set_question_handler("main", AsyncMock())

        task_id = hub.submit(_submit())

        # Simulate: task asks a question while running (before CLI returns)
        entry = registry.get(task_id)
        assert entry is not None
        await hub.forward_question(task_id, "Which framework?")

        # Wait for CLI to complete
        await asyncio.sleep(0.1)

        entry = registry.get(task_id)
        assert entry is not None
        assert entry.status == "waiting"
        assert entry.last_question == "Which framework?"

    async def test_resume_from_waiting(self, registry: TaskRegistry, tmp_path: Path) -> None:
        """Resuming a 'waiting' task should work and clear the question."""
        cli = _make_cli_service()
        hub = TaskHub(
            registry,
            MagicMock(workspace=tmp_path),
            cli_service=cli,
            config=_make_config(),
        )
        hub.set_result_handler("main", AsyncMock())
        hub.set_question_handler("main", AsyncMock())

        task_id = hub.submit(_submit())
        await hub.forward_question(task_id, "Which framework?")
        await asyncio.sleep(0.1)

        entry = registry.get(task_id)
        assert entry is not None
        assert entry.status == "waiting"

        resumed_id = hub.resume(task_id, "Use React")
        assert resumed_id == task_id
        await asyncio.sleep(0.1)

        entry = registry.get(task_id)
        assert entry is not None
        assert entry.status == "done"
        assert entry.last_question == ""


class TestResume:
    def _hub(self, registry: TaskRegistry, tmp_path: Path, **cli_kw: str) -> TaskHub:
        hub = TaskHub(
            registry,
            MagicMock(workspace=tmp_path),
            cli_service=_make_cli_service(**cli_kw),
            config=_make_config(),
        )
        hub.set_result_handler("main", AsyncMock())
        return hub

    async def test_resume_reuses_same_task(self, registry: TaskRegistry, tmp_path: Path) -> None:
        hub = self._hub(registry, tmp_path)
        task_id = hub.submit(_submit())
        await asyncio.sleep(0.1)

        entry = registry.get(task_id)
        assert entry is not None
        assert entry.status == "done"
        assert entry.session_id == "sess-1"

        resumed_id = hub.resume(task_id, "now for 2 weeks")
        assert resumed_id == task_id  # Same task, no new entry
        await asyncio.sleep(0.1)

        entry = registry.get(task_id)
        assert entry is not None
        assert entry.status == "done"  # Completed again
        assert entry.name == "Test Task"

    async def test_resume_uses_original_provider_model(
        self, registry: TaskRegistry, tmp_path: Path
    ) -> None:
        hub = self._hub(registry, tmp_path)
        entry = registry.create(_submit(), "codex", "gpt-4.1", thinking="high")
        registry.update_status(entry.task_id, "done", session_id="codex-sess")

        resumed_id = hub.resume(entry.task_id, "follow up")
        assert resumed_id == entry.task_id

        updated = registry.get(entry.task_id)
        assert updated is not None
        assert updated.provider == "codex"
        assert updated.model == "gpt-4.1"
        assert updated.thinking == "high"

    def test_resume_fails_if_no_session_id(self, registry: TaskRegistry, tmp_path: Path) -> None:
        hub = self._hub(registry, tmp_path)
        entry = registry.create(_submit(), "claude", "opus")
        registry.update_status(entry.task_id, "done")  # No session_id

        with pytest.raises(ValueError, match="no resumable session"):
            hub.resume(entry.task_id, "follow up")

    def test_resume_fails_if_still_running(self, registry: TaskRegistry, tmp_path: Path) -> None:
        hub = self._hub(registry, tmp_path)
        entry = registry.create(_submit(), "claude", "opus")
        # Status is "running" by default

        with pytest.raises(ValueError, match="still running"):
            hub.resume(entry.task_id, "follow up")

    async def test_resume_rejected_while_execution_in_flight(
        self, registry: TaskRegistry, tmp_path: Path
    ) -> None:
        """#158: a stale resumable status must not allow a second overlapping run."""

        async def _hang(_: object) -> MagicMock:
            await asyncio.sleep(999)
            return MagicMock()  # never reached

        cli = _make_cli_service()
        cli.execute = AsyncMock(side_effect=_hang)
        hub = TaskHub(
            registry,
            MagicMock(workspace=tmp_path),
            cli_service=cli,
            config=_make_config(),
        )
        hub.set_result_handler("main", AsyncMock())

        task_id = hub.submit(_submit())
        await asyncio.sleep(0.05)  # execution is now hanging in-flight

        # Simulate a stale persisted status that lags behind the running process.
        registry.update_status(task_id, "waiting", session_id="sess-1")

        with pytest.raises(ValueError, match="already running"):
            hub.resume(task_id, "follow up")

        await hub.shutdown()

    def test_resume_fails_if_no_provider(self, registry: TaskRegistry, tmp_path: Path) -> None:
        hub = self._hub(registry, tmp_path)
        entry = registry.create(_submit(), "", "")
        registry.update_status(entry.task_id, "done", session_id="sess-1")

        with pytest.raises(ValueError, match="no provider recorded"):
            hub.resume(entry.task_id, "follow up")

    def test_resume_fails_if_task_not_found(self, registry: TaskRegistry, tmp_path: Path) -> None:
        hub = self._hub(registry, tmp_path)
        with pytest.raises(ValueError, match="not found"):
            hub.resume("nonexistent", "follow up")


class TestThinkingPersisted:
    async def test_thinking_stored_on_entry(self, registry: TaskRegistry, tmp_path: Path) -> None:
        submit = TaskSubmit(
            chat_id=42,
            prompt="test",
            message_id=1,
            thread_id=None,
            parent_agent="main",
            name="Think Test",
            thinking_override="high",
        )
        hub = TaskHub(
            registry,
            MagicMock(workspace=tmp_path),
            cli_service=_make_cli_service(),
            config=_make_config(),
        )
        hub.set_result_handler("main", AsyncMock())

        task_id = hub.submit(submit)
        entry = registry.get(task_id)
        assert entry is not None
        assert entry.thinking == "high"


class TestNumTurns:
    async def test_num_turns_stored_on_completion(
        self, registry: TaskRegistry, tmp_path: Path
    ) -> None:
        hub = TaskHub(
            registry,
            MagicMock(workspace=tmp_path),
            cli_service=_make_cli_service(num_turns=7),
            config=_make_config(),
        )
        hub.set_result_handler("main", AsyncMock())

        task_id = hub.submit(_submit())
        await asyncio.sleep(0.1)

        entry = registry.get(task_id)
        assert entry is not None
        assert entry.num_turns == 7

    async def test_resume_accumulates_turns(self, registry: TaskRegistry, tmp_path: Path) -> None:
        """Resumed task carries forward + adds new turns."""
        cli = _make_cli_service(num_turns=5)
        hub = TaskHub(
            registry,
            MagicMock(workspace=tmp_path),
            cli_service=cli,
            config=_make_config(),
        )
        hub.set_result_handler("main", AsyncMock())

        task_id = hub.submit(_submit())
        await asyncio.sleep(0.1)

        original = registry.get(task_id)
        assert original is not None
        assert original.num_turns == 5

        # Resume — CLI returns 3 more turns
        cli.execute.return_value.num_turns = 3
        resumed_id = hub.resume(task_id, "follow up")
        assert resumed_id == task_id
        await asyncio.sleep(0.1)

        entry = registry.get(task_id)
        assert entry is not None
        assert entry.num_turns == 8  # 5 carried + 3 new


class TestPerAgentTasksDir:
    async def test_task_folder_in_agent_workspace(
        self, registry: TaskRegistry, tmp_path: Path
    ) -> None:
        """Task folders land in the submitting agent's workspace."""
        from ductor_bot.workspace.paths import DuctorPaths

        agent_home = tmp_path / "agents" / "test"
        agent_paths = DuctorPaths(ductor_home=agent_home)

        hub = TaskHub(
            registry,
            MagicMock(workspace=tmp_path),
            cli_service=_make_cli_service(),
            config=_make_config(),
        )
        hub.set_agent_paths("test", agent_paths)
        hub.set_result_handler("test", AsyncMock())

        submit = TaskSubmit(
            chat_id=99,
            prompt="do stuff",
            message_id=1,
            thread_id=None,
            parent_agent="test",
            name="Agent Task",
        )
        task_id = hub.submit(submit)
        await asyncio.sleep(0.1)

        # Task folder should be in agent's workspace, not main
        entry = registry.get(task_id)
        assert entry is not None
        assert str(agent_home) in entry.tasks_dir

        folder = registry.task_folder(task_id)
        assert str(agent_home) in str(folder)
        assert folder.is_dir()
        assert (folder / "TASKMEMORY.md").is_file()

        # Default main tasks dir should NOT have this task
        assert not (tmp_path / "tasks" / task_id).exists()

        await hub.shutdown()

    async def test_main_agent_uses_default_dir(
        self, registry: TaskRegistry, tmp_path: Path
    ) -> None:
        """Main agent tasks use the default tasks_dir when no override registered."""
        hub = TaskHub(
            registry,
            MagicMock(workspace=tmp_path),
            cli_service=_make_cli_service(),
            config=_make_config(),
        )
        hub.set_result_handler("main", AsyncMock())

        task_id = hub.submit(_submit())
        await asyncio.sleep(0.1)

        folder = registry.task_folder(task_id)
        assert str(tmp_path / "tasks") in str(folder)

        await hub.shutdown()


class TestPerAgentCLI:
    async def test_uses_agent_specific_cli(self, registry: TaskRegistry, tmp_path: Path) -> None:
        """Tasks use the CLI service registered for their parent_agent."""
        main_cli = _make_cli_service("main-output")
        sub_cli = _make_cli_service("sub-output")

        hub = TaskHub(
            registry,
            MagicMock(workspace=tmp_path),
            cli_service=main_cli,
            config=_make_config(),
        )
        hub.set_cli_service("sub1", sub_cli)

        delivered: list[TaskResult] = []
        hub.set_result_handler("sub1", AsyncMock(side_effect=delivered.append))

        submit = TaskSubmit(
            chat_id=99,
            prompt="do stuff",
            message_id=1,
            thread_id=None,
            parent_agent="sub1",
            name="Sub Task",
        )
        hub.submit(submit)
        await asyncio.sleep(0.1)

        # sub_cli should have been called, not main_cli
        sub_cli.execute.assert_called_once()
        main_cli.execute.assert_not_called()
        assert len(delivered) == 1
        assert delivered[0].result_text.startswith("sub-output")

        await hub.shutdown()

    async def test_falls_back_to_default_cli(self, registry: TaskRegistry, tmp_path: Path) -> None:
        """Tasks fall back to default CLI when no per-agent CLI is registered."""
        default_cli = _make_cli_service("default-output")

        hub = TaskHub(
            registry,
            MagicMock(workspace=tmp_path),
            cli_service=default_cli,
            config=_make_config(),
        )

        delivered: list[TaskResult] = []
        hub.set_result_handler("unknown_agent", AsyncMock(side_effect=delivered.append))

        submit = TaskSubmit(
            chat_id=99,
            prompt="do stuff",
            message_id=1,
            thread_id=None,
            parent_agent="unknown_agent",
            name="Fallback Task",
        )
        hub.submit(submit)
        await asyncio.sleep(0.1)

        default_cli.execute.assert_called_once()
        assert len(delivered) == 1
        assert delivered[0].result_text.startswith("default-output")

        await hub.shutdown()

    async def test_enabled_with_only_per_agent_cli(
        self, registry: TaskRegistry, tmp_path: Path
    ) -> None:
        """Hub works when only per-agent CLIs are set (no default)."""
        hub = TaskHub(
            registry,
            MagicMock(workspace=tmp_path),
            cli_service=None,
            config=_make_config(),
        )
        agent_cli = _make_cli_service("agent-output")
        hub.set_cli_service("main", agent_cli)

        delivered: list[TaskResult] = []
        hub.set_result_handler("main", AsyncMock(side_effect=delivered.append))

        hub.submit(_submit())
        await asyncio.sleep(0.1)

        agent_cli.execute.assert_called_once()
        assert len(delivered) == 1

        await hub.shutdown()


class TestTopicIdPlumbing:
    """#74: TaskEntry.thread_id must flow into AgentRequest.topic_id so that
    DUCTOR_TOPIC_ID is set in the task subprocess env. Without this, sub-tasks
    created from within a running task lose the originating topic context and
    route their results to the base/General topic."""

    async def test_run_passes_topic_id_to_agent_request(
        self, registry: TaskRegistry, tmp_path: Path
    ) -> None:
        """AgentRequest.topic_id equals TaskEntry.thread_id when the latter is set."""
        cli = _make_cli_service()

        hub = TaskHub(
            registry,
            MagicMock(workspace=tmp_path),
            cli_service=cli,
            config=_make_config(),
        )
        hub.set_result_handler("main", AsyncMock())

        submit = TaskSubmit(
            chat_id=42,
            prompt="do stuff in a topic",
            message_id=1,
            thread_id=5150,
            parent_agent="main",
            name="Topic Task",
        )
        hub.submit(submit)
        await asyncio.sleep(0.1)

        # Capture the AgentRequest passed to cli.execute.
        cli.execute.assert_called_once()
        agent_request = cli.execute.call_args[0][0]
        assert agent_request.topic_id == 5150
        assert agent_request.chat_id == 42

        await hub.shutdown()

    async def test_run_passes_none_topic_id_when_thread_id_missing(
        self, registry: TaskRegistry, tmp_path: Path
    ) -> None:
        """Backward-compat: thread_id=None yields topic_id=None on AgentRequest."""
        cli = _make_cli_service()

        hub = TaskHub(
            registry,
            MagicMock(workspace=tmp_path),
            cli_service=cli,
            config=_make_config(),
        )
        hub.set_result_handler("main", AsyncMock())

        hub.submit(_submit())  # thread_id=None via helper
        await asyncio.sleep(0.1)

        cli.execute.assert_called_once()
        agent_request = cli.execute.call_args[0][0]
        assert agent_request.topic_id is None

        await hub.shutdown()


class TestPerAgentDeliveryIsolation:
    """#73: TaskResult delivery must route through the parent_agent's registered
    handler only -- sibling agents' handlers MUST NOT see results that weren't
    addressed to them. Locks in the architectural per-agent routing so a future
    refactor cannot silently regress it into delivering everything to main."""

    async def test_result_isolation_between_agents(
        self, registry: TaskRegistry, tmp_path: Path
    ) -> None:
        """A task with parent_agent='sub1' invokes only sub1's handler."""
        main_handler = AsyncMock()
        sub1_handler = AsyncMock()
        sub2_handler = AsyncMock()

        sub_cli = _make_cli_service("sub1-output")

        hub = TaskHub(
            registry,
            MagicMock(workspace=tmp_path),
            cli_service=_make_cli_service("main-output"),
            config=_make_config(),
        )
        hub.set_cli_service("sub1", sub_cli)
        hub.set_result_handler("main", main_handler)
        hub.set_result_handler("sub1", sub1_handler)
        hub.set_result_handler("sub2", sub2_handler)

        submit = TaskSubmit(
            chat_id=55,
            prompt="sub-agent task",
            message_id=1,
            thread_id=None,
            parent_agent="sub1",
            name="Sub1 Task",
        )
        hub.submit(submit)
        await asyncio.sleep(0.1)

        sub1_handler.assert_called_once()
        main_handler.assert_not_called()
        sub2_handler.assert_not_called()

        delivered_result = sub1_handler.call_args[0][0]
        assert delivered_result.parent_agent == "sub1"

        await hub.shutdown()


class TestMaintenance:
    async def test_maintenance_cleans_finished_retention(
        self, registry: TaskRegistry, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Periodic maintenance should prune finished task history, not just orphans."""
        old_done = registry.create(_submit(name="old"), "claude", "opus")
        recent_done = registry.create(_submit(name="recent"), "claude", "opus")
        registry.update_status(old_done.task_id, "done")
        registry.update_status(recent_done.task_id, "done")
        old_done.completed_at = 1.0
        old_done.created_at = 1.0
        recent_done.completed_at = 10_000.0
        recent_done.created_at = 10_000.0
        registry._persist()

        async def fake_sleep(_: float) -> None:
            raise asyncio.CancelledError

        monkeypatch.setattr("ductor_bot.tasks.hub.asyncio.sleep", fake_sleep)
        monkeypatch.setattr("ductor_bot.tasks.registry.time.time", lambda: 10_000.0)

        hub = TaskHub(
            registry,
            MagicMock(workspace=tmp_path),
            cli_service=_make_cli_service(),
            config=_make_config(
                finished_retention_hours=1,
                finished_keep_last=100,
            ),
        )

        await hub._maintenance_loop()

        assert registry.get(old_done.task_id) is None
        assert registry.get(recent_done.task_id) is not None


class TestAppendTaskmemory:
    """#91: _append_taskmemory must emit a WARNING log and include the original
    length + full file path in the suffix when truncation occurs. Without this,
    parent agents receive silently-truncated memory content."""

    def test_warns_on_truncation(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        from ductor_bot.tasks.hub import _TASKMEMORY_MAX_LEN, _append_taskmemory

        memory_file = tmp_path / "TASKMEMORY.md"
        original_len = _TASKMEMORY_MAX_LEN + 1000
        memory_file.write_text("X" * original_len, encoding="utf-8")

        with caplog.at_level("WARNING", logger="ductor_bot.tasks.hub"):
            result = _append_taskmemory("result_text", memory_file)

        # WARNING log fired
        assert any("TASKMEMORY truncated" in rec.message for rec in caplog.records)
        # Suffix shows original length so the parent agent knows how much was cut
        assert str(original_len) in result
        # Suffix points to the full file path so the parent agent can read it
        assert str(memory_file) in result
        assert "truncated" in result.lower()

    def test_no_warning_under_limit(self, tmp_path: Path, caplog: pytest.LogCaptureFixture) -> None:
        from ductor_bot.tasks.hub import _append_taskmemory

        memory_file = tmp_path / "TASKMEMORY.md"
        memory_file.write_text("short content", encoding="utf-8")

        with caplog.at_level("WARNING", logger="ductor_bot.tasks.hub"):
            result = _append_taskmemory("result_text", memory_file)

        assert not any("TASKMEMORY truncated" in rec.message for rec in caplog.records)
        assert "truncated" not in result.lower()
        assert "short content" in result


class TestAppendSystemPromptFiles:
    async def test_uses_parent_agent_workspace_files(
        self, registry: TaskRegistry, tmp_path: Path
    ) -> None:
        """_run reads append_system_prompt_files from the parent agent's workspace."""
        cli = _make_cli_service("task output")
        hub = TaskHub(
            registry,
            MagicMock(workspace=tmp_path / "main_ws"),
            cli_service=cli,
            config=_make_config(),
            append_system_prompt_files=["PERSONA.md"],
        )
        # Parent agent "dev" has its own workspace + persona file.
        dev_paths = DuctorPaths(ductor_home=tmp_path / "dev_home")
        dev_paths.workspace.mkdir(parents=True, exist_ok=True)
        (dev_paths.workspace / "PERSONA.md").write_text("Dev persona.")
        hub.set_agent_paths("dev", dev_paths)
        hub.set_result_handler("dev", AsyncMock())

        submit = TaskSubmit(
            chat_id=42, prompt="go", message_id=1, thread_id=None, parent_agent="dev", name="T"
        )
        hub.submit(submit)
        await asyncio.sleep(0.1)

        await hub.shutdown()
        request = cli.execute.await_args.args[0]
        assert request.append_system_prompt is not None
        assert "Dev persona." in request.append_system_prompt

    async def test_no_files_leaves_append_none(
        self, registry: TaskRegistry, tmp_path: Path
    ) -> None:
        cli = _make_cli_service("task output")
        hub = TaskHub(
            registry,
            MagicMock(workspace=tmp_path),
            cli_service=cli,
            config=_make_config(),
        )
        hub.set_result_handler("main", AsyncMock())
        hub.submit(_submit())
        await asyncio.sleep(0.1)

        await hub.shutdown()
        request = cli.execute.await_args.args[0]
        assert request.append_system_prompt is None
