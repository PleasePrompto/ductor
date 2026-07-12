"""Tests for BackgroundObserver: submit, execute, cancel, deliver."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ductor_bot.background.models import BackgroundResult, BackgroundSubmit
from ductor_bot.background.observer import MAX_TASKS_PER_CHAT, BackgroundObserver
from ductor_bot.cli.param_resolver import TaskExecutionConfig
from ductor_bot.cli.types import AgentResponse
from ductor_bot.config import AgentConfig
from ductor_bot.cron.execution import OneShotExecutionResult
from ductor_bot.infra.task_runner import TaskResult
from ductor_bot.workspace.paths import DuctorPaths


def _sub(
    chat_id: int = 123,
    prompt: str = "",
    message_id: int = 1,
    *,
    thread_id: int | None = None,
    transport: str = "tg",
) -> BackgroundSubmit:
    return BackgroundSubmit(
        chat_id=chat_id,
        prompt=prompt,
        message_id=message_id,
        thread_id=thread_id,
        transport=transport,
    )


def _make_paths(tmp_path: Path) -> DuctorPaths:
    fw = tmp_path / "fw"
    paths = DuctorPaths(
        ductor_home=tmp_path / "home", home_defaults=fw / "workspace", framework_root=fw
    )
    paths.workspace.mkdir(parents=True, exist_ok=True)
    return paths


def _make_exec_config(**overrides: Any) -> TaskExecutionConfig:
    defaults: dict[str, Any] = {
        "provider": "claude",
        "model": "sonnet",
        "reasoning_effort": "",
        "cli_parameters": [],
        "permission_mode": "bypassPermissions",
        "working_dir": "/tmp/test",
        "file_access": "workspace",
    }
    defaults.update(overrides)
    return TaskExecutionConfig(**defaults)


def _make_observer(
    paths: DuctorPaths, timeout: float = 300.0, config: AgentConfig | None = None
) -> BackgroundObserver:
    return BackgroundObserver(paths, timeout_seconds=timeout, config=config or AgentConfig())


def _success_task_result(text: str = "") -> TaskResult:
    return TaskResult(
        status="success",
        result_text=text,
        execution=OneShotExecutionResult(
            status="success",
            result_text=text,
            stdout=b"",
            stderr=b"",
            returncode=0,
            timed_out=False,
        ),
    )


def _cli_not_found_task_result() -> TaskResult:
    return TaskResult(
        status="error:cli_not_found_claude",
        result_text="[claude CLI not found]",
        execution=None,
    )


def _blocking_run(event: asyncio.Event) -> AsyncMock:
    """Return a mock run_oneshot_task that blocks until *event* is set."""

    async def _slow(*_args: Any, **_kw: Any) -> TaskResult:
        await event.wait()
        return _success_task_result()

    return AsyncMock(side_effect=_slow)


@pytest.fixture
def paths(tmp_path: Path) -> DuctorPaths:
    return _make_paths(tmp_path)


@pytest.fixture
async def observer(paths: DuctorPaths) -> AsyncIterator[BackgroundObserver]:
    obs = _make_observer(paths)
    yield obs
    await obs.shutdown()
    await asyncio.sleep(0.01)


class TestSubmit:
    async def test_returns_task_id(self, observer: BackgroundObserver) -> None:
        config = _make_exec_config()
        with patch(
            "ductor_bot.background.observer.run_oneshot_task",
            return_value=_cli_not_found_task_result(),
        ):
            handler = AsyncMock()
            observer.set_result_handler(handler)
            task_id = observer.submit(_sub(prompt="test prompt"), config)
            assert isinstance(task_id, str)
            assert len(task_id) == 8

    async def test_task_appears_in_active(self, observer: BackgroundObserver) -> None:
        config = _make_exec_config()
        event = asyncio.Event()
        with patch(
            "ductor_bot.background.observer.run_oneshot_task",
            new=_blocking_run(event),
        ):
            observer.set_result_handler(AsyncMock())
            observer.submit(_sub(prompt="test"), config)
            await asyncio.sleep(0)
            assert len(observer.active_tasks(123)) == 1
            assert len(observer.active_tasks(999)) == 0
            event.set()
            await asyncio.sleep(0.05)

    async def test_max_tasks_limit(self, observer: BackgroundObserver) -> None:
        config = _make_exec_config()
        event = asyncio.Event()
        with patch(
            "ductor_bot.background.observer.run_oneshot_task",
            new=_blocking_run(event),
        ):
            observer.set_result_handler(AsyncMock())
            for _ in range(MAX_TASKS_PER_CHAT):
                observer.submit(_sub(prompt="task"), config)

            with pytest.raises(ValueError, match="Too many"):
                observer.submit(_sub(prompt="one more"), config)

            event.set()
            await asyncio.sleep(0.05)


class TestExecution:
    async def test_success_delivers_result(self, observer: BackgroundObserver) -> None:
        config = _make_exec_config()
        handler = AsyncMock()
        observer.set_result_handler(handler)

        result = _success_task_result("Hello world")
        with patch("ductor_bot.background.observer.run_oneshot_task", return_value=result):
            observer.submit(_sub(prompt="say hello", message_id=42), config)
            await asyncio.sleep(0.05)

        handler.assert_awaited_once()
        bg_result: BackgroundResult = handler.call_args[0][0]
        assert bg_result.status == "success"
        assert bg_result.result_text == "Hello world"
        assert bg_result.chat_id == 123
        assert bg_result.message_id == 42
        assert bg_result.prompt_preview == "say hello"
        assert bg_result.transport == "tg"

    async def test_preserves_transport_and_thread_target(
        self, observer: BackgroundObserver
    ) -> None:
        config = _make_exec_config()
        handler = AsyncMock()
        observer.set_result_handler(handler)

        result = _success_task_result("Hello thread")
        with patch("ductor_bot.background.observer.run_oneshot_task", return_value=result):
            observer.submit(_sub(prompt="say hello", thread_id=77, transport="sl"), config)
            await asyncio.sleep(0.05)

        bg_result: BackgroundResult = handler.call_args[0][0]
        assert bg_result.thread_id == 77
        assert bg_result.transport == "sl"

    async def test_cli_not_found(self, observer: BackgroundObserver) -> None:
        config = _make_exec_config()
        handler = AsyncMock()
        observer.set_result_handler(handler)

        with patch(
            "ductor_bot.background.observer.run_oneshot_task",
            return_value=_cli_not_found_task_result(),
        ):
            observer.submit(_sub(prompt="test"), config)
            await asyncio.sleep(0.05)

        handler.assert_awaited_once()
        bg_result: BackgroundResult = handler.call_args[0][0]
        assert bg_result.status == "error:cli_not_found"

    async def test_timeout_status(self, observer: BackgroundObserver) -> None:
        config = _make_exec_config()
        handler = AsyncMock()
        observer.set_result_handler(handler)

        result = TaskResult(
            status="error:timeout",
            result_text="timed out",
            execution=OneShotExecutionResult(
                status="error:timeout",
                result_text="timed out",
                stdout=b"",
                stderr=b"",
                returncode=None,
                timed_out=True,
            ),
        )
        with patch("ductor_bot.background.observer.run_oneshot_task", return_value=result):
            observer.submit(_sub(prompt="slow task"), config)
            await asyncio.sleep(0.05)

        bg_result: BackgroundResult = handler.call_args[0][0]
        assert bg_result.status == "error:timeout"


class TestCancel:
    async def test_cancel_all(self, observer: BackgroundObserver) -> None:
        config = _make_exec_config()
        event = asyncio.Event()
        handler = AsyncMock()
        observer.set_result_handler(handler)

        with patch(
            "ductor_bot.background.observer.run_oneshot_task",
            new=_blocking_run(event),
        ):
            observer.submit(_sub(prompt="task1"), config)
            observer.submit(_sub(prompt="task2", message_id=2), config)
            await asyncio.sleep(0)

            cancelled = await observer.cancel_all(123)
            assert cancelled == 2
            await asyncio.sleep(0.05)

    async def test_cancel_delivers_aborted(self, observer: BackgroundObserver) -> None:
        config = _make_exec_config()
        event = asyncio.Event()
        handler = AsyncMock()
        observer.set_result_handler(handler)

        with patch(
            "ductor_bot.background.observer.run_oneshot_task",
            new=_blocking_run(event),
        ):
            observer.submit(_sub(prompt="cancellable"), config)
            await asyncio.sleep(0)

            await observer.cancel_all(123)
            await asyncio.sleep(0.05)

        aborted_calls = [c for c in handler.call_args_list if c[0][0].status == "aborted"]
        assert len(aborted_calls) == 1

    async def test_shutdown_cancels_all(self, observer: BackgroundObserver) -> None:
        config = _make_exec_config()
        event = asyncio.Event()

        with patch(
            "ductor_bot.background.observer.run_oneshot_task",
            new=_blocking_run(event),
        ):
            observer.set_result_handler(AsyncMock())
            observer.submit(_sub(prompt="t1"), config)
            observer.submit(_sub(chat_id=456, prompt="t2", message_id=2), config)
            await asyncio.sleep(0)

            await observer.shutdown()
            assert len(observer.active_tasks()) == 0


class TestCleanup:
    async def test_task_removed_after_completion(self, observer: BackgroundObserver) -> None:
        config = _make_exec_config()
        handler = AsyncMock()
        observer.set_result_handler(handler)

        result = _success_task_result("ok")
        with patch("ductor_bot.background.observer.run_oneshot_task", return_value=result):
            observer.submit(_sub(prompt="quick"), config)
            await asyncio.sleep(0.05)

        assert len(observer.active_tasks(123)) == 0


class TestAppendSystemPromptFiles:
    async def test_named_session_injects_configured_files(self, tmp_path: Path) -> None:
        """_run_with_session injects append_system_prompt_files from the workspace."""
        paths = _make_paths(tmp_path)
        (paths.workspace / "PERSONA.md").write_text("Bg persona.")
        config = AgentConfig(append_system_prompt_files=["PERSONA.md"])
        cli = MagicMock()
        cli.execute = AsyncMock(return_value=AgentResponse(result="ok", session_id="s1"))
        obs = _make_observer(paths, config=config)
        object.__setattr__(obs, "_cli_service", cli)
        obs.set_result_handler(AsyncMock())

        sub = BackgroundSubmit(
            chat_id=1, prompt="hi", message_id=1, thread_id=None, session_name="work"
        )
        obs.submit(sub, _make_exec_config())
        await asyncio.sleep(0.05)
        await obs.shutdown()

        request = cli.execute.await_args.args[0]
        assert request.append_system_prompt is not None
        assert "Bg persona." in request.append_system_prompt

    async def test_named_session_no_files_leaves_append_none(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        cli = MagicMock()
        cli.execute = AsyncMock(return_value=AgentResponse(result="ok", session_id="s1"))
        obs = _make_observer(paths, config=AgentConfig())
        object.__setattr__(obs, "_cli_service", cli)
        obs.set_result_handler(AsyncMock())

        sub = BackgroundSubmit(
            chat_id=1, prompt="hi", message_id=1, thread_id=None, session_name="work"
        )
        obs.submit(sub, _make_exec_config())
        await asyncio.sleep(0.05)
        await obs.shutdown()

        request = cli.execute.await_args.args[0]
        assert request.append_system_prompt is None
