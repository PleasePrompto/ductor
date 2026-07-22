"""Tests for CronObserver: in-process scheduling, file watching, execution."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import time_machine

from ductor_bot.cli.codex_cache import CodexModelCache
from ductor_bot.cli.codex_discovery import CodexModelInfo
from ductor_bot.config import AgentConfig
from ductor_bot.cron.execution import (
    enrich_instruction,
    parse_claude_result,
    parse_codex_result,
)
from ductor_bot.cron.manager import CronJob, CronManager
from ductor_bot.cron.observer import CronObserver
from ductor_bot.workspace.paths import DuctorPaths


def _make_paths(tmp_path: Path) -> DuctorPaths:
    fw = tmp_path / "fw"
    paths = DuctorPaths(
        ductor_home=tmp_path / "home", home_defaults=fw / "workspace", framework_root=fw
    )
    paths.cron_tasks_dir.mkdir(parents=True)
    return paths


def _make_manager(paths: DuctorPaths) -> CronManager:
    return CronManager(jobs_path=paths.cron_jobs_path)


def _make_config(**overrides: Any) -> AgentConfig:
    return AgentConfig(**overrides)


def _make_codex_cache(model_id: str = "gpt-5.2-codex") -> CodexModelCache:
    """Return a mock CodexModelCache resolving *model_id*."""
    cache = MagicMock(spec=CodexModelCache)
    cache.validate_model.return_value = True
    cache.get_model.return_value = CodexModelInfo(
        id=model_id,
        display_name="GPT-5.2 Codex",
        description="Codex model",
        supported_efforts=("low", "medium", "high"),
        default_effort="medium",
        is_default=True,
    )
    return cache


def _make_observer(
    paths: DuctorPaths,
    mgr: CronManager,
    *,
    codex_cache: CodexModelCache | None = None,
    **config_overrides: Any,
) -> CronObserver:
    return CronObserver(
        paths,
        mgr,
        config=_make_config(**config_overrides),
        codex_cache=codex_cache or _make_codex_cache(),
    )


def _make_job(job_id: str = "daily", **overrides: Any) -> CronJob:
    defaults: dict[str, Any] = {
        "id": job_id,
        "title": "Daily Report",
        "description": "Generate report",
        "schedule": "0 9 * * *",
        "task_folder": job_id,
        "agent_instruction": "Do the daily work",
    }
    defaults.update(overrides)
    return CronJob(**defaults)


def _write_jobs(paths: DuctorPaths, jobs: list[CronJob]) -> None:
    """Write jobs directly to JSON file."""
    data = {"jobs": [j.to_dict() for j in jobs]}
    paths.cron_jobs_path.parent.mkdir(parents=True, exist_ok=True)
    paths.cron_jobs_path.write_text(json.dumps(data), encoding="utf-8")


class TestCronObserverScheduling:
    """Scheduling and lifecycle tests."""

    async def test_observer_imports(self) -> None:
        from ductor_bot.cron.observer import CronObserver

        assert CronObserver is not None

    async def test_observer_loads_jobs_on_start(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        mgr = _make_manager(paths)
        mgr.add_job(_make_job("daily"))
        (paths.cron_tasks_dir / "daily").mkdir()

        observer = _make_observer(paths, mgr)
        await observer.start()

        assert len(observer._scheduled) == 1
        assert "daily" in observer._scheduled
        await observer.stop()

    async def test_observer_schedules_enabled_jobs_only(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        mgr = _make_manager(paths)
        mgr.add_job(_make_job("enabled"))
        mgr.add_job(_make_job("disabled", enabled=False))

        observer = _make_observer(paths, mgr)
        await observer.start()

        assert "enabled" in observer._scheduled
        assert "disabled" not in observer._scheduled
        await observer.stop()

    async def test_observer_stop_cancels_tasks(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        mgr = _make_manager(paths)
        mgr.add_job(_make_job("daily"))

        observer = _make_observer(paths, mgr)
        await observer.start()
        assert len(observer._scheduled) > 0

        await observer.stop()
        assert len(observer._scheduled) == 0

    async def test_observer_empty_json(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        mgr = _make_manager(paths)

        observer = _make_observer(paths, mgr)
        await observer.start()
        assert len(observer._scheduled) == 0
        await observer.stop()

    async def test_observer_invalid_cron_expression(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        mgr = _make_manager(paths)
        mgr.add_job(_make_job("bad-cron", schedule="not a cron expression"))

        observer = _make_observer(paths, mgr)
        await observer.start()

        assert "bad-cron" not in observer._scheduled
        await observer.stop()

    async def test_observer_reschedules_on_file_change(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        mgr = _make_manager(paths)
        mgr.add_job(_make_job("original"))

        observer = _make_observer(paths, mgr)
        await observer.start()
        assert len(observer._scheduled) == 1

        _write_jobs(paths, [_make_job("original"), _make_job("added")])
        mgr.reload()
        await observer._reschedule_all()

        assert len(observer._scheduled) == 2
        assert "added" in observer._scheduled
        await observer.stop()

    async def test_reschedule_now_runs_immediately(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        mgr = _make_manager(paths)
        observer = _make_observer(paths, mgr)
        observer._running = True

        with (
            patch.object(observer, "_update_mtime", new_callable=AsyncMock) as mtime_mock,
            patch.object(observer, "_reschedule_all", new_callable=AsyncMock) as reschedule_mock,
        ):
            await observer.reschedule_now()

        mtime_mock.assert_awaited_once()
        reschedule_mock.assert_awaited_once()

    async def test_request_reschedule_coalesces_pending_task(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        mgr = _make_manager(paths)
        observer = _make_observer(paths, mgr)
        observer._running = True

        pending_task = MagicMock()
        pending_task.done.return_value = False

        def _create_task(coro: object) -> MagicMock:
            close = getattr(coro, "close", None)
            if callable(close):
                close()
            return pending_task

        with patch(
            "ductor_bot.cron.observer.asyncio.create_task", side_effect=_create_task
        ) as create:
            observer.request_reschedule()
            observer.request_reschedule()

        create.assert_called_once()

    async def test_executing_job_survives_reschedule(self, tmp_path: Path) -> None:
        """A job whose _execute_job is running must not be cancelled by _reschedule_all."""
        paths = _make_paths(tmp_path)
        mgr = _make_manager(paths)
        mgr.add_job(_make_job("running"))
        mgr.add_job(_make_job("idle"))

        observer = _make_observer(paths, mgr)
        await observer.start()
        assert "running" in observer._scheduled
        assert "idle" in observer._scheduled

        running_task = observer._scheduled["running"]
        idle_task = observer._scheduled["idle"]

        observer._executing.add("running")
        try:
            await observer._reschedule_all()
        finally:
            observer._executing.discard("running")

        assert not running_task.cancelled()
        assert idle_task.cancelled()
        await observer.stop()

    async def test_stop_cancels_executing_jobs(self, tmp_path: Path) -> None:
        """stop() must cancel ALL tasks including currently executing ones."""
        paths = _make_paths(tmp_path)
        mgr = _make_manager(paths)
        mgr.add_job(_make_job("running"))

        observer = _make_observer(paths, mgr)
        await observer.start()
        assert "running" in observer._scheduled

        task = observer._scheduled["running"]
        observer._executing.add("running")

        await observer.stop()

        assert len(observer._scheduled) == 0
        assert task.done()


class TestCronObserverExecution:
    """Job execution tests."""

    async def test_preflight_skip_avoids_agent_execution(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        mgr = _make_manager(paths)
        mgr.add_job(_make_job("daily"))
        scripts = paths.cron_tasks_dir / "daily" / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "preflight.py").write_text("print('HEARTBEAT_OK')\n")
        observer = _make_observer(paths, mgr, cron_preflight={"enabled": True})

        preflight_proc = AsyncMock()
        preflight_proc.returncode = 0
        preflight_proc.communicate = AsyncMock(return_value=(b"checked\nHEARTBEAT_OK\n", b""))
        with (
            patch("asyncio.create_subprocess_exec", return_value=preflight_proc),
            patch("ductor_bot.cron.observer.execute_in_task_folder") as execute,
        ):
            await observer._execute_job("daily", "Do work", "daily")

        execute.assert_not_awaited()
        job = mgr.get_job("daily")
        assert job is not None
        assert job.last_run_status == "success:preflight"
        assert job.last_delivery_status == "skipped"

    async def test_preflight_is_opt_in(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        mgr = _make_manager(paths)
        scripts = paths.cron_tasks_dir / "daily" / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "preflight.py").write_text("print('HEARTBEAT_OK')\n")
        observer = _make_observer(paths, mgr)

        with patch("asyncio.create_subprocess_exec") as spawn:
            assert await observer._run_preflight("Daily Report", "daily") is False

        spawn.assert_not_called()

    async def test_preflight_non_skip_result_fails_open(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        mgr = _make_manager(paths)
        scripts = paths.cron_tasks_dir / "daily" / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "preflight.py").write_text("raise SystemExit(2)\n")
        observer = _make_observer(paths, mgr, cron_preflight={"enabled": True})
        preflight_proc = AsyncMock()
        preflight_proc.returncode = 2
        preflight_proc.communicate = AsyncMock(return_value=(b"CONTINUE\n", b""))

        with patch("asyncio.create_subprocess_exec", return_value=preflight_proc):
            assert await observer._run_preflight("Daily Report", "daily") is False

    async def test_preflight_end_to_end_real_script_skip(self, tmp_path: Path) -> None:
        """Unmocked run: a real script printing the marker skips the agent."""
        paths = _make_paths(tmp_path)
        mgr = _make_manager(paths)
        scripts = paths.cron_tasks_dir / "daily" / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "preflight.py").write_text("print('checked')\nprint('HEARTBEAT_OK')\n")
        observer = _make_observer(paths, mgr, cron_preflight={"enabled": True})

        assert await observer._run_preflight("Daily Report", "daily") is True

    async def test_preflight_end_to_end_real_script_fails_open(self, tmp_path: Path) -> None:
        """Unmocked run: a real script exiting non-zero lets the agent run."""
        paths = _make_paths(tmp_path)
        mgr = _make_manager(paths)
        scripts = paths.cron_tasks_dir / "daily" / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "preflight.py").write_text("raise SystemExit(2)\n")
        observer = _make_observer(paths, mgr, cron_preflight={"enabled": True})

        assert await observer._run_preflight("Daily Report", "daily") is False

    async def test_preflight_spawn_failure_fails_open(self, tmp_path: Path) -> None:
        """A preflight that cannot even spawn must not suppress the agent run."""
        paths = _make_paths(tmp_path)
        mgr = _make_manager(paths)
        scripts = paths.cron_tasks_dir / "daily" / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "preflight.py").write_text("print('HEARTBEAT_OK')\n")
        observer = _make_observer(paths, mgr, cron_preflight={"enabled": True})

        with patch(
            "asyncio.create_subprocess_exec",
            side_effect=OSError(24, "Too many open files"),
        ):
            assert await observer._run_preflight("Daily Report", "daily") is False

    def test_kill_process_group_kills_group_for_real_pid(self) -> None:
        from ductor_bot.cron.observer import _kill_process_group

        proc = MagicMock()
        proc.pid = 1234
        with (
            patch("ductor_bot.cron.observer.os.getpgid", return_value=1234) as getpgid,
            patch("ductor_bot.cron.observer.os.killpg") as killpg,
        ):
            _kill_process_group(proc)

        getpgid.assert_called_once_with(1234)
        killpg.assert_called_once()
        proc.kill.assert_not_called()

    def test_kill_process_group_never_signals_pgid_one(self) -> None:
        """pgid <= 1 would become kill(-1) and take down the whole user session."""
        from ductor_bot.cron.observer import _kill_process_group

        proc = MagicMock()
        proc.pid = 1234
        with (
            patch("ductor_bot.cron.observer.os.getpgid", return_value=1),
            patch("ductor_bot.cron.observer.os.killpg") as killpg,
        ):
            _kill_process_group(proc)

        killpg.assert_not_called()
        proc.kill.assert_called_once()

    def test_kill_process_group_rejects_non_int_pid(self) -> None:
        """A mocked/corrupted pid must never reach getpgid/killpg."""
        from ductor_bot.cron.observer import _kill_process_group

        proc = MagicMock()  # proc.pid is a MagicMock, not an int
        with (
            patch("ductor_bot.cron.observer.os.getpgid") as getpgid,
            patch("ductor_bot.cron.observer.os.killpg") as killpg,
        ):
            _kill_process_group(proc)

        getpgid.assert_not_called()
        killpg.assert_not_called()
        proc.kill.assert_called_once()

    def test_kill_process_group_falls_back_when_group_gone(self) -> None:
        from ductor_bot.cron.observer import _kill_process_group

        proc = MagicMock()
        proc.pid = 1234
        with (
            patch("ductor_bot.cron.observer.os.getpgid", side_effect=ProcessLookupError),
            patch("ductor_bot.cron.observer.os.killpg") as killpg,
        ):
            _kill_process_group(proc)

        killpg.assert_not_called()
        proc.kill.assert_called_once()

    async def test_preflight_timeout_kills_process_and_fails_open(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        mgr = _make_manager(paths)
        scripts = paths.cron_tasks_dir / "daily" / "scripts"
        scripts.mkdir(parents=True)
        (scripts / "preflight.py").write_text("pass\n")
        observer = _make_observer(
            paths,
            mgr,
            cron_preflight={"enabled": True, "timeout_seconds": 0.01},
        )
        preflight_proc = AsyncMock()
        preflight_proc.returncode = -9
        calls = 0

        async def communicate() -> tuple[bytes, bytes]:
            nonlocal calls
            calls += 1
            if calls == 1:
                await asyncio.sleep(999)
            return b"", b""

        preflight_proc.communicate = AsyncMock(side_effect=communicate)
        preflight_proc.kill = MagicMock()
        with patch("asyncio.create_subprocess_exec", return_value=preflight_proc):
            assert await observer._run_preflight("Daily Report", "daily") is False

        preflight_proc.kill.assert_called_once()

    async def test_handles_missing_task_folder(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        mgr = _make_manager(paths)
        mgr.add_job(_make_job("missing-folder"))

        observer = _make_observer(paths, mgr)

        # Freeze time to active hours (14:00 UTC) to avoid quiet hour skip
        with time_machine.travel(datetime(2026, 1, 15, 14, 0, tzinfo=UTC)):
            await observer._execute_job("missing-folder", "do stuff", "missing-folder")

        job = mgr.get_job("missing-folder")
        assert job is not None
        assert job.last_run_status == "error:folder_missing"

    async def test_handles_missing_cli(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        mgr = _make_manager(paths)
        mgr.add_job(_make_job("no-cli"))
        (paths.cron_tasks_dir / "no-cli").mkdir()

        observer = _make_observer(paths, mgr)

        # Freeze time to active hours (14:00 UTC) to avoid quiet hour skip
        with (
            time_machine.travel(datetime(2026, 1, 15, 14, 0, tzinfo=UTC)),
            patch("ductor_bot.cron.execution.which", return_value=None),
        ):
            await observer._execute_job("no-cli", "do stuff", "no-cli")

        job = mgr.get_job("no-cli")
        assert job is not None
        assert job.last_run_status is not None
        assert job.last_run_status.startswith("error:cli_not_found")

    async def test_executes_claude_job_success(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        mgr = _make_manager(paths)
        mgr.add_job(_make_job("daily"))
        task_folder = paths.cron_tasks_dir / "daily"
        task_folder.mkdir()

        observer = _make_observer(paths, mgr)

        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b'{"result": "Done."}', b""))

        # Freeze time to active hours (14:00 UTC) to avoid quiet hour skip
        with (
            time_machine.travel(datetime(2026, 1, 15, 14, 0, tzinfo=UTC)),
            patch("ductor_bot.cron.execution.which", return_value="/usr/bin/claude"),
            patch("asyncio.create_subprocess_exec", return_value=mock_proc) as exec_mock,
        ):
            await observer._execute_job("daily", "Generate report", "daily")

        exec_mock.assert_called_once()
        call_args = exec_mock.call_args
        assert "/usr/bin/claude" in call_args[0]
        assert str(task_folder) == call_args[1]["cwd"]

        job = mgr.get_job("daily")
        assert job is not None
        assert job.last_run_status == "success"

    async def test_executes_codex_job_success(self, tmp_path: Path) -> None:
        """Codex model uses codex CLI with exec subcommand."""
        paths = _make_paths(tmp_path)
        mgr = _make_manager(paths)
        mgr.add_job(_make_job("daily"))
        task_folder = paths.cron_tasks_dir / "daily"
        task_folder.mkdir()

        # Mock cache for Codex model
        codex_cache = _make_codex_cache(model_id="gpt-5.2")

        observer = _make_observer(
            paths,
            mgr,
            codex_cache=codex_cache,
            model="gpt-5.2",
            provider="codex",  # Set provider to match model
        )

        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(
            return_value=(
                b'{"type":"item.completed","item":{"type":"agent_message","text":"Done."}}',
                b"",
            )
        )

        # Freeze time to active hours (14:00 UTC) to avoid quiet hour skip
        with (
            time_machine.travel(datetime(2026, 1, 15, 14, 0, tzinfo=UTC)),
            patch("ductor_bot.cron.execution.which", return_value="/usr/bin/codex"),
            patch("asyncio.create_subprocess_exec", return_value=mock_proc) as exec_mock,
        ):
            await observer._execute_job("daily", "Generate report", "daily")

        cmd = exec_mock.call_args[0]
        assert "/usr/bin/codex" in cmd
        assert "exec" in cmd
        assert "--json" in cmd

        job = mgr.get_job("daily")
        assert job is not None
        assert job.last_run_status == "success"

    async def test_updates_run_status_on_failure(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        mgr = _make_manager(paths)
        mgr.add_job(_make_job("failing"))
        (paths.cron_tasks_dir / "failing").mkdir()

        observer = _make_observer(paths, mgr)

        mock_proc = AsyncMock()
        mock_proc.returncode = 1
        mock_proc.communicate = AsyncMock(return_value=(b"", b"error output"))

        with (
            time_machine.travel(datetime(2026, 1, 15, 14, 0, tzinfo=UTC)),
            patch("ductor_bot.cron.execution.which", return_value="/usr/bin/claude"),
            patch("asyncio.create_subprocess_exec", return_value=mock_proc),
        ):
            await observer._execute_job("failing", "Do stuff", "failing")

        job = mgr.get_job("failing")
        assert job is not None
        assert job.last_run_status == "error:exit_1"

    async def test_uses_config_model(self, tmp_path: Path) -> None:
        """CLI command includes --model from AgentConfig."""
        paths = _make_paths(tmp_path)
        mgr = _make_manager(paths)
        mgr.add_job(_make_job("daily"))
        (paths.cron_tasks_dir / "daily").mkdir()

        observer = _make_observer(paths, mgr, model="sonnet")

        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b'{"result": "ok"}', b""))

        with (
            time_machine.travel(datetime(2026, 1, 15, 14, 0, tzinfo=UTC)),
            patch("ductor_bot.cron.execution.which", return_value="/usr/bin/claude"),
            patch("asyncio.create_subprocess_exec", return_value=mock_proc) as exec_mock,
        ):
            await observer._execute_job("daily", "Do work", "daily")

        cmd = exec_mock.call_args[0]
        assert "--model" in cmd
        model_idx = cmd.index("--model")
        assert cmd[model_idx + 1] == "sonnet"

    async def test_uses_config_permission_mode(self, tmp_path: Path) -> None:
        """Claude CLI command includes --permission-mode from AgentConfig."""
        paths = _make_paths(tmp_path)
        mgr = _make_manager(paths)
        mgr.add_job(_make_job("daily"))
        (paths.cron_tasks_dir / "daily").mkdir()

        observer = _make_observer(paths, mgr, permission_mode="plan")

        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))

        with (
            time_machine.travel(datetime(2026, 1, 15, 14, 0, tzinfo=UTC)),
            patch("ductor_bot.cron.execution.which", return_value="/usr/bin/claude"),
            patch("asyncio.create_subprocess_exec", return_value=mock_proc) as exec_mock,
        ):
            await observer._execute_job("daily", "Do work", "daily")

        cmd = exec_mock.call_args[0]
        assert "--permission-mode" in cmd
        idx = cmd.index("--permission-mode")
        assert cmd[idx + 1] == "plan"

    async def test_no_session_persistence_flag(self, tmp_path: Path) -> None:
        """Claude CLI command includes --no-session-persistence."""
        paths = _make_paths(tmp_path)
        mgr = _make_manager(paths)
        mgr.add_job(_make_job("daily"))
        (paths.cron_tasks_dir / "daily").mkdir()

        observer = _make_observer(paths, mgr)

        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))

        with (
            time_machine.travel(datetime(2026, 1, 15, 14, 0, tzinfo=UTC)),
            patch("ductor_bot.cron.execution.which", return_value="/usr/bin/claude"),
            patch("asyncio.create_subprocess_exec", return_value=mock_proc) as exec_mock,
        ):
            await observer._execute_job("daily", "Do work", "daily")

        cmd = exec_mock.call_args[0]
        assert "--no-session-persistence" in cmd

    async def test_enriches_instruction(self, tmp_path: Path) -> None:
        """Instruction passed to CLI contains memory file references."""
        paths = _make_paths(tmp_path)
        mgr = _make_manager(paths)
        mgr.add_job(_make_job("daily"))
        (paths.cron_tasks_dir / "daily").mkdir()

        observer = _make_observer(paths, mgr)

        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b"", b""))

        with (
            time_machine.travel(datetime(2026, 1, 15, 14, 0, tzinfo=UTC)),
            patch("ductor_bot.cron.execution.which", return_value="/usr/bin/claude"),
            patch("asyncio.create_subprocess_exec", return_value=mock_proc) as exec_mock,
        ):
            await observer._execute_job("daily", "Do the work", "daily")

        cmd = exec_mock.call_args[0]
        # Prompt is the last argument (after "--" separator)
        instruction = cmd[-1]
        assert "Do the work" in instruction
        assert "daily_MEMORY.md" in instruction

    async def test_calls_on_result_callback(self, tmp_path: Path) -> None:
        """on_result callback receives (title, result_text, status)."""
        paths = _make_paths(tmp_path)
        mgr = _make_manager(paths)
        mgr.add_job(_make_job("daily", title="My Daily Task"))
        (paths.cron_tasks_dir / "daily").mkdir()

        observer = _make_observer(paths, mgr)
        callback = AsyncMock(return_value=(True, ""))
        observer.set_result_handler(callback)

        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b'{"result": "All done."}', b""))

        with (
            time_machine.travel(datetime(2026, 1, 15, 14, 0, tzinfo=UTC)),
            patch("ductor_bot.cron.execution.which", return_value="/usr/bin/claude"),
            patch("asyncio.create_subprocess_exec", return_value=mock_proc),
        ):
            await observer._execute_job("daily", "Do work", "daily")

        callback.assert_awaited_once_with("My Daily Task", "All done.", "success", 0, None, "tg")
        job = mgr.get_job("daily")
        assert job is not None
        assert job.last_run_status == "success"
        assert job.last_delivery_status == "ok"
        assert job.last_result_text is None

    async def test_delivery_failure_recorded_and_result_preserved(self, tmp_path: Path) -> None:
        """#160: execution success + delivery failure must be visible and resendable."""
        paths = _make_paths(tmp_path)
        mgr = _make_manager(paths)
        mgr.add_job(_make_job("daily", title="My Daily Task"))
        (paths.cron_tasks_dir / "daily").mkdir()

        observer = _make_observer(paths, mgr)
        callback = AsyncMock(return_value=(False, "TelegramNetworkError"))
        observer.set_result_handler(callback)

        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b'{"result": "All done."}', b""))

        with (
            time_machine.travel(datetime(2026, 1, 15, 14, 0, tzinfo=UTC)),
            patch("ductor_bot.cron.execution.which", return_value="/usr/bin/claude"),
            patch("asyncio.create_subprocess_exec", return_value=mock_proc),
        ):
            await observer._execute_job("daily", "Do work", "daily")

        job = mgr.get_job("daily")
        assert job is not None
        assert job.last_run_status == "success"
        assert job.last_delivery_status == "failed"
        assert job.last_delivery_error == "TelegramNetworkError"
        assert job.last_result_text == "All done."

    async def test_retries_preserved_result_without_rerunning_agent(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        mgr = _make_manager(paths)
        job = _make_job(
            "daily",
            title="My Daily Task",
            last_run_status="success",
            last_delivery_status="failed",
            last_delivery_error="TelegramNetworkError",
            last_result_text="Preserved result",
            chat_id=42,
            topic_id=7,
        )
        mgr.add_job(job)
        observer = _make_observer(
            paths,
            mgr,
            cron_delivery_retry={"enabled": True, "interval_seconds": 60},
        )
        callback = AsyncMock(return_value=(True, ""))
        observer.set_result_handler(callback)

        with patch("ductor_bot.cron.observer.execute_in_task_folder") as execute:
            await observer._retry_failed_deliveries()

        execute.assert_not_called()
        callback.assert_awaited_once_with(
            "My Daily Task", "Preserved result", "success", 42, 7, "tg"
        )
        assert job.last_delivery_status == "ok"
        assert job.last_result_text is None
        assert job.delivery_retry_attempts == 0

    async def test_failed_retry_obeys_cooldown_and_attempt_limit(self, tmp_path: Path) -> None:
        paths = _make_paths(tmp_path)
        mgr = _make_manager(paths)
        job = _make_job(
            "daily",
            last_run_status="success",
            last_delivery_status="failed",
            last_result_text="Preserved result",
        )
        mgr.add_job(job)
        observer = _make_observer(
            paths,
            mgr,
            cron_delivery_retry={
                "enabled": True,
                "interval_seconds": 60,
                "max_attempts": 1,
            },
        )
        callback = AsyncMock(return_value=(False, "still offline"))
        observer.set_result_handler(callback)

        await observer._retry_failed_deliveries()
        await observer._retry_failed_deliveries()

        callback.assert_awaited_once()
        assert job.last_delivery_status == "failed"
        assert job.last_result_text == "Preserved result"
        assert job.delivery_retry_attempts == 1
        assert job.next_delivery_retry_at is not None

    async def test_failed_retry_cooldown_skips_before_next_attempt_time(
        self, tmp_path: Path
    ) -> None:
        """With attempt budget left, the cooldown alone must gate the next retry."""
        paths = _make_paths(tmp_path)
        mgr = _make_manager(paths)
        job = _make_job(
            "daily",
            last_run_status="success",
            last_delivery_status="failed",
            last_result_text="Preserved result",
        )
        mgr.add_job(job)
        observer = _make_observer(
            paths,
            mgr,
            cron_delivery_retry={
                "enabled": True,
                "interval_seconds": 300,
                "max_attempts": 5,
            },
        )
        callback = AsyncMock(return_value=(False, "still offline"))
        observer.set_result_handler(callback)

        await observer._retry_failed_deliveries()
        await observer._retry_failed_deliveries()

        callback.assert_awaited_once()
        assert job.delivery_retry_attempts == 1
        assert job.next_delivery_retry_at is not None

    async def test_retry_sweep_skips_currently_executing_job(self, tmp_path: Path) -> None:
        """A job whose regular execution is in flight is never retried concurrently."""
        paths = _make_paths(tmp_path)
        mgr = _make_manager(paths)
        job = _make_job(
            "daily",
            last_run_status="success",
            last_delivery_status="failed",
            last_result_text="Preserved result",
        )
        mgr.add_job(job)
        observer = _make_observer(
            paths,
            mgr,
            cron_delivery_retry={
                "enabled": True,
                "interval_seconds": 60,
                "max_attempts": 3,
            },
        )
        callback = AsyncMock(return_value=(True, ""))
        observer.set_result_handler(callback)
        observer._executing.add(job.id)

        await observer._retry_failed_deliveries()

        callback.assert_not_awaited()
        assert job.last_result_text == "Preserved result"

    async def test_retry_success_keeps_newer_result_preserved(self, tmp_path: Path) -> None:
        """A newer failed result that lands mid-retry is not cleared by the old ack."""
        paths = _make_paths(tmp_path)
        mgr = _make_manager(paths)
        job = _make_job(
            "daily",
            last_run_status="success",
            last_delivery_status="failed",
            last_result_text="Old result",
        )
        mgr.add_job(job)
        observer = _make_observer(
            paths,
            mgr,
            cron_delivery_retry={
                "enabled": True,
                "interval_seconds": 60,
                "max_attempts": 3,
            },
        )

        async def _deliver_and_race(*_args: object, **_kwargs: object) -> tuple[bool, str]:
            job.last_result_text = "New result"
            job.delivery_retry_attempts = 0
            return (True, "")

        callback = AsyncMock(side_effect=_deliver_and_race)
        observer.set_result_handler(callback)

        await observer._retry_failed_deliveries()

        assert job.last_result_text == "New result"
        assert job.last_delivery_status == "failed"

    async def test_execute_job_timeout_kills_process(self, tmp_path: Path) -> None:
        """Subprocess that exceeds cli_timeout is killed and reported as timeout."""
        paths = _make_paths(tmp_path)
        mgr = _make_manager(paths)
        mgr.add_job(_make_job("slow"))
        (paths.cron_tasks_dir / "slow").mkdir()

        observer = _make_observer(paths, mgr, cli_timeout=0.1)

        mock_proc = AsyncMock()
        mock_proc.returncode = -9

        async def _hang() -> tuple[bytes, bytes]:
            await asyncio.sleep(999)
            return b"", b""

        call_count = 0

        async def _communicate_side_effect(
            *,
            input: bytes | None = None,  # noqa: A002, ARG001
        ) -> tuple[bytes, bytes]:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                await asyncio.sleep(999)
            return b"", b""

        mock_proc.communicate = AsyncMock(side_effect=_communicate_side_effect)
        mock_proc.kill = MagicMock()

        with (
            time_machine.travel(datetime(2026, 1, 15, 14, 0, tzinfo=UTC)),
            patch("ductor_bot.cron.execution.which", return_value="/usr/bin/claude"),
            patch("asyncio.create_subprocess_exec", return_value=mock_proc),
        ):
            await observer._execute_job("slow", "Take forever", "slow")

        assert mock_proc.communicate.await_count >= 2
        job = mgr.get_job("slow")
        assert job is not None
        assert job.last_run_status == "error:timeout"

    async def test_execute_job_uses_stdin_devnull(self, tmp_path: Path) -> None:
        """Subprocess is spawned with stdin=DEVNULL to prevent interactive hangs."""
        paths = _make_paths(tmp_path)
        mgr = _make_manager(paths)
        mgr.add_job(_make_job("daily"))
        (paths.cron_tasks_dir / "daily").mkdir()

        observer = _make_observer(paths, mgr)

        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b'{"result": "ok"}', b""))

        with (
            time_machine.travel(datetime(2026, 1, 15, 14, 0, tzinfo=UTC)),
            patch("ductor_bot.cron.execution.which", return_value="/usr/bin/claude"),
            patch("asyncio.create_subprocess_exec", return_value=mock_proc) as exec_mock,
        ):
            await observer._execute_job("daily", "Do work", "daily")

        call_kwargs = exec_mock.call_args[1]
        assert call_kwargs["stdin"] == asyncio.subprocess.DEVNULL

    async def test_on_result_not_called_without_handler(self, tmp_path: Path) -> None:
        """No error when on_result is not set."""
        paths = _make_paths(tmp_path)
        mgr = _make_manager(paths)
        mgr.add_job(_make_job("daily"))
        (paths.cron_tasks_dir / "daily").mkdir()

        observer = _make_observer(paths, mgr)

        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b'{"result": "ok"}', b""))

        with (
            time_machine.travel(datetime(2026, 1, 15, 14, 0, tzinfo=UTC)),
            patch("ductor_bot.cron.execution.which", return_value="/usr/bin/claude"),
            patch("asyncio.create_subprocess_exec", return_value=mock_proc),
        ):
            await observer._execute_job("daily", "Do work", "daily")

        # No exception = success


class TestCronResultDelivery:
    """Result delivery must be robust: no silent drops, no race conditions."""

    async def test_result_delivered_when_job_deleted_after_schedule(self, tmp_path: Path) -> None:
        """Result handler fires even if the job is removed before execution."""
        paths = _make_paths(tmp_path)
        mgr = _make_manager(paths)
        mgr.add_job(_make_job("ephemeral", title="Ephemeral"))
        (paths.cron_tasks_dir / "ephemeral").mkdir()

        observer = _make_observer(paths, mgr)
        callback = AsyncMock(return_value=(True, ""))
        observer.set_result_handler(callback)

        # Remove the job from the manager (simulates a reload that drops it)
        mgr.remove_job("ephemeral")
        assert mgr.get_job("ephemeral") is None

        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b'{"result": "Done"}', b""))

        with (
            time_machine.travel(datetime(2026, 1, 15, 14, 0, tzinfo=UTC)),
            patch("ductor_bot.cron.execution.which", return_value="/usr/bin/claude"),
            patch("asyncio.create_subprocess_exec", return_value=mock_proc),
        ):
            await observer._execute_job("ephemeral", "Do work", "ephemeral")

        # Result must be delivered using job_id as fallback title
        callback.assert_awaited_once()
        title, result_text, status, chat_id, topic_id, transport = callback.call_args[0]
        assert title == "ephemeral"
        assert result_text == "Done"
        assert status == "success"
        assert chat_id == 0
        assert topic_id is None
        assert transport == "tg"

    async def test_result_delivered_before_file_write(self, tmp_path: Path) -> None:
        """Result handler is called before update_run_status writes to disk."""
        paths = _make_paths(tmp_path)
        mgr = _make_manager(paths)
        mgr.add_job(_make_job("daily", title="My Task"))
        (paths.cron_tasks_dir / "daily").mkdir()

        observer = _make_observer(paths, mgr)

        call_order: list[str] = []

        async def track_result(*_args: object) -> tuple[bool, str]:
            call_order.append("result_delivered")
            return True, ""

        original_update = mgr.update_run_status

        def track_update(job_id: str, *, status: str, **_kwargs: object) -> None:
            call_order.append("file_written")
            original_update(job_id, status=status)

        observer.set_result_handler(track_result)

        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b'{"result": "ok"}', b""))

        with (
            time_machine.travel(datetime(2026, 1, 15, 14, 0, tzinfo=UTC)),
            patch("ductor_bot.cron.execution.which", return_value="/usr/bin/claude"),
            patch("asyncio.create_subprocess_exec", return_value=mock_proc),
            patch.object(mgr, "update_run_status", side_effect=track_update),
        ):
            await observer._execute_job("daily", "Do work", "daily")

        assert call_order == ["result_delivered", "file_written"]

    async def test_result_delivered_for_cli_not_found(self, tmp_path: Path) -> None:
        """CLI-not-found error is delivered to the result handler."""
        paths = _make_paths(tmp_path)
        mgr = _make_manager(paths)
        mgr.add_job(_make_job("broken", title="Broken Job"))
        (paths.cron_tasks_dir / "broken").mkdir()

        observer = _make_observer(paths, mgr)
        callback = AsyncMock(return_value=(True, ""))
        observer.set_result_handler(callback)

        with (
            time_machine.travel(datetime(2026, 1, 15, 14, 0, tzinfo=UTC)),
            patch("ductor_bot.cron.execution.which", return_value=None),
        ):
            await observer._execute_job("broken", "Do work", "broken")

        callback.assert_awaited_once()
        title, result_text, _status, chat_id, topic_id, transport = callback.call_args[0]
        assert title == "Broken Job"
        assert "not found" in result_text.lower()
        assert chat_id == 0
        assert topic_id is None
        assert transport == "tg"

    async def test_silent_on_success_suppresses_delivery(self, tmp_path: Path) -> None:
        """silent_on_success mutes the push on success but still records status (#133)."""
        paths = _make_paths(tmp_path)
        mgr = _make_manager(paths)
        mgr.add_job(_make_job("reindex", title="Reindex", silent_on_success=True))
        (paths.cron_tasks_dir / "reindex").mkdir()

        observer = _make_observer(paths, mgr)
        callback = AsyncMock(return_value=(True, ""))
        observer.set_result_handler(callback)

        mock_proc = AsyncMock()
        mock_proc.returncode = 0
        mock_proc.communicate = AsyncMock(return_value=(b'{"result": "indexed 5000 files"}', b""))

        with (
            time_machine.travel(datetime(2026, 1, 15, 14, 0, tzinfo=UTC)),
            patch("ductor_bot.cron.execution.which", return_value="/usr/bin/claude"),
            patch("asyncio.create_subprocess_exec", return_value=mock_proc),
        ):
            await observer._execute_job("reindex", "Do work", "reindex")

        callback.assert_not_awaited()
        job = mgr.get_job("reindex")
        assert job is not None
        assert job.last_run_status == "success"

    async def test_silent_on_success_still_delivers_on_error(self, tmp_path: Path) -> None:
        """silent_on_success never mutes the failure path (#133)."""
        paths = _make_paths(tmp_path)
        mgr = _make_manager(paths)
        mgr.add_job(_make_job("reindex", title="Reindex", silent_on_success=True))
        (paths.cron_tasks_dir / "reindex").mkdir()

        observer = _make_observer(paths, mgr)
        callback = AsyncMock(return_value=(True, ""))
        observer.set_result_handler(callback)

        mock_proc = AsyncMock()
        mock_proc.returncode = 1
        mock_proc.communicate = AsyncMock(return_value=(b'{"result": "boom"}', b""))

        with (
            time_machine.travel(datetime(2026, 1, 15, 14, 0, tzinfo=UTC)),
            patch("ductor_bot.cron.execution.which", return_value="/usr/bin/claude"),
            patch("asyncio.create_subprocess_exec", return_value=mock_proc),
        ):
            await observer._execute_job("reindex", "Do work", "reindex")

        callback.assert_awaited_once()
        status = callback.call_args[0][2]
        assert status.startswith("error")

    async def test_run_at_catches_unexpected_exception(self, tmp_path: Path) -> None:
        """Unexpected exception in _execute_job does not crash the observer."""
        paths = _make_paths(tmp_path)
        mgr = _make_manager(paths)
        observer = _make_observer(paths, mgr)
        observer._running = True

        from ductor_bot.cron.observer import _ScheduledJob

        job = _ScheduledJob(
            id="crash",
            schedule="* * * * *",
            instruction="Do",
            task_folder="crash",
            timezone="",
        )

        with patch.object(observer, "_execute_job", side_effect=RuntimeError("boom")):
            # Must not raise — the exception is logged and the job is rescheduled
            await observer._run_at(0, job)


class TestEnrichInstruction:
    """Tests for enrich_instruction helper."""

    def test_appends_memory_references(self) -> None:
        result = enrich_instruction("Do the work", "daily-report")
        assert "Do the work" in result
        assert "daily-report_MEMORY.md" in result
        assert "IMPORTANT" in result

    def test_preserves_original_instruction(self) -> None:
        original = "Generate weekly summary of all commits"
        result = enrich_instruction(original, "weekly")
        assert result.startswith(original)


class TestParseCLIResults:
    """Tests for result parsing helpers."""

    def test_parse_claude_json_result(self) -> None:
        stdout = b'{"result": "Hello world"}'
        assert parse_claude_result(stdout) == "Hello world"

    def test_parse_claude_empty_stdout(self) -> None:
        assert parse_claude_result(b"") == ""

    def test_parse_claude_non_json_fallback(self) -> None:
        raw = b"Some raw text output"
        assert parse_claude_result(raw) == "Some raw text output"

    def test_parse_claude_truncates_long_non_json(self) -> None:
        raw = b"x" * 3000
        result = parse_claude_result(raw)
        assert len(result) == 2000

    def test_parse_claude_missing_result_key(self) -> None:
        stdout = b'{"other": "value"}'
        assert parse_claude_result(stdout) == ""

    def test_parse_codex_empty_stdout(self) -> None:
        assert parse_codex_result(b"") == ""

    def test_parse_codex_jsonl_with_agent_message(self) -> None:
        line = b'{"type":"item.completed","item":{"type":"agent_message","text":"Weather report done."}}'
        assert parse_codex_result(line) == "Weather report done."
