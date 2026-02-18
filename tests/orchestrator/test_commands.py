"""Tests for command handlers."""

from __future__ import annotations

import json
from unittest.mock import AsyncMock, patch

from ductor_bot.cli.auth import AuthResult, AuthStatus
from ductor_bot.orchestrator.commands import (
    cmd_cron,
    cmd_diagnose,
    cmd_memory,
    cmd_model,
    cmd_status,
    cmd_stop,
)
from ductor_bot.orchestrator.core import Orchestrator

# -- cmd_model (wizard + direct switch) --

_AUTHED = {
    "claude": AuthResult("claude", AuthStatus.AUTHENTICATED),
    "codex": AuthResult("codex", AuthStatus.AUTHENTICATED),
}


async def test_model_list_returns_keyboard(orch: Orchestrator) -> None:
    with patch("ductor_bot.orchestrator.model_selector.check_all_auth", return_value=_AUTHED):
        result = await cmd_model(orch, 1, "/model")
    assert result.reply_markup is not None
    assert "Model Selector" in result.text


async def test_model_direct_switch(orch: Orchestrator) -> None:
    kill_mock = AsyncMock(return_value=0)
    object.__setattr__(orch._process_registry, "kill_all", kill_mock)
    object.__setattr__(orch._sessions, "reset_session", AsyncMock())
    result = await cmd_model(orch, 1, "/model sonnet")
    assert "opus" in result.text
    assert "sonnet" in result.text
    assert orch._config.model == "sonnet"
    kill_mock.assert_called_once_with(1)


async def test_model_already_set(orch: Orchestrator) -> None:
    result = await cmd_model(orch, 1, "/model opus")
    assert "Already running" in result.text


async def test_model_provider_change(orch: Orchestrator) -> None:
    object.__setattr__(orch._process_registry, "kill_all", AsyncMock(return_value=0))
    object.__setattr__(orch._sessions, "reset_session", AsyncMock())
    result = await cmd_model(orch, 1, "/model o3")
    assert "Provider:" in result.text


async def test_model_switch_persists_to_config(orch: Orchestrator) -> None:
    object.__setattr__(orch._process_registry, "kill_all", AsyncMock(return_value=0))
    object.__setattr__(orch._sessions, "reset_session", AsyncMock())
    await cmd_model(orch, 1, "/model sonnet")
    saved = json.loads(orch.paths.config_path.read_text(encoding="utf-8"))
    assert saved["model"] == "sonnet"
    assert saved["provider"] == "claude"


async def test_model_provider_change_persists_to_config(orch: Orchestrator) -> None:
    object.__setattr__(orch._process_registry, "kill_all", AsyncMock(return_value=0))
    object.__setattr__(orch._sessions, "reset_session", AsyncMock())
    await cmd_model(orch, 1, "/model o3")
    saved = json.loads(orch.paths.config_path.read_text(encoding="utf-8"))
    assert saved["model"] == "o3"
    assert saved["provider"] == "codex"


async def test_model_same_provider_resets_session(orch: Orchestrator) -> None:
    kill_mock = AsyncMock(return_value=0)
    object.__setattr__(orch._process_registry, "kill_all", kill_mock)
    object.__setattr__(orch._sessions, "reset_session", AsyncMock())
    result = await cmd_model(orch, 1, "/model sonnet")
    assert "Session reset" in result.text
    assert "Provider:" not in result.text
    kill_mock.assert_called_once_with(1)


# -- cmd_status --


async def test_status_no_session(orch: Orchestrator) -> None:
    with patch("ductor_bot.orchestrator.commands.check_all_auth", return_value={}):
        result = await cmd_status(orch, 1, "/status")
    assert "No active session" in result.text
    assert "opus" in result.text


async def test_status_with_session(orch: Orchestrator) -> None:
    await orch._sessions.resolve_session(1)
    with patch("ductor_bot.orchestrator.commands.check_all_auth", return_value={}):
        result = await cmd_status(orch, 1, "/status")
    assert "Session:" in result.text
    assert "Messages:" in result.text


async def test_status_prefers_session_model_over_config(orch: Orchestrator) -> None:
    await orch._sessions.resolve_session(1, provider="codex", model="gpt-5.2-codex")
    with patch("ductor_bot.orchestrator.commands.check_all_auth", return_value={}):
        result = await cmd_status(orch, 1, "/status")
    assert "Model: gpt-5.2-codex (configured: opus)" in result.text


# -- cmd_memory --


async def test_memory_shows_content(orch: Orchestrator) -> None:
    orch.paths.mainmemory_path.write_text("# My Memories\n- Learned X")
    result = await cmd_memory(orch, 0, "/memory")
    assert "My Memories" in result.text


async def test_memory_empty(orch: Orchestrator) -> None:
    orch.paths.mainmemory_path.write_text("")
    result = await cmd_memory(orch, 0, "/memory")
    assert "empty" in result.text.lower()


# -- cmd_cron --


async def test_cron_no_jobs(orch: Orchestrator) -> None:
    result = await cmd_cron(orch, 0, "/cron")
    assert "No cron jobs" in result.text


async def test_cron_lists_jobs(orch: Orchestrator) -> None:
    from ductor_bot.cron.manager import CronJob

    orch._cron_manager.add_job(
        CronJob(
            id="test-job",
            title="Test Job",
            description="A test job",
            schedule="0 9 * * *",
            agent_instruction="do stuff",
            task_folder="test-task",
        ),
    )
    result = await cmd_cron(orch, 0, "/cron")
    assert result.reply_markup is not None
    assert "0 9 * * *" in result.text
    assert "Test Job" in result.text
    assert "Tap a button to toggle" in result.text


# -- cmd_diagnose --


async def test_diagnose_no_logs(orch: Orchestrator) -> None:
    result = await cmd_diagnose(orch, 0, "/diagnose")
    assert "Diagnostics" in result.text
    assert "No log file" in result.text


async def test_diagnose_with_logs(orch: Orchestrator) -> None:
    log_path = orch.paths.logs_dir / "agent.log"
    log_path.write_text("2024-01-01 INFO Started\n2024-01-01 ERROR Something broke\n")
    result = await cmd_diagnose(orch, 0, "/diagnose")
    assert "Something broke" in result.text


async def test_diagnose_shows_cache_status(orch: Orchestrator) -> None:
    """Should display Codex cache status in /diagnose output."""
    from datetime import UTC, datetime
    from unittest.mock import MagicMock

    from ductor_bot.cli.codex_cache import CodexModelCache
    from ductor_bot.cli.codex_discovery import CodexModelInfo

    # Create mock cache with test data
    mock_cache = CodexModelCache(
        last_updated=datetime.now(UTC).isoformat(),
        models=[
            CodexModelInfo(
                id="gpt-4o",
                display_name="GPT-4o",
                description="Test model",
                supported_efforts=("low", "medium", "high"),
                default_effort="medium",
                is_default=True,
            ),
        ],
    )

    # Mock the cache observer
    mock_observer = MagicMock()
    mock_observer.get_cache = MagicMock(return_value=mock_cache)
    object.__setattr__(orch, "_codex_cache_observer", mock_observer)

    result = await cmd_diagnose(orch, 0, "/diagnose")

    # Verify cache info is in output
    assert "Codex Model Cache" in result.text
    assert "Models cached: 1" in result.text
    assert "Default model: gpt-4o" in result.text


async def test_diagnose_shows_effective_runtime_target(orch: Orchestrator) -> None:
    object.__setattr__(orch, "_available_providers", frozenset({"codex"}))

    result = await cmd_diagnose(orch, 0, "/diagnose")

    assert "Configured: claude / opus" in result.text
    assert "Effective runtime: codex / gpt-5.2-codex" in result.text


# -- cmd_stop --


async def test_stop_kills_one_process(orch: Orchestrator) -> None:
    object.__setattr__(orch._process_registry, "kill_all", AsyncMock(return_value=1))
    result = await cmd_stop(orch, 1, "/stop")
    assert "terminated" in result.text
    assert "queued messages discarded" in result.text


async def test_stop_kills_multiple_processes(orch: Orchestrator) -> None:
    object.__setattr__(orch._process_registry, "kill_all", AsyncMock(return_value=3))
    result = await cmd_stop(orch, 1, "/stop")
    assert "terminated" in result.text


# -- cmd_model (unknown model) --


async def test_model_unknown_name(orch: Orchestrator) -> None:
    """Unknown model names are treated as codex models and the switch succeeds."""
    object.__setattr__(orch._process_registry, "kill_all", AsyncMock(return_value=0))
    object.__setattr__(orch._sessions, "reset_session", AsyncMock())
    result = await cmd_model(orch, 1, "/model totally_fake_model")
    assert "Model switched" in result.text
    assert "totally_fake_model" in result.text
    assert orch._config.model == "totally_fake_model"
    assert orch._config.provider == "codex"
