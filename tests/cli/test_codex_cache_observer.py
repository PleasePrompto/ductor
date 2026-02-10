"""Tests for CodexCacheObserver: periodic Codex model cache refresh."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from ductor_bot.cli.codex_cache import CodexModelCache
from ductor_bot.cli.codex_cache_observer import CodexCacheObserver
from ductor_bot.cli.codex_discovery import CodexModelInfo


@pytest.fixture
def mock_cache() -> CodexModelCache:
    """Create a mock CodexModelCache."""
    return CodexModelCache(
        last_updated="2025-01-01T00:00:00Z",
        models=[
            CodexModelInfo(
                id="gpt-5.2-codex",
                display_name="GPT-5.2 Codex",
                description="Codex model",
                supported_efforts=("low", "medium", "high"),
                default_effort="medium",
                is_default=True,
            ),
        ],
    )


class TestCodexCacheObserver:
    """Test CodexCacheObserver lifecycle."""

    async def test_observer_loads_cache_at_start(
        self,
        tmp_path: Path,
        mock_cache: CodexModelCache,
    ) -> None:
        """Observer loads cache on start()."""
        cache_path = tmp_path / "codex_cache.json"
        observer = CodexCacheObserver(cache_path)

        with patch(
            "ductor_bot.cli.codex_cache_observer.CodexModelCache.load_or_refresh",
            new_callable=AsyncMock,
        ) as mock_load:
            mock_load.return_value = mock_cache

            await observer.start()

            # Verify cache was loaded
            mock_load.assert_called_once_with(cache_path)
            assert observer.get_cache() is mock_cache
            assert observer._running is True
            assert observer._task is not None

            # Clean up
            await observer.stop()

    async def test_observer_stop_cancels_task(
        self,
        tmp_path: Path,
        mock_cache: CodexModelCache,
    ) -> None:
        """Observer.stop() cancels refresh task cleanly."""
        cache_path = tmp_path / "codex_cache.json"
        observer = CodexCacheObserver(cache_path)

        with patch(
            "ductor_bot.cli.codex_cache_observer.CodexModelCache.load_or_refresh",
            new_callable=AsyncMock,
        ) as mock_load:
            mock_load.return_value = mock_cache

            await observer.start()

            # Verify task is running
            assert observer._task is not None
            assert not observer._task.done()

            # Stop observer
            await observer.stop()

            # Verify task is cancelled and stopped
            assert observer._running is False
            assert observer._task is None

    async def test_observer_get_cache_returns_none_before_start(self, tmp_path: Path) -> None:
        """get_cache() returns None before start() is called."""
        cache_path = tmp_path / "codex_cache.json"
        observer = CodexCacheObserver(cache_path)

        assert observer.get_cache() is None

    async def test_observer_verifies_60_minute_interval(
        self,
        tmp_path: Path,
        mock_cache: CodexModelCache,
    ) -> None:
        """Verify observer's code uses 3600 seconds (60 minutes)."""
        # This test verifies the implementation uses the correct constant
        # by checking the source code directly rather than mocking the event loop
        import inspect

        from ductor_bot.cli.codex_cache_observer import CodexCacheObserver

        source = inspect.getsource(CodexCacheObserver._refresh_loop)
        # Verify the source code contains 3600 (60 minutes in seconds)
        assert "3600" in source, "Refresh loop should use 3600 seconds (60 minutes)"
