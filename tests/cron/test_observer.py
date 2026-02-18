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
from ductor_bot.config import AgentConfig, ModelRegistry
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


def _make_models() -> ModelRegistry:
    """Return a real ModelRegistry (stateless, no file needed)."""
    return ModelRegistry()


def _make_codex_cache() -> CodexModelCache:
    """Return a mock CodexModelCache."""
    cache = MagicMock(spec=CodexModelCache)
    cache.validate_model.return_value = True
    cache.get_model.return_value = CodexModelInfo(
        id="gpt-5.2-codex",
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
    models: ModelRegistry | None = None,
    codex_cache: CodexModelCache | None = None,
    **config_overrides: Any,
) -> CronObserver:
    return CronObserver(
        paths,
        mgr,
        config=_make_config(**config_overrides),
        models=models or _make_models(),
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
        from ductor_bot.cron.observer import CronObserver  # noqa: F401

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


class TestCronObserverExecution:
    """Job execution tests."""

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

        observer = _make_observer(paths, mgr, models=_make_models())

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
        codex_cache = _make_codex_cache()
        codex_cache.validate_model.return_value = True
        codex_cache.get_model.return_value = CodexModelInfo(
            id="gpt-5.2",
            display_name="GPT-5.2",
            description="Codex model",
            supported_efforts=("low", "medium", "high"),
            default_effort="medium",
            is_default=True,
        )

        observer = _make_observer(
            paths,
            mgr,
            models=_make_models(),
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
        callback = AsyncMock()
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

        callback.assert_awaited_once_with("My Daily Task", "All done.", "success")

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

        async def _communicate_side_effect() -> tuple[bytes, bytes]:
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

        mock_proc.kill.assert_called_once()
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
