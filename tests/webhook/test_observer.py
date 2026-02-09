"""Tests for WebhookObserver: lifecycle, dispatch routing, cron_task execution."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from ductor_bot.config import AgentConfig, ModelRegistry, WebhookConfig
from ductor_bot.webhook.manager import WebhookManager
from ductor_bot.webhook.models import WebhookEntry, WebhookResult, render_template
from ductor_bot.webhook.observer import _SAFETY_END, _SAFETY_START, WebhookObserver
from ductor_bot.workspace.paths import DuctorPaths


def _make_paths(tmp_path: Path) -> DuctorPaths:
    fw = tmp_path / "fw"
    paths = DuctorPaths(
        ductor_home=tmp_path / "home", home_defaults=fw / "workspace", framework_root=fw
    )
    paths.cron_tasks_dir.mkdir(parents=True)
    return paths


def _make_manager(paths: DuctorPaths) -> WebhookManager:
    return WebhookManager(hooks_path=paths.webhooks_path)


def _make_config(**overrides: Any) -> AgentConfig:
    defaults: dict[str, Any] = {
        "webhooks": WebhookConfig(enabled=True, token="test-token"),
    }
    defaults.update(overrides)
    return AgentConfig(**defaults)


def _make_models() -> ModelRegistry:
    return ModelRegistry()


def _make_hook(hook_id: str = "test-hook", **overrides: Any) -> WebhookEntry:
    defaults: dict[str, Any] = {
        "id": hook_id,
        "title": "Test Hook",
        "description": "Testing",
        "mode": "wake",
        "prompt_template": "{{msg}}",
    }
    defaults.update(overrides)
    return WebhookEntry(**defaults)


def _make_observer(
    paths: DuctorPaths,
    mgr: WebhookManager,
    *,
    models: ModelRegistry | None = None,
    **config_overrides: Any,
) -> WebhookObserver:
    return WebhookObserver(
        paths,
        mgr,
        config=_make_config(**config_overrides),
        models=models or _make_models(),
    )


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestWebhookObserverLifecycle:
    async def test_disabled_does_not_start(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        mgr = _make_manager(paths)
        config = AgentConfig(webhooks=WebhookConfig(enabled=False))
        observer = WebhookObserver(paths, mgr, config=config, models=_make_models())

        await observer.start()
        assert observer._server is None
        assert observer._watcher_task is None
        await observer.stop()

    async def test_stop_cleans_up(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        mgr = _make_manager(paths)
        observer = _make_observer(paths, mgr)

        # Mock the server start to avoid actual port binding
        with patch.object(observer, "_server") as mock_server:
            observer._running = True
            observer._watcher_task = asyncio.create_task(asyncio.sleep(999))
            mock_server.stop = AsyncMock()

            await observer.stop()

            assert observer._running is False
            assert observer._watcher_task is None


# ---------------------------------------------------------------------------
# Dispatch routing
# ---------------------------------------------------------------------------


class TestDispatchRouting:
    async def test_dispatch_unknown_hook_returns_not_found(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        mgr = _make_manager(paths)
        observer = _make_observer(paths, mgr)

        result = await observer._dispatch("nonexistent", {"msg": "hi"})
        assert result.status == "error:not_found"

    async def test_dispatch_wake_mode(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        mgr = _make_manager(paths)
        mgr.add_hook(_make_hook("wake-hook", mode="wake"))
        observer = _make_observer(paths, mgr, allowed_user_ids=[100])

        wake_handler = AsyncMock(return_value="Wake response text")
        observer.set_wake_handler(wake_handler)

        result = await observer._dispatch("wake-hook", {"msg": "hello"})
        assert result.status == "success"
        assert result.mode == "wake"
        assert "Wake response text" in result.result_text
        wake_handler.assert_awaited_once()

    async def test_dispatch_wake_no_handler(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        mgr = _make_manager(paths)
        mgr.add_hook(_make_hook("wake-hook", mode="wake"))
        observer = _make_observer(paths, mgr, allowed_user_ids=[100])

        result = await observer._dispatch("wake-hook", {"msg": "hi"})
        assert result.status == "error:no_wake_handler"

    async def test_dispatch_unknown_mode(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        mgr = _make_manager(paths)
        mgr.add_hook(_make_hook("bad-mode", mode="invalid"))
        observer = _make_observer(paths, mgr)

        result = await observer._dispatch("bad-mode", {})
        assert result.status == "error:unknown_mode_invalid"

    async def test_dispatch_records_trigger_success(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        mgr = _make_manager(paths)
        mgr.add_hook(_make_hook("wake-hook", mode="wake"))
        observer = _make_observer(paths, mgr, allowed_user_ids=[100])
        observer.set_wake_handler(AsyncMock(return_value="ok"))

        await observer._dispatch("wake-hook", {"msg": "hi"})

        hook = mgr.get_hook("wake-hook")
        assert hook is not None
        assert hook.trigger_count == 1
        assert hook.last_error is None

    async def test_dispatch_records_trigger_error(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        mgr = _make_manager(paths)
        mgr.add_hook(_make_hook("wake-hook", mode="wake"))
        observer = _make_observer(paths, mgr, allowed_user_ids=[100])
        observer.set_wake_handler(AsyncMock(return_value=None))

        await observer._dispatch("wake-hook", {"msg": "hi"})

        hook = mgr.get_hook("wake-hook")
        assert hook is not None
        assert hook.trigger_count == 1
        assert hook.last_error == "error:no_response"

    async def test_dispatch_calls_result_handler(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        mgr = _make_manager(paths)
        mgr.add_hook(_make_hook("wake-hook", mode="wake"))
        observer = _make_observer(paths, mgr, allowed_user_ids=[100])
        observer.set_wake_handler(AsyncMock(return_value="result text"))

        result_handler = AsyncMock()
        observer.set_result_handler(result_handler)

        await observer._dispatch("wake-hook", {"msg": "hi"})
        result_handler.assert_awaited_once()
        called_result = result_handler.call_args[0][0]
        assert isinstance(called_result, WebhookResult)
        assert called_result.status == "success"

    async def test_dispatch_wake_exception_per_user_is_caught(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        mgr = _make_manager(paths)
        mgr.add_hook(_make_hook("wake-hook", mode="wake"))
        observer = _make_observer(paths, mgr, allowed_user_ids=[100])
        observer.set_wake_handler(AsyncMock(side_effect=RuntimeError("boom")))

        # Per-user exceptions are caught inside _dispatch_wake, resulting in no_response
        result = await observer._dispatch("wake-hook", {"msg": "hi"})
        assert result.status == "error:no_response"

        hook = mgr.get_hook("wake-hook")
        assert hook is not None
        assert hook.last_error == "error:no_response"


# ---------------------------------------------------------------------------
# Wake dispatch
# ---------------------------------------------------------------------------


class TestDispatchWake:
    async def test_wake_calls_handler_for_each_user(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        mgr = _make_manager(paths)
        mgr.add_hook(_make_hook("wake-hook", mode="wake"))
        observer = _make_observer(paths, mgr, allowed_user_ids=[100, 200])

        handler = AsyncMock(return_value="response")
        observer.set_wake_handler(handler)

        result = await observer._dispatch("wake-hook", {"msg": "hi"})
        assert handler.call_count == 2
        assert result.status == "success"

    async def test_wake_no_response_is_error(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        mgr = _make_manager(paths)
        mgr.add_hook(_make_hook("wake-hook", mode="wake"))
        observer = _make_observer(paths, mgr, allowed_user_ids=[100])

        observer.set_wake_handler(AsyncMock(return_value=None))
        result = await observer._dispatch("wake-hook", {"msg": "hi"})
        assert result.status == "error:no_response"

    async def test_wake_prompt_includes_safety_boundary(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        mgr = _make_manager(paths)
        mgr.add_hook(_make_hook("wake-hook", mode="wake", prompt_template="Hello {{name}}"))
        observer = _make_observer(paths, mgr, allowed_user_ids=[100])

        handler = AsyncMock(return_value="ok")
        observer.set_wake_handler(handler)

        await observer._dispatch("wake-hook", {"name": "Alice"})
        prompt = handler.call_args[0][1]
        assert _SAFETY_START in prompt
        assert _SAFETY_END in prompt
        assert "Hello Alice" in prompt


# ---------------------------------------------------------------------------
# Cron task dispatch
# ---------------------------------------------------------------------------


class TestDispatchCronTask:
    async def test_cron_task_no_task_folder(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        mgr = _make_manager(paths)
        mgr.add_hook(_make_hook("ct-hook", mode="cron_task", task_folder=None))
        observer = _make_observer(paths, mgr)

        result = await observer._dispatch("ct-hook", {})
        assert result.status == "error:no_task_folder"

    async def test_cron_task_missing_folder(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        mgr = _make_manager(paths)
        mgr.add_hook(_make_hook("ct-hook", mode="cron_task", task_folder="missing"))
        observer = _make_observer(paths, mgr)

        result = await observer._dispatch("ct-hook", {})
        assert result.status == "error:folder_missing"

    async def test_cron_task_missing_cli(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        mgr = _make_manager(paths)
        mgr.add_hook(_make_hook("ct-hook", mode="cron_task", task_folder="my-task"))
        (paths.cron_tasks_dir / "my-task").mkdir()
        observer = _make_observer(paths, mgr)

        with patch("ductor_bot.cron.execution.which", return_value=None):
            result = await observer._dispatch("ct-hook", {"msg": "go"})

        assert result.status.startswith("error:cli_not_found")

    async def test_cron_task_success(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        mgr = _make_manager(paths)
        mgr.add_hook(_make_hook("ct-hook", mode="cron_task", task_folder="my-task"))
        (paths.cron_tasks_dir / "my-task").mkdir()
        observer = _make_observer(paths, mgr)

        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b'{"result": "Done."}', b""))

        with (
            patch("ductor_bot.cron.execution.which", return_value="/usr/bin/claude"),
            patch("asyncio.create_subprocess_exec", return_value=mock_proc) as exec_mock,
        ):
            result = await observer._dispatch("ct-hook", {"msg": "go"})

        assert result.status == "success"
        assert result.result_text == "Done."
        exec_mock.assert_called_once()

    async def test_cron_task_failure_exit_code(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        mgr = _make_manager(paths)
        mgr.add_hook(_make_hook("ct-hook", mode="cron_task", task_folder="my-task"))
        (paths.cron_tasks_dir / "my-task").mkdir()
        observer = _make_observer(paths, mgr)

        mock_proc = AsyncMock()
        mock_proc.returncode = 1
        mock_proc.communicate = AsyncMock(return_value=(b"", b"error"))

        with (
            patch("ductor_bot.cron.execution.which", return_value="/usr/bin/claude"),
            patch("asyncio.create_subprocess_exec", return_value=mock_proc),
        ):
            result = await observer._dispatch("ct-hook", {"msg": "go"})

        assert result.status == "error:exit_1"

    async def test_cron_task_timeout(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        mgr = _make_manager(paths)
        mgr.add_hook(_make_hook("ct-hook", mode="cron_task", task_folder="my-task"))
        (paths.cron_tasks_dir / "my-task").mkdir()
        observer = _make_observer(paths, mgr, cli_timeout=0.1)

        mock_proc = AsyncMock()
        mock_proc.returncode = -9
        mock_proc.kill = MagicMock()

        call_count = 0

        async def _communicate_side_effect() -> tuple[bytes, bytes]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                await asyncio.sleep(999)
            return b"", b""

        mock_proc.communicate = AsyncMock(side_effect=_communicate_side_effect)

        with (
            patch("ductor_bot.cron.execution.which", return_value="/usr/bin/claude"),
            patch("asyncio.create_subprocess_exec", return_value=mock_proc),
        ):
            result = await observer._dispatch("ct-hook", {"msg": "go"})

        assert result.status == "error:timeout"
        mock_proc.kill.assert_called_once()

    async def test_cron_task_uses_stdin_devnull(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        mgr = _make_manager(paths)
        mgr.add_hook(_make_hook("ct-hook", mode="cron_task", task_folder="my-task"))
        (paths.cron_tasks_dir / "my-task").mkdir()
        observer = _make_observer(paths, mgr)

        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b'{"result": "ok"}', b""))

        with (
            patch("ductor_bot.cron.execution.which", return_value="/usr/bin/claude"),
            patch("asyncio.create_subprocess_exec", return_value=mock_proc) as exec_mock,
        ):
            await observer._dispatch("ct-hook", {"msg": "go"})

        call_kwargs = exec_mock.call_args[1]
        assert call_kwargs["stdin"] == asyncio.subprocess.DEVNULL


# ---------------------------------------------------------------------------
# Safety boundary
# ---------------------------------------------------------------------------


class TestSafetyBoundary:
    def test_safety_markers_in_prompt(self) -> None:
        rendered = render_template("Hello {{name}}", {"name": "World"})
        safe = f"{_SAFETY_START}\n{rendered}\n{_SAFETY_END}"
        assert _SAFETY_START in safe
        assert _SAFETY_END in safe
        assert "Hello World" in safe
