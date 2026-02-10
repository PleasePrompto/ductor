"""Integration tests for cron task override system."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from ductor_bot.cli.codex_cache import CodexModelCache
from ductor_bot.cli.codex_discovery import CodexModelInfo
from ductor_bot.cli.param_resolver import TaskOverrides, resolve_cli_config
from ductor_bot.config import AgentConfig
from ductor_bot.cron.execution import build_cmd
from ductor_bot.cron.manager import CronJob, CronManager
from ductor_bot.workspace.paths import DuctorPaths


@pytest.fixture
def mock_paths(tmp_path: Path) -> DuctorPaths:
    """Create mock DuctorPaths for testing."""
    home = tmp_path / "ductor_home"
    fw = tmp_path / "fw"
    paths = DuctorPaths(
        ductor_home=home,
        home_defaults=fw / "workspace",
        framework_root=fw,
    )
    # Create required directories
    paths.cron_tasks_dir.mkdir(parents=True, exist_ok=True)
    paths.config_dir.mkdir(parents=True, exist_ok=True)
    return paths


@pytest.fixture
def mock_codex_cache() -> CodexModelCache:
    """Create mock CodexModelCache with test models."""
    return CodexModelCache(
        last_updated="2025-01-01T00:00:00Z",
        models=[
            CodexModelInfo(
                id="gpt-5.2-codex",
                display_name="GPT-5.2 Codex",
                description="Test codex model",
                supported_efforts=("low", "medium", "high"),
                default_effort="medium",
                is_default=True,
            ),
        ],
    )


@pytest.fixture
def base_config() -> AgentConfig:
    """Base AgentConfig for testing."""
    return AgentConfig(
        provider="claude",
        model="opus",
        cli_parameters={"claude": [], "codex": []},
        permission_mode="bypassPermissions",
        cli_timeout=600,
    )


async def test_cron_task_model_override(
    mock_paths: DuctorPaths,
    base_config: AgentConfig,
    mock_codex_cache: CodexModelCache,
) -> None:
    """Verify that a cron task with a custom model executes correctly."""
    # Create test job with model override
    manager = CronManager(jobs_path=mock_paths.cron_jobs_path)
    job = CronJob(
        id="test-job",
        title="Test Job",
        description="Test job description",
        schedule="0 8 * * *",
        task_folder="test_task",
        agent_instruction="Test instruction",
        model="sonnet",  # Override to sonnet
    )
    manager.add_job(job)

    # Create task folder
    task_folder = mock_paths.cron_tasks_dir / "test_task"
    task_folder.mkdir()

    # Resolve configuration with override
    overrides = TaskOverrides(
        provider=job.provider,
        model=job.model,
        reasoning_effort=job.reasoning_effort,
        cli_parameters=job.cli_parameters or [],
    )
    exec_config = resolve_cli_config(base_config, mock_codex_cache, task_overrides=overrides)

    # Build command
    with patch("ductor_bot.cron.execution.which", return_value="/usr/bin/claude"):
        cmd = build_cmd(exec_config, "Test prompt")

    # Verify command structure
    assert cmd is not None
    assert cmd[0] == "/usr/bin/claude"
    assert "--model" in cmd
    assert "sonnet" in cmd  # Model override applied
    assert cmd[-2] == "--"
    assert cmd[-1] == "Test prompt"


async def test_cron_task_cli_parameters(
    mock_paths: DuctorPaths,
    base_config: AgentConfig,
    mock_codex_cache: CodexModelCache,
) -> None:
    """Verify that a cron task with custom CLI parameters works."""
    # Create test job with CLI parameters
    manager = CronManager(jobs_path=mock_paths.cron_jobs_path)
    job = CronJob(
        id="test-job",
        title="Test Job",
        description="Test job description",
        schedule="0 8 * * *",
        task_folder="test_task",
        agent_instruction="Test instruction",
        provider="codex",
        model="gpt-5.2-codex",
        cli_parameters=["--chrome"],  # Custom CLI parameter
    )
    manager.add_job(job)

    # Create task folder
    task_folder = mock_paths.cron_tasks_dir / "test_task"
    task_folder.mkdir()

    # Resolve configuration with override
    overrides = TaskOverrides(
        provider=job.provider,
        model=job.model,
        reasoning_effort=job.reasoning_effort,
        cli_parameters=job.cli_parameters or [],
    )
    exec_config = resolve_cli_config(base_config, mock_codex_cache, task_overrides=overrides)

    # Build command
    with patch("ductor_bot.cron.execution.which", return_value="/usr/bin/codex"):
        cmd = build_cmd(exec_config, "Test prompt")

    # Verify --chrome appears in command before --
    assert cmd is not None
    assert "--chrome" in cmd
    separator_idx = cmd.index("--")
    assert cmd.index("--chrome") < separator_idx
    assert cmd[-1] == "Test prompt"


async def test_cron_task_reasoning_effort(
    mock_paths: DuctorPaths,
    base_config: AgentConfig,
    mock_codex_cache: CodexModelCache,
) -> None:
    """Verify Codex reasoning effort in cron task."""
    # Create test job with reasoning effort override
    manager = CronManager(jobs_path=mock_paths.cron_jobs_path)
    job = CronJob(
        id="test-job",
        title="Test Job",
        description="Test job description",
        schedule="0 8 * * *",
        task_folder="test_task",
        agent_instruction="Test instruction",
        provider="codex",
        model="gpt-5.2-codex",
        reasoning_effort="high",  # High reasoning effort
    )
    manager.add_job(job)

    # Create task folder
    task_folder = mock_paths.cron_tasks_dir / "test_task"
    task_folder.mkdir()

    # Resolve configuration with override
    overrides = TaskOverrides(
        provider=job.provider,
        model=job.model,
        reasoning_effort=job.reasoning_effort,
        cli_parameters=job.cli_parameters or [],
    )
    exec_config = resolve_cli_config(base_config, mock_codex_cache, task_overrides=overrides)

    # Build command
    with patch("ductor_bot.cron.execution.which", return_value="/usr/bin/codex"):
        cmd = build_cmd(exec_config, "Test prompt")

    # Verify reasoning effort parameter
    assert cmd is not None
    assert "-c" in cmd
    assert "model_reasoning_effort=high" in cmd
    # Verify it's positioned before --
    separator_idx = cmd.index("--")
    config_idx = cmd.index("-c")
    assert config_idx < separator_idx


async def test_cron_task_fallback_to_global(
    mock_paths: DuctorPaths,
    mock_codex_cache: CodexModelCache,
) -> None:
    """Verify that tasks without overrides use global config."""
    # Use global config with default provider and model
    global_config = AgentConfig(
        provider="claude",
        model="opus",
        cli_parameters={"claude": [], "codex": []},
        permission_mode="bypassPermissions",
        cli_timeout=600,
    )

    # Create test job with NO overrides
    manager = CronManager(jobs_path=mock_paths.cron_jobs_path)
    job = CronJob(
        id="test-job",
        title="Test Job",
        description="Test job description",
        schedule="0 8 * * *",
        task_folder="test_task",
        agent_instruction="Test instruction",
        # No provider, model, reasoning_effort, or cli_parameters
    )
    manager.add_job(job)

    # Create task folder
    task_folder = mock_paths.cron_tasks_dir / "test_task"
    task_folder.mkdir()

    # Resolve configuration with empty overrides
    overrides = TaskOverrides(
        provider=job.provider,
        model=job.model,
        reasoning_effort=job.reasoning_effort,
        cli_parameters=job.cli_parameters or [],
    )
    exec_config = resolve_cli_config(global_config, mock_codex_cache, task_overrides=overrides)

    # Build command
    with patch("ductor_bot.cron.execution.which", return_value="/usr/bin/claude"):
        cmd = build_cmd(exec_config, "Test prompt")

    # Verify global config is used
    assert cmd is not None
    assert cmd[0] == "/usr/bin/claude"
    assert "--model" in cmd
    assert "opus" in cmd  # Global model used when task has no override
    # No CLI parameters since task has empty cli_parameters list


async def test_cron_task_provider_switch(
    mock_paths: DuctorPaths,
    base_config: AgentConfig,
    mock_codex_cache: CodexModelCache,
) -> None:
    """Verify a task can use a different provider than main agent."""
    # Set global config to Claude
    base_config = AgentConfig(
        provider="claude",
        model="opus",
        cli_parameters={"claude": [], "codex": []},
        permission_mode="bypassPermissions",
        cli_timeout=600,
    )

    # Create test job with Codex override
    manager = CronManager(jobs_path=mock_paths.cron_jobs_path)
    job = CronJob(
        id="test-job",
        title="Test Job",
        description="Test job description",
        schedule="0 8 * * *",
        task_folder="test_task",
        agent_instruction="Test instruction",
        provider="codex",  # Switch to Codex
        model="gpt-5.2-codex",
    )
    manager.add_job(job)

    # Create task folder
    task_folder = mock_paths.cron_tasks_dir / "test_task"
    task_folder.mkdir()

    # Resolve configuration with override
    overrides = TaskOverrides(
        provider=job.provider,
        model=job.model,
        reasoning_effort=job.reasoning_effort,
        cli_parameters=job.cli_parameters or [],
    )
    exec_config = resolve_cli_config(base_config, mock_codex_cache, task_overrides=overrides)

    # Build command
    with patch("ductor_bot.cron.execution.which", return_value="/usr/bin/codex"):
        cmd = build_cmd(exec_config, "Test prompt")

    # Verify Codex command instead of Claude
    assert cmd is not None
    assert cmd[0] == "/usr/bin/codex"
    assert "exec" in cmd  # Codex uses 'exec' subcommand
    assert "--model" in cmd
    assert "gpt-5.2-codex" in cmd
