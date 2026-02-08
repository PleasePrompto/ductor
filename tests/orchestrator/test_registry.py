"""Tests for CommandRegistry and OrchestratorResult."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from ductor_bot.orchestrator.registry import CommandRegistry, OrchestratorResult


@pytest.fixture
def registry() -> CommandRegistry:
    return CommandRegistry()


def test_orchestrator_result_defaults() -> None:
    r = OrchestratorResult(text="hello")
    assert r.text == "hello"
    assert r.stream_fallback is False


async def test_dispatch_async_handler(registry: CommandRegistry) -> None:
    handler = AsyncMock(return_value=OrchestratorResult(text="ok"))
    registry.register_async("/test", handler)

    result = await registry.dispatch("/test", AsyncMock(), 1, "/test")
    assert result is not None
    assert result.text == "ok"
    handler.assert_called_once()


async def test_dispatch_unknown_returns_none(registry: CommandRegistry) -> None:
    result = await registry.dispatch("/unknown", AsyncMock(), 1, "/unknown")
    assert result is None


async def test_prefix_match(registry: CommandRegistry) -> None:
    handler = AsyncMock(return_value=OrchestratorResult(text="matched"))
    registry.register_async("/model ", handler)

    result = await registry.dispatch("/model opus", AsyncMock(), 1, "/model opus")
    assert result is not None
    assert result.text == "matched"


async def test_exact_match_no_extra(registry: CommandRegistry) -> None:
    handler = AsyncMock(return_value=OrchestratorResult(text="ok"))
    registry.register_async("/status", handler)

    result = await registry.dispatch("/status extra", AsyncMock(), 1, "/status extra")
    assert result is None
