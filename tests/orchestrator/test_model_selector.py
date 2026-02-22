"""Tests for the interactive model selector wizard."""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from ductor_bot.cli.auth import AuthResult, AuthStatus
from ductor_bot.cli.codex_cache import CodexModelCache
from ductor_bot.cli.codex_discovery import CodexModelInfo
from ductor_bot.orchestrator.core import Orchestrator
from ductor_bot.orchestrator.model_selector import (
    handle_model_callback,
    is_model_selector_callback,
    model_selector_start,
    switch_model,
)

_AUTHED_CLAUDE = AuthResult("claude", AuthStatus.AUTHENTICATED)
_AUTHED_CODEX = AuthResult("codex", AuthStatus.AUTHENTICATED)
_NOT_FOUND_CLAUDE = AuthResult("claude", AuthStatus.NOT_FOUND)
_NOT_FOUND_CODEX = AuthResult("codex", AuthStatus.NOT_FOUND)

_CODEX_MODELS = [
    CodexModelInfo(
        id="gpt-5.2-codex",
        display_name="gpt-5.2-codex",
        description="Frontier",
        supported_efforts=("low", "medium", "high", "xhigh"),
        default_effort="medium",
        is_default=True,
    ),
    CodexModelInfo(
        id="gpt-5.1-codex-mini",
        display_name="gpt-5.1-codex-mini",
        description="Mini",
        supported_efforts=("medium", "high"),
        default_effort="medium",
        is_default=False,
    ),
]


def _patch_auth(auth_map: dict[str, AuthResult]) -> Any:
    return patch(
        "ductor_bot.orchestrator.model_selector.check_all_auth",
        return_value=auth_map,
    )


@contextmanager
def _with_codex_cache(orch: Orchestrator, models: list[CodexModelInfo] | None = None) -> Any:
    """Set up a mock _codex_cache_observer with the given models."""
    cache = CodexModelCache(
        last_updated=datetime.now(UTC).isoformat(),
        models=models if models is not None else _CODEX_MODELS,
    )
    mock_observer = MagicMock()
    mock_observer.get_cache = MagicMock(return_value=cache)
    old = getattr(orch, "_codex_cache_observer", None)
    object.__setattr__(orch, "_codex_cache_observer", mock_observer)
    try:
        yield
    finally:
        object.__setattr__(orch, "_codex_cache_observer", old)


# -- is_model_selector_callback --


def test_prefix_detection() -> None:
    assert is_model_selector_callback("ms:p:claude") is True
    assert is_model_selector_callback("ms:m:opus") is True
    assert is_model_selector_callback("other") is False
    assert is_model_selector_callback("") is False


# -- model_selector_start --


async def test_start_no_providers(orch: Orchestrator) -> None:
    with _patch_auth({"claude": _NOT_FOUND_CLAUDE, "codex": _NOT_FOUND_CODEX}):
        text, keyboard = await model_selector_start(orch, 1)
    assert "No authenticated providers" in text
    assert keyboard is None


async def test_start_one_provider_claude(orch: Orchestrator) -> None:
    with _patch_auth({"claude": _AUTHED_CLAUDE, "codex": _NOT_FOUND_CODEX}):
        text, keyboard = await model_selector_start(orch, 1)
    assert "Select Claude model" in text
    assert keyboard is not None
    labels = [btn.text for row in keyboard.inline_keyboard for btn in row]
    assert "HAIKU" in labels
    assert "SONNET" in labels
    assert "OPUS" in labels


async def test_start_one_provider_codex(orch: Orchestrator) -> None:
    with (
        _patch_auth({"claude": _NOT_FOUND_CLAUDE, "codex": _AUTHED_CODEX}),
        _with_codex_cache(orch),
    ):
        text, keyboard = await model_selector_start(orch, 1)
    assert "Select Codex model" in text
    assert keyboard is not None


async def test_start_shows_effective_model_when_fallback_is_active(orch: Orchestrator) -> None:
    object.__setattr__(orch, "_available_providers", frozenset({"codex"}))
    with (
        _patch_auth({"claude": _NOT_FOUND_CLAUDE, "codex": _AUTHED_CODEX}),
        _with_codex_cache(orch),
    ):
        text, keyboard = await model_selector_start(orch, 1)
    assert keyboard is not None
    assert "Current: gpt-5.2-codex" in text
    assert "Configured default: opus" in text


async def test_start_two_providers(orch: Orchestrator) -> None:
    with _patch_auth({"claude": _AUTHED_CLAUDE, "codex": _AUTHED_CODEX}):
        text, keyboard = await model_selector_start(orch, 1)
    assert "Model Selector" in text
    assert keyboard is not None
    labels = [btn.text for row in keyboard.inline_keyboard for btn in row]
    assert "CLAUDE" in labels
    assert "CODEX" in labels


# -- handle_model_callback: provider selection --


async def test_callback_provider_claude(orch: Orchestrator) -> None:
    text, keyboard = await handle_model_callback(orch, 1, "ms:p:claude")
    assert "Select Claude model" in text
    assert keyboard is not None
    labels = [btn.text for row in keyboard.inline_keyboard for btn in row]
    assert "OPUS" in labels
    assert "<< Back" in labels


async def test_callback_provider_codex(orch: Orchestrator) -> None:
    with _with_codex_cache(orch):
        text, keyboard = await handle_model_callback(orch, 1, "ms:p:codex")
    assert "Select Codex model" in text
    assert keyboard is not None
    labels = [btn.text for row in keyboard.inline_keyboard for btn in row]
    assert "gpt-5.2-codex" in labels


async def test_callback_provider_codex_fallback(orch: Orchestrator) -> None:
    with _with_codex_cache(orch, models=[]):
        _text, keyboard = await handle_model_callback(orch, 1, "ms:p:codex")
    assert keyboard is not None
    labels = [btn.text for row in keyboard.inline_keyboard for btn in row]
    assert any("o3" in label.lower() for label in labels) or "<< Back" in labels


# -- handle_model_callback: model selection --


async def test_callback_model_claude_switches(orch: Orchestrator) -> None:
    object.__setattr__(orch._process_registry, "kill_all", AsyncMock(return_value=0))
    text, keyboard = await handle_model_callback(orch, 1, "ms:m:sonnet")
    assert "sonnet" in text
    assert keyboard is None
    assert orch._config.model == "sonnet"


async def test_callback_model_codex_shows_reasoning(orch: Orchestrator) -> None:
    with _with_codex_cache(orch):
        text, keyboard = await handle_model_callback(orch, 1, "ms:m:gpt-5.2-codex")
    assert "Thinking level" in text
    assert keyboard is not None
    labels = [btn.text for row in keyboard.inline_keyboard for btn in row]
    assert "Low" in labels
    assert "High" in labels
    assert "XHigh" in labels


async def test_callback_model_codex_mini_limited_efforts(orch: Orchestrator) -> None:
    with _with_codex_cache(orch):
        _text, keyboard = await handle_model_callback(orch, 1, "ms:m:gpt-5.1-codex-mini")
    assert keyboard is not None
    labels = [btn.text for row in keyboard.inline_keyboard for btn in row]
    assert "Medium" in labels
    assert "High" in labels
    assert "Low" not in labels
    assert "XHigh" not in labels


# -- handle_model_callback: reasoning selection --


async def test_callback_reasoning_switches(orch: Orchestrator) -> None:
    object.__setattr__(orch._process_registry, "kill_all", AsyncMock(return_value=0))
    text, keyboard = await handle_model_callback(orch, 1, "ms:r:high:gpt-5.2-codex")
    assert "gpt-5.2-codex" in text
    assert "high" in text.lower()
    assert keyboard is None


# -- handle_model_callback: back navigation --


async def test_callback_back_root(orch: Orchestrator) -> None:
    with _patch_auth({"claude": _AUTHED_CLAUDE, "codex": _AUTHED_CODEX}):
        _text, keyboard = await handle_model_callback(orch, 1, "ms:b:root")
    assert keyboard is not None
    labels = [btn.text for row in keyboard.inline_keyboard for btn in row]
    assert "CLAUDE" in labels


async def test_callback_back_provider(orch: Orchestrator) -> None:
    text, _keyboard = await handle_model_callback(orch, 1, "ms:b:claude")
    assert "Select Claude model" in text


# -- switch_model --


async def test_switch_model_basic(orch: Orchestrator) -> None:
    mock_kill = AsyncMock(return_value=0)
    mock_reset = AsyncMock()
    object.__setattr__(orch._process_registry, "kill_all", mock_kill)
    object.__setattr__(orch._sessions, "reset_provider_session", mock_reset)
    result = await switch_model(orch, 1, "sonnet")
    assert "opus" in result
    assert "sonnet" in result
    assert "Session reset" not in result
    assert "Resuming session" not in result
    assert orch._config.model == "sonnet"
    mock_kill.assert_called_once_with(1)
    mock_reset.assert_not_called()


async def test_switch_model_already_set(orch: Orchestrator) -> None:
    result = await switch_model(orch, 1, "opus")
    assert "Already running" in result


async def test_switch_model_with_reasoning_effort(orch: Orchestrator) -> None:
    object.__setattr__(orch._process_registry, "kill_all", AsyncMock(return_value=0))
    result = await switch_model(orch, 1, "sonnet", reasoning_effort="high")
    assert "high" in result.lower()
    assert orch._config.reasoning_effort == "high"
    saved = json.loads(orch.paths.config_path.read_text(encoding="utf-8"))
    assert saved["reasoning_effort"] == "high"


async def test_switch_model_persists_to_config(orch: Orchestrator) -> None:
    object.__setattr__(orch._process_registry, "kill_all", AsyncMock(return_value=0))
    await switch_model(orch, 1, "sonnet")
    saved = json.loads(orch.paths.config_path.read_text(encoding="utf-8"))
    assert saved["model"] == "sonnet"


async def test_switch_model_provider_change(orch: Orchestrator) -> None:
    mock_reset = AsyncMock()
    object.__setattr__(orch._process_registry, "kill_all", AsyncMock(return_value=0))
    object.__setattr__(orch._sessions, "reset_provider_session", mock_reset)
    result = await switch_model(orch, 1, "o3")
    assert "Provider:" in result
    assert orch._config.provider == "codex"
    mock_reset.assert_not_called()


async def test_switch_model_shows_resume_hint_same_provider(orch: Orchestrator) -> None:
    session, _ = await orch._sessions.resolve_session(1, provider="claude", model="opus")
    session.session_id = "claude-abc123"
    await orch._sessions.update_session(session)

    object.__setattr__(orch._process_registry, "kill_all", AsyncMock(return_value=0))
    result = await switch_model(orch, 1, "sonnet")

    assert "Resuming session `claude-abc123`." in result
    assert "You have already sent 1 message in this provider session." in result
    assert "Current model: `sonnet`." in result
    assert "Use /new to start a fresh session." in result


async def test_switch_model_shows_resume_hint_provider_change(orch: Orchestrator) -> None:
    session, _ = await orch._sessions.resolve_session(1, provider="codex", model="gpt-5.2-codex")
    session.session_id = "codex-xyz789"
    await orch._sessions.update_session(session)

    object.__setattr__(orch._process_registry, "kill_all", AsyncMock(return_value=0))
    result = await switch_model(orch, 1, "o3")

    assert "Resuming session `codex-xyz789`." in result
    assert "You have already sent 1 message in this provider session." in result
    assert "Current model: `o3`." in result
    assert "Use /new to start a fresh session." in result


async def test_switch_reasoning_only(orch: Orchestrator) -> None:
    """Changing only reasoning effort does not reset session."""
    mock_kill = AsyncMock(return_value=0)
    mock_reset = AsyncMock()
    object.__setattr__(orch._process_registry, "kill_all", mock_kill)
    object.__setattr__(orch._sessions, "reset_provider_session", mock_reset)
    result = await switch_model(orch, 1, "opus", reasoning_effort="high")
    assert "Reasoning effort updated" in result
    mock_kill.assert_not_called()
    mock_reset.assert_not_called()
