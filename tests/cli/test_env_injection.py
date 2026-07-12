"""Tests for .env secret injection into subprocess and Docker environments."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

from ductor_bot.cli.base import CLIConfig, docker_wrap
from ductor_bot.cli.executor import build_subprocess_env
from ductor_bot.infra.env_secrets import clear_cache


def test_subprocess_env_merges_secrets(tmp_path: Path) -> None:
    """Secrets from .env are merged into the subprocess env dict."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    env_file = tmp_path / ".env"
    env_file.write_text("MY_SECRET=hunter2\n")

    config = CLIConfig(working_dir=str(workspace))
    clear_cache()
    env = build_subprocess_env(config)

    assert env is not None
    assert env["MY_SECRET"] == "hunter2"


def test_subprocess_env_does_not_override_existing(tmp_path: Path) -> None:
    """Existing environment variables must not be overridden by .env."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    env_file = tmp_path / ".env"
    env_file.write_text("PATH=/evil\n")

    config = CLIConfig(working_dir=str(workspace))
    clear_cache()
    env = build_subprocess_env(config)

    assert env is not None
    assert env["PATH"] != "/evil"


def test_subprocess_env_works_without_env_file(tmp_path: Path) -> None:
    """No .env file should not break subprocess env construction."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    config = CLIConfig(working_dir=str(workspace))
    clear_cache()
    env = build_subprocess_env(config)

    assert env is not None
    assert "DUCTOR_AGENT_NAME" in env


def test_subprocess_env_sets_task_id_for_task_label(tmp_path: Path) -> None:
    """Background-task labels (task:<id>) expose DUCTOR_TASK_ID to the subprocess."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    config = CLIConfig(working_dir=str(workspace), process_label="task:abc123")
    clear_cache()
    env = build_subprocess_env(config)

    assert env is not None
    assert env["DUCTOR_TASK_ID"] == "abc123"


def test_subprocess_env_omits_task_id_for_other_labels(tmp_path: Path) -> None:
    """Non-task labels (main, ns:*, interagent:*) must not leak DUCTOR_TASK_ID."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    for label in ("main", "ns:build", "interagent:worker"):
        config = CLIConfig(working_dir=str(workspace), process_label=label)
        clear_cache()
        env = build_subprocess_env(config)

        assert env is not None
        assert "DUCTOR_TASK_ID" not in env


def test_docker_wrap_sets_task_id_for_task_label(tmp_path: Path) -> None:
    """Docker exec gets DUCTOR_TASK_ID as an -e flag for task labels."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    config = CLIConfig(
        working_dir=str(workspace),
        docker_container="test-container",
        process_label="task:abc123",
    )
    clear_cache()
    cmd, _ = docker_wrap(["codex"], config)

    assert "DUCTOR_TASK_ID=abc123" in cmd


def test_docker_wrap_injects_secrets(tmp_path: Path) -> None:
    """Docker wrap should include .env secrets as -e flags."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    env_file = tmp_path / ".env"
    env_file.write_text("PPLX_API_KEY=sk-test\n")

    config = CLIConfig(
        working_dir=str(workspace),
        docker_container="test-container",
    )
    clear_cache()
    with patch.dict("os.environ", {}, clear=False):
        cmd, cwd = docker_wrap(["gemini"], config)

    assert cwd is None  # Docker mode
    assert "PPLX_API_KEY=sk-test" in cmd


def test_docker_wrap_does_not_override_host_env(tmp_path: Path) -> None:
    """Secrets already in host env must not be duplicated in Docker."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    env_file = tmp_path / ".env"
    env_file.write_text("EXISTING_VAR=from-dotenv\n")

    config = CLIConfig(
        working_dir=str(workspace),
        docker_container="test-container",
    )
    clear_cache()
    with patch.dict("os.environ", {"EXISTING_VAR": "from-host"}, clear=False):
        cmd, _ = docker_wrap(["gemini"], config)

    # The .env value should NOT appear (host env takes precedence).
    assert "EXISTING_VAR=from-dotenv" not in cmd


def test_docker_wrap_provider_extra_env_wins(tmp_path: Path) -> None:
    """Provider-specific extra_env must override .env values."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    env_file = tmp_path / ".env"
    env_file.write_text("GEMINI_API_KEY=from-dotenv\n")

    config = CLIConfig(
        working_dir=str(workspace),
        docker_container="test-container",
    )
    clear_cache()
    with patch.dict("os.environ", {}, clear=False):
        cmd, _ = docker_wrap(
            ["gemini"],
            config,
            extra_env={"GEMINI_API_KEY": "from-provider"},
        )

    assert "GEMINI_API_KEY=from-provider" in cmd
    assert "GEMINI_API_KEY=from-dotenv" not in cmd
