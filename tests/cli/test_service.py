"""Tests for CLIService gateway."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from ductor_bot.cli.process_registry import ProcessRegistry
from ductor_bot.cli.service import CLIService, CLIServiceConfig
from ductor_bot.cli.stream_events import StreamEvent
from ductor_bot.cli.types import AgentRequest, CLIResponse
from ductor_bot.config import ModelRegistry


def _make_service(**overrides: Any) -> CLIService:
    config = CLIServiceConfig(
        working_dir=overrides.pop("working_dir", "/tmp"),
        default_model=overrides.pop("default_model", "opus"),
        provider=overrides.pop("provider", "claude"),
        max_turns=overrides.pop("max_turns", None),
        max_budget_usd=overrides.pop("max_budget_usd", None),
        permission_mode=overrides.pop("permission_mode", "bypassPermissions"),
    )
    models = ModelRegistry()

    return CLIService(
        config=config,
        models=models,
        available_providers=frozenset({"claude"}),
        process_registry=ProcessRegistry(),
    )


async def test_execute_returns_agent_response() -> None:
    svc = _make_service()
    mock_response = CLIResponse(
        result="Hello!",
        session_id="sess-1",
        total_cost_usd=0.05,
        usage={"input_tokens": 500, "output_tokens": 200},
    )
    with patch("ductor_bot.cli.service.create_cli") as mock_create:
        mock_cli = AsyncMock()
        mock_cli.send.return_value = mock_response
        mock_create.return_value = mock_cli

        resp = await svc.execute(AgentRequest(prompt="hello", chat_id=1))

    assert resp.result == "Hello!"
    assert resp.session_id == "sess-1"
    assert resp.cost_usd == 0.05
    assert resp.is_error is False


async def test_execute_error_response() -> None:
    svc = _make_service()
    mock_response = CLIResponse(result="Error occurred", is_error=True)
    with patch("ductor_bot.cli.service.create_cli") as mock_create:
        mock_cli = AsyncMock()
        mock_cli.send.return_value = mock_response
        mock_create.return_value = mock_cli

        resp = await svc.execute(AgentRequest(prompt="fail", chat_id=1))

    assert resp.is_error is True
    assert resp.result == "Error occurred"


async def test_execute_streaming_success() -> None:
    svc = _make_service()

    from ductor_bot.cli.stream_events import AssistantTextDelta, ResultEvent

    async def fake_stream(*_args: Any, **_kwargs: Any) -> AsyncGenerator[StreamEvent, None]:
        yield AssistantTextDelta(type="assistant", text="Hello ")
        yield AssistantTextDelta(type="assistant", text="world!")
        yield ResultEvent(
            type="result",
            session_id="sess-1",
            result="Hello world!",
            total_cost_usd=0.03,
            usage={"input_tokens": 100, "output_tokens": 50},
        )

    deltas: list[str] = []

    async def on_delta(text: str) -> None:
        deltas.append(text)

    with patch("ductor_bot.cli.service.create_cli") as mock_create:
        mock_cli = MagicMock()
        mock_cli.send_streaming = fake_stream
        mock_create.return_value = mock_cli

        resp = await svc.execute_streaming(
            AgentRequest(prompt="hello", chat_id=1),
            on_text_delta=on_delta,
        )

    assert resp.result == "Hello world!"
    assert resp.session_id == "sess-1"
    assert deltas == ["Hello ", "world!"]


async def test_execute_streaming_fallback_on_error() -> None:
    svc = _make_service()

    mock_response = CLIResponse(result="Fallback result", session_id="sess-2")
    with patch("ductor_bot.cli.service.create_cli") as mock_create:
        mock_cli = MagicMock()
        mock_cli.send_streaming = MagicMock(side_effect=RuntimeError("Stream broken"))
        mock_cli.send = AsyncMock(return_value=mock_response)
        mock_create.return_value = mock_cli

        resp = await svc.execute_streaming(AgentRequest(prompt="hello", chat_id=1))

    assert resp.stream_fallback is True
    assert resp.result == "Fallback result"


def test_update_default_model() -> None:
    svc = _make_service()
    svc.update_default_model("sonnet")
    assert svc._config.default_model == "sonnet"


def test_update_available_providers() -> None:
    svc = _make_service()
    svc.update_available_providers(frozenset({"claude", "codex"}))
    assert svc._available_providers == frozenset({"claude", "codex"})
