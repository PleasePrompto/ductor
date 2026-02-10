"""Tests for cron observer parameter resolver integration."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ductor_bot.cli.codex_cache import CodexModelCache
from ductor_bot.cli.codex_discovery import CodexModelInfo
from ductor_bot.cli.param_resolver import TaskExecutionConfig, TaskOverrides
from ductor_bot.config import AgentConfig
from ductor_bot.cron.manager import CronJob, CronManager
from ductor_bot.cron.observer import CronObserver


@pytest.fixture
def mock_codex_cache() -> CodexModelCache:
    """Create a mock CodexModelCache with predefined models."""
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


@pytest.fixture
def observer(tmp_path: Path, mock_codex_cache: CodexModelCache) -> CronObserver:
    """Create a CronObserver with mock dependencies."""
    from ductor_bot.config import ModelRegistry
    from ductor_bot.workspace.paths import DuctorPaths

    # Mock paths
    paths = MagicMock(spec=DuctorPaths)
    paths.cron_jobs_path = tmp_path / "cron_jobs.json"
    paths.cron_tasks_dir = tmp_path / "cron_tasks"
    paths.cron_tasks_dir.mkdir(exist_ok=True)

    # Create manager
    manager = CronManager(jobs_path=paths.cron_jobs_path)

    # Create config
    config = AgentConfig(
        provider="claude",
        model="opus",
        reasoning_effort="medium",
    )

    # Create observer
    models = ModelRegistry()
    return CronObserver(
        paths=paths,
        manager=manager,
        config=config,
        models=models,
        codex_cache=mock_codex_cache,
    )


class TestResolveExecutionConfig:
    """Test _resolve_execution_config method."""

    def test_resolve_execution_config_no_overrides(
        self,
        observer: CronObserver,
        mock_codex_cache: CodexModelCache,
    ) -> None:
        """Falls back to global config when no task overrides."""
        overrides = TaskOverrides()

        with patch("ductor_bot.cron.observer.resolve_cli_config") as mock_resolve:
            mock_resolve.return_value = TaskExecutionConfig(
                provider="claude",
                model="opus",
                reasoning_effort="",
                cli_parameters=[],
                permission_mode="bypassPermissions",
                working_dir="/tmp",
                file_access="all",
            )

            exec_config = observer._resolve_execution_config(overrides)

        # Should call resolve_cli_config with config and cache
        mock_resolve.assert_called_once()
        call_args = mock_resolve.call_args
        assert call_args[0][0] == observer._config
        assert call_args[0][1] == mock_codex_cache
        assert call_args[1]["task_overrides"] == overrides

        # Should use global config values
        assert exec_config.provider == "claude"
        assert exec_config.model == "opus"

    def test_resolve_execution_config_with_overrides(
        self,
        observer: CronObserver,
        mock_codex_cache: CodexModelCache,
    ) -> None:
        """Task overrides apply correctly."""
        overrides = TaskOverrides(
            provider="codex",
            model="gpt-5.2-codex",
            reasoning_effort="high",
            cli_parameters=["--fast"],
        )

        with patch("ductor_bot.cron.observer.resolve_cli_config") as mock_resolve:
            mock_resolve.return_value = TaskExecutionConfig(
                provider="codex",
                model="gpt-5.2-codex",
                reasoning_effort="high",
                cli_parameters=["--fast"],
                permission_mode="bypassPermissions",
                working_dir="/tmp",
                file_access="all",
            )

            exec_config = observer._resolve_execution_config(overrides)

        # Should pass overrides to resolver
        call_args = mock_resolve.call_args
        passed_overrides = call_args[1]["task_overrides"]
        assert passed_overrides.provider == "codex"
        assert passed_overrides.model == "gpt-5.2-codex"
        assert passed_overrides.reasoning_effort == "high"
        assert passed_overrides.cli_parameters == ["--fast"]

        # Result should reflect overrides
        assert exec_config.provider == "codex"
        assert exec_config.model == "gpt-5.2-codex"
        assert exec_config.reasoning_effort == "high"


class TestExecuteJobWithOverrides:
    """Test _execute_job method with parameter overrides."""

    async def test_execute_job_with_model_override(
        self,
        observer: CronObserver,
        tmp_path: Path,
    ) -> None:
        """Job uses its own model override."""
        # Create job with model override
        job = CronJob(
            id="test-job",
            title="Test Job",
            description="Test",
            schedule="* * * * *",
            task_folder="test",
            agent_instruction="Do work",
            model="sonnet",  # Override
        )
        observer._manager.add_job(job)

        # Create task folder
        task_dir = tmp_path / "cron_tasks" / "test"
        task_dir.mkdir(parents=True)

        # Mock CLI execution
        with (
            patch("ductor_bot.cron.observer.build_cmd") as mock_build,
            patch("ductor_bot.cron.observer.resolve_cli_config") as mock_resolve,
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_subprocess,
        ):
            # Setup resolve to return config with override
            mock_resolve.return_value = TaskExecutionConfig(
                provider="claude",
                model="sonnet",
                reasoning_effort="",
                cli_parameters=[],
                permission_mode="bypassPermissions",
                working_dir=str(tmp_path),
                file_access="all",
            )

            mock_build.return_value = ["/usr/bin/claude", "test"]

            # Mock subprocess
            proc = AsyncMock()
            proc.communicate.return_value = (b'{"result":"done"}', b"")
            proc.returncode = 0
            mock_subprocess.return_value = proc

            await observer._execute_job("test-job", "Do work", "test")

        # Verify resolve was called with task overrides
        mock_resolve.assert_called_once()
        call_args = mock_resolve.call_args
        task_overrides = call_args[1]["task_overrides"]
        assert task_overrides.model == "sonnet"

    async def test_execute_job_with_cli_parameters(
        self,
        observer: CronObserver,
        tmp_path: Path,
    ) -> None:
        """Job CLI parameters appear in command."""
        # Create job with CLI parameters
        job = CronJob(
            id="test-job",
            title="Test Job",
            description="Test",
            schedule="* * * * *",
            task_folder="test",
            agent_instruction="Do work",
            cli_parameters=["--fast", "--verbose"],
        )
        observer._manager.add_job(job)

        # Create task folder
        task_dir = tmp_path / "cron_tasks" / "test"
        task_dir.mkdir(parents=True)

        # Mock CLI execution
        with (
            patch("ductor_bot.cron.observer.build_cmd") as mock_build,
            patch("ductor_bot.cron.observer.resolve_cli_config") as mock_resolve,
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_subprocess,
        ):
            # Setup resolve to return config with CLI params
            mock_resolve.return_value = TaskExecutionConfig(
                provider="claude",
                model="opus",
                reasoning_effort="",
                cli_parameters=["--fast", "--verbose"],
                permission_mode="bypassPermissions",
                working_dir=str(tmp_path),
                file_access="all",
            )

            mock_build.return_value = ["/usr/bin/claude", "--fast", "--verbose", "test"]

            # Mock subprocess
            proc = AsyncMock()
            proc.communicate.return_value = (b'{"result":"done"}', b"")
            proc.returncode = 0
            mock_subprocess.return_value = proc

            await observer._execute_job("test-job", "Do work", "test")

        # Verify build_cmd was called with TaskExecutionConfig containing parameters
        mock_build.assert_called_once()
        exec_config = mock_build.call_args[0][0]
        assert isinstance(exec_config, TaskExecutionConfig)
        assert exec_config.cli_parameters == ["--fast", "--verbose"]

    async def test_execute_job_with_reasoning_effort(
        self,
        observer: CronObserver,
        tmp_path: Path,
    ) -> None:
        """Codex job with reasoning effort override."""
        # Create Codex job with reasoning effort
        job = CronJob(
            id="test-job",
            title="Test Job",
            description="Test",
            schedule="* * * * *",
            task_folder="test",
            agent_instruction="Do work",
            provider="codex",
            model="gpt-5.2-codex",
            reasoning_effort="high",
        )
        observer._manager.add_job(job)

        # Create task folder
        task_dir = tmp_path / "cron_tasks" / "test"
        task_dir.mkdir(parents=True)

        # Mock CLI execution
        with (
            patch("ductor_bot.cron.observer.build_cmd") as mock_build,
            patch("ductor_bot.cron.observer.resolve_cli_config") as mock_resolve,
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_subprocess,
        ):
            # Setup resolve to return Codex config with reasoning effort
            mock_resolve.return_value = TaskExecutionConfig(
                provider="codex",
                model="gpt-5.2-codex",
                reasoning_effort="high",
                cli_parameters=[],
                permission_mode="bypassPermissions",
                working_dir=str(tmp_path),
                file_access="all",
            )

            mock_build.return_value = ["/usr/bin/codex", "exec", "test"]

            # Mock subprocess
            proc = AsyncMock()
            proc.communicate.return_value = (b"", b"")
            proc.returncode = 0
            mock_subprocess.return_value = proc

            await observer._execute_job("test-job", "Do work", "test")

        # Verify resolve was called with reasoning effort override
        mock_resolve.assert_called_once()
        task_overrides = mock_resolve.call_args[1]["task_overrides"]
        assert task_overrides.provider == "codex"
        assert task_overrides.model == "gpt-5.2-codex"
        assert task_overrides.reasoning_effort == "high"

    async def test_execute_job_all_overrides_combined(
        self,
        observer: CronObserver,
        tmp_path: Path,
    ) -> None:
        """Job with all override fields set."""
        # Create job with all overrides
        job = CronJob(
            id="test-job",
            title="Test Job",
            description="Test",
            schedule="* * * * *",
            task_folder="test",
            agent_instruction="Do work",
            provider="codex",
            model="gpt-5.1-codex-mini",
            reasoning_effort="low",
            cli_parameters=["--no-cache", "--debug"],
        )
        observer._manager.add_job(job)

        # Create task folder
        task_dir = tmp_path / "cron_tasks" / "test"
        task_dir.mkdir(parents=True)

        # Mock CLI execution
        with (
            patch("ductor_bot.cron.observer.build_cmd") as mock_build,
            patch("ductor_bot.cron.observer.resolve_cli_config") as mock_resolve,
            patch("asyncio.create_subprocess_exec", new_callable=AsyncMock) as mock_subprocess,
        ):
            # Setup resolve to return full config
            mock_resolve.return_value = TaskExecutionConfig(
                provider="codex",
                model="gpt-5.1-codex-mini",
                reasoning_effort="low",
                cli_parameters=["--no-cache", "--debug"],
                permission_mode="bypassPermissions",
                working_dir=str(tmp_path),
                file_access="all",
            )

            mock_build.return_value = ["/usr/bin/codex", "exec", "test"]

            # Mock subprocess
            proc = AsyncMock()
            proc.communicate.return_value = (b"", b"")
            proc.returncode = 0
            mock_subprocess.return_value = proc

            await observer._execute_job("test-job", "Do work", "test")

        # Verify all overrides were passed
        task_overrides = mock_resolve.call_args[1]["task_overrides"]
        assert task_overrides.provider == "codex"
        assert task_overrides.model == "gpt-5.1-codex-mini"
        assert task_overrides.reasoning_effort == "low"
        assert task_overrides.cli_parameters == ["--no-cache", "--debug"]

        # Verify build_cmd got the full config
        exec_config = mock_build.call_args[0][0]
        assert exec_config.provider == "codex"
        assert exec_config.model == "gpt-5.1-codex-mini"
        assert exec_config.reasoning_effort == "low"
        assert exec_config.cli_parameters == ["--no-cache", "--debug"]
