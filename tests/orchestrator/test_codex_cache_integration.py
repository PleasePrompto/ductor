"""Tests for Codex cache integration into orchestrator."""

from __future__ import annotations

from datetime import UTC, datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ductor_bot.cli.codex_cache import CodexModelCache
from ductor_bot.cli.codex_discovery import CodexModelInfo


@pytest.fixture
def mock_codex_cache() -> CodexModelCache:
    """Mock Codex cache with sample models."""
    return CodexModelCache(
        last_updated=datetime.now(UTC).isoformat(),
        models=[
            CodexModelInfo(
                id="gpt-4o",
                display_name="GPT-4o",
                description="GPT-4o model",
                supported_efforts=("low", "medium", "high"),
                default_effort="medium",
                is_default=True,
            ),
        ],
    )


async def test_orchestrator_starts_cache_observer(mock_codex_cache: CodexModelCache) -> None:
    """Should start CodexCacheObserver during orchestrator creation."""
    from ductor_bot.config import AgentConfig
    from ductor_bot.orchestrator.core import Orchestrator

    mock_observer = MagicMock()
    mock_observer.start = AsyncMock()
    mock_observer.stop = AsyncMock()
    mock_observer.get_cache = MagicMock(return_value=mock_codex_cache)

    mock_config = AgentConfig()

    with (
        patch("ductor_bot.orchestrator.core.CodexCacheObserver", return_value=mock_observer),
        patch("ductor_bot.orchestrator.core.resolve_paths"),
        patch("ductor_bot.orchestrator.core.init_workspace"),
        patch("ductor_bot.orchestrator.core.inject_runtime_environment"),
        patch("ductor_bot.cli.auth.check_all_auth", return_value={}),
    ):
        orch = await Orchestrator.create(mock_config)

        # Verify observer was started
        mock_observer.start.assert_called_once()

        await orch.shutdown()


async def test_orchestrator_passes_cache_to_observers(
    mock_codex_cache: CodexModelCache,
) -> None:
    """Should pass Codex cache to CronObserver and WebhookObserver."""
    from ductor_bot.config import AgentConfig
    from ductor_bot.orchestrator.core import Orchestrator

    mock_cache_observer = MagicMock()
    mock_cache_observer.start = AsyncMock()
    mock_cache_observer.stop = AsyncMock()
    mock_cache_observer.get_cache = MagicMock(return_value=mock_codex_cache)

    mock_cron_instance = MagicMock()
    mock_cron_instance.start = AsyncMock()
    mock_cron_instance.stop = AsyncMock()
    mock_cron_class = MagicMock(return_value=mock_cron_instance)

    mock_webhook_instance = MagicMock()
    mock_webhook_instance.start = AsyncMock()
    mock_webhook_instance.stop = AsyncMock()
    mock_webhook_class = MagicMock(return_value=mock_webhook_instance)

    mock_config = AgentConfig()

    with (
        patch("ductor_bot.orchestrator.core.CodexCacheObserver", return_value=mock_cache_observer),
        patch("ductor_bot.orchestrator.core.CronObserver", mock_cron_class),
        patch("ductor_bot.orchestrator.core.WebhookObserver", mock_webhook_class),
        patch("ductor_bot.orchestrator.core.resolve_paths"),
        patch("ductor_bot.orchestrator.core.init_workspace"),
        patch("ductor_bot.orchestrator.core.inject_runtime_environment"),
        patch("ductor_bot.cli.auth.check_all_auth", return_value={}),
    ):
        orch = await Orchestrator.create(mock_config)

        # Verify cache was passed to observers
        # Check call_args for codex_cache keyword argument
        assert mock_cron_class.called, "CronObserver should be instantiated"
        assert mock_webhook_class.called, "WebhookObserver should be instantiated"

        # Check if codex_cache was passed
        cron_kwargs = mock_cron_class.call_args[1]
        webhook_kwargs = mock_webhook_class.call_args[1]

        assert "codex_cache" in cron_kwargs, "CronObserver should receive codex_cache"
        assert cron_kwargs["codex_cache"] == mock_codex_cache

        assert "codex_cache" in webhook_kwargs, "WebhookObserver should receive codex_cache"
        assert webhook_kwargs["codex_cache"] == mock_codex_cache

        await orch.shutdown()


async def test_orchestrator_stops_cache_observer(mock_codex_cache: CodexModelCache) -> None:
    """Should stop CodexCacheObserver during orchestrator shutdown."""
    from ductor_bot.config import AgentConfig
    from ductor_bot.orchestrator.core import Orchestrator

    mock_observer = MagicMock()
    mock_observer.start = AsyncMock()
    mock_observer.stop = AsyncMock()
    mock_observer.get_cache = MagicMock(return_value=mock_codex_cache)

    mock_config = AgentConfig()

    with (
        patch("ductor_bot.orchestrator.core.CodexCacheObserver", return_value=mock_observer),
        patch("ductor_bot.orchestrator.core.resolve_paths"),
        patch("ductor_bot.orchestrator.core.init_workspace"),
        patch("ductor_bot.orchestrator.core.inject_runtime_environment"),
        patch("ductor_bot.cli.auth.check_all_auth", return_value={}),
    ):
        orch = await Orchestrator.create(mock_config)

        # Shutdown orchestrator
        await orch.shutdown()

        # Verify observer was stopped
        mock_observer.stop.assert_called_once()
