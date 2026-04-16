"""Tests for TaskHub."""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from ductor_bot.tasks.hub import TaskHub
from ductor_bot.tasks.models import TaskResult, TaskSubmit
from ductor_bot.tasks.registry import TaskRegistry


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
    config.capacity_retry_attempts = 2
    config.capacity_retry_base_delay_seconds = 0.01
    config.capacity_retry_jitter_seconds = 0.0
    config.capacity_retry_max_delay_seconds = 0.02
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

    async def test_retries_capacity_then_succeeds(
        self, registry: TaskRegistry, tmp_path: Path
    ) -> None:
        cli = _make_cli_service()
        capacity = MagicMock(
            result='{"type":"turn.failed","error":{"message":"Selected model is at capacity. Please try a different model."}}',
            session_id="sess-cap",
            is_error=True,
            timed_out=False,
            num_turns=0,
        )
        success = MagicMock(
            result="task output",
            session_id="sess-ok",
            is_error=False,
            timed_out=False,
            num_turns=2,
        )
        cli.execute = AsyncMock(side_effect=[capacity, success])

        delivered: list[TaskResult] = []
        hub = TaskHub(
            registry,
            MagicMock(workspace=tmp_path),
            cli_service=cli,
            config=_make_config(),
        )
        hub.set_result_handler("main", AsyncMock(side_effect=delivered.append))

        task_id = hub.submit(_submit())
        await asyncio.sleep(0.2)

        assert cli.execute.await_count == 2
        assert len(delivered) == 1
        assert delivered[0].status == "done"

        entry = registry.get(task_id)
        assert entry is not None
        assert entry.status == "done"
        assert entry.session_id == "sess-ok"

        await hub.shutdown()

    async def test_capacity_retry_exhaustion_fails_cleanly(
        self, registry: TaskRegistry, tmp_path: Path
    ) -> None:
        cli = _make_cli_service()
        cli.execute = AsyncMock(
            return_value=MagicMock(
                result='{"type":"turn.failed","error":{"message":"Selected model is at capacity. Please try a different model."}}',
                session_id="sess-cap",
                is_error=True,
                timed_out=False,
                num_turns=0,
            )
        )

        delivered: list[TaskResult] = []
        hub = TaskHub(
            registry,
            MagicMock(workspace=tmp_path),
            cli_service=cli,
            config=_make_config(capacity_retry_attempts=2),
        )
        hub.set_result_handler("main", AsyncMock(side_effect=delivered.append))

        hub.submit(_submit())
        await asyncio.sleep(0.2)

        assert cli.execute.await_count == 3
        assert len(delivered) == 1
        assert delivered[0].status == "failed"
        assert "provider capacity persisted after 3 attempt" in delivered[0].error.lower()

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
        await asyncio.sleep(0.05)

        success = await hub.cancel(task_id)
        assert success
        await asyncio.sleep(0.05)

        assert len(delivered) == 1
        assert delivered[0].status == "cancelled"

        entry = registry.get(task_id)
        assert entry is not None
        assert entry.status == "cancelled"

    async def test_cancel_nonexistent(self, registry: TaskRegistry, tmp_path: Path) -> None:
        hub = TaskHub(
            registry,
            MagicMock(workspace=tmp_path),
            cli_service=_make_cli_service(),
            config=_make_config(),
        )
        assert not await hub.cancel("nonexistent")


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
