"""Tests for the interactive model selector wizard."""

from __future__ import annotations

import json
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ductor_bot.cli.auth import AuthResult, AuthStatus
from ductor_bot.cli.codex_cache import CodexModelCache
from ductor_bot.cli.codex_discovery import CodexModelInfo
from ductor_bot.config import reset_gemini_models, set_gemini_models
from ductor_bot.orchestrator.core import Orchestrator
from ductor_bot.orchestrator.selectors.model_selector import (
    handle_model_callback,
    is_model_selector_callback,
    model_selector_start,
    switch_model,
)
from ductor_bot.session.key import SessionKey

_AUTHED_CLAUDE = AuthResult("claude", AuthStatus.AUTHENTICATED)
_AUTHED_CODEX = AuthResult("codex", AuthStatus.AUTHENTICATED)
_AUTHED_GEMINI = AuthResult("gemini", AuthStatus.AUTHENTICATED)
_AUTHED_ANTIGRAVITY = AuthResult("antigravity", AuthStatus.AUTHENTICATED)
_NOT_FOUND_CLAUDE = AuthResult("claude", AuthStatus.NOT_FOUND)
_NOT_FOUND_CODEX = AuthResult("codex", AuthStatus.NOT_FOUND)
_NOT_FOUND_GEMINI = AuthResult("gemini", AuthStatus.NOT_FOUND)

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
        "ductor_bot.orchestrator.selectors.model_selector.check_all_auth",
        return_value=auth_map,
    )


@pytest.fixture(autouse=True)
def _reset_gemini_models() -> Any:
    reset_gemini_models()
    yield
    reset_gemini_models()


@contextmanager
def _with_codex_cache(orch: Orchestrator, models: list[CodexModelInfo] | None = None) -> Any:
    """Set up a mock codex_cache_obs on the observer manager."""
    cache = CodexModelCache(
        last_updated=datetime.now(UTC).isoformat(),
        models=models if models is not None else _CODEX_MODELS,
    )
    mock_observer = MagicMock()
    mock_observer.get_cache = MagicMock(return_value=cache)
    old = getattr(orch._observers, "codex_cache_obs", None)
    orch._observers.codex_cache_obs = mock_observer
    try:
        yield
    finally:
        orch._observers.codex_cache_obs = old


# -- is_model_selector_callback --


def test_prefix_detection() -> None:
    assert is_model_selector_callback("ms:p:claude") is True
    assert is_model_selector_callback("ms:m:opus") is True
    assert is_model_selector_callback("other") is False
    assert is_model_selector_callback("") is False


# -- model_selector_start --


async def test_start_no_providers(orch: Orchestrator) -> None:
    with _patch_auth(
        {"claude": _NOT_FOUND_CLAUDE, "codex": _NOT_FOUND_CODEX, "gemini": _NOT_FOUND_GEMINI}
    ):
        resp = await model_selector_start(orch, SessionKey(chat_id=1))
    assert "No authenticated providers" in resp.text
    assert resp.buttons is None


async def test_start_one_provider_claude(orch: Orchestrator) -> None:
    with _patch_auth(
        {"claude": _AUTHED_CLAUDE, "codex": _NOT_FOUND_CODEX, "gemini": _NOT_FOUND_GEMINI}
    ):
        resp = await model_selector_start(orch, SessionKey(chat_id=1))
    assert "Select Claude model" in resp.text
    assert resp.buttons is not None
    labels = [btn.text for row in resp.buttons.rows for btn in row]
    assert "HAIKU" in labels
    assert "SONNET" in labels
    assert "OPUS" in labels


async def test_start_one_provider_claude_includes_1m_variants(orch: Orchestrator) -> None:
    """/model selector surfaces SONNET[1M] + OPUS[1M] buttons for Claude (#76)."""
    with _patch_auth(
        {"claude": _AUTHED_CLAUDE, "codex": _NOT_FOUND_CODEX, "gemini": _NOT_FOUND_GEMINI}
    ):
        resp = await model_selector_start(orch, SessionKey(chat_id=1))
    assert resp.buttons is not None
    labels = [btn.text for row in resp.buttons.rows for btn in row]
    callbacks = [btn.callback_data for row in resp.buttons.rows for btn in row]
    assert "SONNET[1M]" in labels
    assert "OPUS[1M]" in labels
    assert "ms:m:opus[1m]" in callbacks
    assert "ms:m:sonnet[1m]" in callbacks


async def test_start_one_provider_codex(orch: Orchestrator) -> None:
    with (
        _patch_auth(
            {"claude": _NOT_FOUND_CLAUDE, "codex": _AUTHED_CODEX, "gemini": _NOT_FOUND_GEMINI}
        ),
        _with_codex_cache(orch),
    ):
        resp = await model_selector_start(orch, SessionKey(chat_id=1))
    assert "Select Codex model" in resp.text
    assert resp.buttons is not None


async def test_start_shows_configured_model_without_runtime_fallback(orch: Orchestrator) -> None:
    orch._providers._available_providers = frozenset({"codex"})
    with (
        _patch_auth(
            {"claude": _NOT_FOUND_CLAUDE, "codex": _AUTHED_CODEX, "gemini": _NOT_FOUND_GEMINI}
        ),
        _with_codex_cache(orch),
    ):
        resp = await model_selector_start(orch, SessionKey(chat_id=1))
    assert resp.buttons is not None
    assert "Current: opus" in resp.text
    assert "Configured default:" not in resp.text


async def test_start_two_providers(orch: Orchestrator) -> None:
    with _patch_auth(
        {"claude": _AUTHED_CLAUDE, "codex": _AUTHED_CODEX, "gemini": _NOT_FOUND_GEMINI}
    ):
        resp = await model_selector_start(orch, SessionKey(chat_id=1))
    assert "Model Selector" in resp.text
    assert resp.buttons is not None
    labels = [btn.text for row in resp.buttons.rows for btn in row]
    assert "CLAUDE" in labels
    assert "CODEX" in labels


async def test_start_one_provider_gemini_uses_discovered_models(orch: Orchestrator) -> None:
    set_gemini_models(
        frozenset(
            {
                "gemini-2.5-pro",
                "gemini-2.5-flash",
                "gemini-3-pro-preview",
            }
        )
    )
    with _patch_auth(
        {"claude": _NOT_FOUND_CLAUDE, "codex": _NOT_FOUND_CODEX, "gemini": _AUTHED_GEMINI}
    ):
        resp = await model_selector_start(orch, SessionKey(chat_id=1))
    assert "Select Gemini model" in resp.text
    assert resp.buttons is not None
    labels = [btn.text for row in resp.buttons.rows for btn in row]
    assert "2.5-pro" in labels
    assert "2.5-flash" in labels
    assert "3-pro-preview" in labels


async def test_start_one_provider_gemini_includes_builtin_aliases(orch: Orchestrator) -> None:
    set_gemini_models(frozenset({"gemini-2.5-pro", "gemini-2.5-flash"}))
    with _patch_auth(
        {"claude": _NOT_FOUND_CLAUDE, "codex": _NOT_FOUND_CODEX, "gemini": _AUTHED_GEMINI}
    ):
        resp = await model_selector_start(orch, SessionKey(chat_id=1))
    assert resp.buttons is not None
    labels = [btn.text for row in resp.buttons.rows for btn in row]
    # Built-in CLI aliases come first so the user can pick auto-routing without
    # pinning a specific model version.
    for alias in ("auto", "pro", "flash", "flash-lite"):
        assert alias in labels
    assert labels.index("auto") < labels.index("2.5-pro")


async def test_start_one_provider_antigravity(orch: Orchestrator) -> None:
    with _patch_auth(
        {
            "claude": _NOT_FOUND_CLAUDE,
            "codex": _NOT_FOUND_CODEX,
            "gemini": _NOT_FOUND_GEMINI,
            "antigravity": _AUTHED_ANTIGRAVITY,
        }
    ):
        resp = await model_selector_start(orch, SessionKey(chat_id=1))
    assert "Select Antigravity model" in resp.text
    assert resp.buttons is not None
    labels = [btn.text for row in resp.buttons.rows for btn in row]
    assert "antigravity-default" in labels


# -- handle_model_callback: provider selection --


async def test_callback_provider_claude(orch: Orchestrator) -> None:
    resp = await handle_model_callback(orch, SessionKey(chat_id=1), "ms:p:claude")
    assert "Select Claude model" in resp.text
    assert resp.buttons is not None
    labels = [btn.text for row in resp.buttons.rows for btn in row]
    assert "OPUS" in labels
    assert "<< Back" in labels


async def test_callback_provider_codex(orch: Orchestrator) -> None:
    with _with_codex_cache(orch):
        resp = await handle_model_callback(orch, SessionKey(chat_id=1), "ms:p:codex")
    assert "Select Codex model" in resp.text
    assert resp.buttons is not None
    labels = [btn.text for row in resp.buttons.rows for btn in row]
    assert "gpt-5.2-codex" in labels


async def test_callback_provider_codex_fallback(orch: Orchestrator) -> None:
    with _with_codex_cache(orch, models=[]):
        resp = await handle_model_callback(orch, SessionKey(chat_id=1), "ms:p:codex")
    assert resp.buttons is not None
    labels = [btn.text for row in resp.buttons.rows for btn in row]
    assert any("o3" in label.lower() for label in labels) or "<< Back" in labels


async def test_callback_provider_antigravity(orch: Orchestrator) -> None:
    resp = await handle_model_callback(orch, SessionKey(chat_id=1), "ms:p:antigravity")
    assert "Select Antigravity model" in resp.text
    assert resp.buttons is not None
    labels = [btn.text for row in resp.buttons.rows for btn in row]
    assert "antigravity-default" in labels


# -- handle_model_callback: model selection --


async def test_callback_model_claude_shows_reasoning(orch: Orchestrator) -> None:
    """Picking a Claude model offers the effort sub-selector (incl. max)."""
    resp = await handle_model_callback(orch, SessionKey(chat_id=1), "ms:m:sonnet")
    assert "Thinking level" in resp.text
    assert resp.buttons is not None
    labels = [btn.text for row in resp.buttons.rows for btn in row]
    assert "Low" in labels
    assert "Max" in labels  # Claude-only top level
    callbacks = [btn.callback_data for row in resp.buttons.rows for btn in row]
    assert "ms:r:max:sonnet" in callbacks
    assert "ms:b:claude" in callbacks  # back to the Claude model list
    # Model is not switched until an effort is chosen.
    assert orch._config.model == "opus"


async def test_callback_claude_reasoning_applies_via_picker(orch: Orchestrator) -> None:
    """Selecting a Claude effort in the picker applies it via the shared path."""
    object.__setattr__(orch._process_registry, "kill_all", AsyncMock(return_value=0))
    # Step 1: pick the claude model -> effort sub-selector.
    await handle_model_callback(orch, SessionKey(chat_id=1), "ms:m:sonnet")
    # Step 2: pick an effort -> same ms:r path codex/_effort use.
    resp = await handle_model_callback(orch, SessionKey(chat_id=1), "ms:r:max:sonnet")
    assert resp.buttons is None
    assert orch._config.model == "sonnet"
    assert orch._config.reasoning_effort == "max"


async def test_callback_model_antigravity_switches_without_reasoning_step(
    orch: Orchestrator,
) -> None:
    object.__setattr__(orch._process_registry, "kill_all", AsyncMock(return_value=0))
    resp = await handle_model_callback(orch, SessionKey(chat_id=1), "ms:m:antigravity-default")
    assert "antigravity-default" in resp.text
    assert "Thinking level" not in resp.text
    assert resp.buttons is None
    assert orch._config.model == "antigravity-default"
    assert orch._config.provider == "antigravity"


async def test_callback_model_codex_shows_reasoning(orch: Orchestrator) -> None:
    with _with_codex_cache(orch):
        resp = await handle_model_callback(orch, SessionKey(chat_id=1), "ms:m:gpt-5.2-codex")
    assert "Thinking level" in resp.text
    assert resp.buttons is not None
    labels = [btn.text for row in resp.buttons.rows for btn in row]
    assert "Low" in labels
    assert "High" in labels
    assert "XHigh" in labels


async def test_callback_model_codex_mini_limited_efforts(orch: Orchestrator) -> None:
    with _with_codex_cache(orch):
        resp = await handle_model_callback(orch, SessionKey(chat_id=1), "ms:m:gpt-5.1-codex-mini")
    assert resp.buttons is not None
    labels = [btn.text for row in resp.buttons.rows for btn in row]
    assert "Medium" in labels
    assert "High" in labels
    assert "Low" not in labels
    assert "XHigh" not in labels


# -- handle_model_callback: reasoning selection --


async def test_callback_reasoning_switches(orch: Orchestrator) -> None:
    object.__setattr__(orch._process_registry, "kill_all", AsyncMock(return_value=0))
    resp = await handle_model_callback(orch, SessionKey(chat_id=1), "ms:r:high:gpt-5.2-codex")
    assert "gpt-5.2-codex" in resp.text
    assert "high" in resp.text.lower()
    assert resp.buttons is None


# -- handle_model_callback: back navigation --


async def test_callback_back_root(orch: Orchestrator) -> None:
    with _patch_auth(
        {"claude": _AUTHED_CLAUDE, "codex": _AUTHED_CODEX, "gemini": _NOT_FOUND_GEMINI}
    ):
        resp = await handle_model_callback(orch, SessionKey(chat_id=1), "ms:b:root")
    assert resp.buttons is not None
    labels = [btn.text for row in resp.buttons.rows for btn in row]
    assert "CLAUDE" in labels


async def test_callback_back_provider(orch: Orchestrator) -> None:
    resp = await handle_model_callback(orch, SessionKey(chat_id=1), "ms:b:claude")
    assert "Select Claude model" in resp.text


# -- switch_model --


async def test_switch_model_basic(orch: Orchestrator) -> None:
    mock_kill = AsyncMock(return_value=0)
    mock_reset = AsyncMock()
    object.__setattr__(orch._process_registry, "kill_all", mock_kill)
    object.__setattr__(orch._sessions, "reset_provider_session", mock_reset)
    result = await switch_model(orch, SessionKey(chat_id=1), "sonnet")
    assert "opus" in result
    assert "sonnet" in result
    assert "Session reset" not in result
    assert "Resuming session" not in result
    assert orch._config.model == "sonnet"
    mock_kill.assert_called_once_with(1)
    mock_reset.assert_not_called()


async def test_switch_model_opus_1m_persists(orch: Orchestrator) -> None:
    """opus[1m] is a valid Claude alias; switch_model persists it to config (#76)."""
    object.__setattr__(orch._process_registry, "kill_all", AsyncMock(return_value=0))
    result = await switch_model(orch, SessionKey(chat_id=1), "opus[1m]")
    assert "opus[1m]" in result
    assert orch._config.model == "opus[1m]"
    saved = json.loads(orch.paths.config_path.read_text(encoding="utf-8"))
    assert saved["model"] == "opus[1m]"
    assert saved["provider"] == "claude"


async def test_switch_model_already_set(orch: Orchestrator) -> None:
    result = await switch_model(orch, SessionKey(chat_id=1), "opus")
    assert "Already running" in result


async def test_switch_model_with_reasoning_effort(orch: Orchestrator) -> None:
    object.__setattr__(orch._process_registry, "kill_all", AsyncMock(return_value=0))
    result = await switch_model(orch, SessionKey(chat_id=1), "sonnet", reasoning_effort="high")
    assert "high" in result.lower()
    assert orch._config.reasoning_effort == "high"
    saved = json.loads(orch.paths.config_path.read_text(encoding="utf-8"))
    assert saved["reasoning_effort"] == "high"


async def test_switch_model_persists_to_config(orch: Orchestrator) -> None:
    object.__setattr__(orch._process_registry, "kill_all", AsyncMock(return_value=0))
    await switch_model(orch, SessionKey(chat_id=1), "sonnet")
    saved = json.loads(orch.paths.config_path.read_text(encoding="utf-8"))
    assert saved["model"] == "sonnet"


async def test_switch_model_provider_change(orch: Orchestrator) -> None:
    mock_reset = AsyncMock()
    object.__setattr__(orch._process_registry, "kill_all", AsyncMock(return_value=0))
    object.__setattr__(orch._sessions, "reset_provider_session", mock_reset)
    result = await switch_model(orch, SessionKey(chat_id=1), "o3")
    assert "Provider:" in result
    assert orch._config.provider == "codex"
    mock_reset.assert_not_called()


async def test_switch_model_shows_resume_hint_same_provider(orch: Orchestrator) -> None:
    session, _ = await orch._sessions.resolve_session(
        SessionKey(chat_id=1), provider="claude", model="opus"
    )
    session.session_id = "claude-abc123"
    await orch._sessions.update_session(session)

    object.__setattr__(orch._process_registry, "kill_all", AsyncMock(return_value=0))
    result = await switch_model(orch, SessionKey(chat_id=1), "sonnet")

    assert "Resuming session `claude-abc123`." in result
    assert "You have already sent 1 message in this provider session." in result
    assert "Current model: `sonnet`." in result
    assert "Use /new to start a fresh session." in result


async def test_switch_model_shows_resume_hint_provider_change(orch: Orchestrator) -> None:
    session, _ = await orch._sessions.resolve_session(
        SessionKey(chat_id=1), provider="codex", model="gpt-5.2-codex"
    )
    session.session_id = "codex-xyz789"
    await orch._sessions.update_session(session)

    object.__setattr__(orch._process_registry, "kill_all", AsyncMock(return_value=0))
    result = await switch_model(orch, SessionKey(chat_id=1), "o3")

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
    result = await switch_model(orch, SessionKey(chat_id=1), "opus", reasoning_effort="high")
    assert "Reasoning effort updated" in result
    mock_kill.assert_not_called()
    mock_reset.assert_not_called()


async def test_switch_model_rejects_invalid_codex_reasoning_effort(orch: Orchestrator) -> None:
    from unittest.mock import MagicMock

    from ductor_bot.cli.codex_cache import CodexModelCache
    from ductor_bot.cli.codex_discovery import CodexModelInfo

    object.__setattr__(orch._process_registry, "kill_all", AsyncMock(return_value=0))
    orch._observers.codex_cache_obs = MagicMock(
        get_cache=MagicMock(
            return_value=CodexModelCache(
                last_updated="2026-04-23T12:00:00",
                models=[
                    CodexModelInfo(
                        id="gpt-4o-mini",
                        display_name="GPT-4o Mini",
                        description="mini",
                        supported_efforts=(),
                        default_effort="",
                        is_default=False,
                    )
                ],
            )
        )
    )

    result = await switch_model(
        orch,
        SessionKey(chat_id=1),
        "gpt-4o-mini",
        reasoning_effort="high",
    )

    assert "Invalid reasoning effort" in result
    assert "gpt-4o-mini" in result


# -- Claude effort + provider-aware validation ------------------------------


async def test_switch_model_claude_accepts_max(orch: Orchestrator) -> None:
    object.__setattr__(orch._process_registry, "kill_all", AsyncMock(return_value=0))
    result = await switch_model(orch, SessionKey(chat_id=1), "opus", reasoning_effort="max")
    assert "Invalid reasoning effort" not in result
    assert orch._config.reasoning_effort == "max"


async def test_switch_model_codex_rejects_max_with_cache(orch: Orchestrator) -> None:
    object.__setattr__(orch._process_registry, "kill_all", AsyncMock(return_value=0))
    with _with_codex_cache(orch):
        result = await switch_model(
            orch, SessionKey(chat_id=1), "gpt-5.2-codex", reasoning_effort="max"
        )
    assert "Invalid reasoning effort" in result
    assert "max" in result


async def test_switch_model_codex_rejects_max_no_cache(orch: Orchestrator) -> None:
    """Even without a Codex cache, the fallback set rejects ``max``."""
    object.__setattr__(orch._process_registry, "kill_all", AsyncMock(return_value=0))
    orch._observers.codex_cache_obs = None
    result = await switch_model(
        orch, SessionKey(chat_id=1), "gpt-5.2-codex", reasoning_effort="max"
    )
    assert "Invalid reasoning effort" in result


async def test_provider_switch_resets_invalid_effort_to_medium(orch: Orchestrator) -> None:
    """Claude+max then /model to Codex must reset effort to medium (max not sent)."""
    object.__setattr__(orch._process_registry, "kill_all", AsyncMock(return_value=0))
    await switch_model(orch, SessionKey(chat_id=1), "opus", reasoning_effort="max")
    assert orch._config.reasoning_effort == "max"

    orch._observers.codex_cache_obs = None  # exercise the fallback path
    await switch_model(orch, SessionKey(chat_id=1), "gpt-5.2-codex")
    assert orch._config.reasoning_effort == "medium"
    saved = json.loads(orch.paths.config_path.read_text(encoding="utf-8"))
    assert saved["reasoning_effort"] == "medium"


async def test_provider_switch_keeps_valid_effort(orch: Orchestrator) -> None:
    """A carried-over effort valid for the new provider is left untouched."""
    object.__setattr__(orch._process_registry, "kill_all", AsyncMock(return_value=0))
    await switch_model(orch, SessionKey(chat_id=1), "opus", reasoning_effort="high")
    orch._observers.codex_cache_obs = None
    await switch_model(orch, SessionKey(chat_id=1), "gpt-5.2-codex")
    assert orch._config.reasoning_effort == "high"


# -- /effort selector -------------------------------------------------------


async def test_effort_selector_claude_shows_max(orch: Orchestrator) -> None:
    from ductor_bot.orchestrator.selectors.model_selector import effort_selector_start

    resp = await effort_selector_start(orch, SessionKey(chat_id=1))  # default model: opus (claude)
    assert resp.buttons is not None
    labels = [b.text for row in resp.buttons.rows for b in row]
    assert "Max" in labels
    callbacks = [b.callback_data for row in resp.buttons.rows for b in row]
    assert any(c.startswith("ms:r:max:") for c in callbacks)


async def test_effort_selector_codex_no_max(orch: Orchestrator) -> None:
    from ductor_bot.orchestrator.selectors.model_selector import effort_selector_start

    object.__setattr__(orch._process_registry, "kill_all", AsyncMock(return_value=0))
    await switch_model(orch, SessionKey(chat_id=1), "gpt-5.2-codex")
    orch._observers.codex_cache_obs = None
    resp = await effort_selector_start(orch, SessionKey(chat_id=1))
    assert resp.buttons is not None
    labels = [b.text for row in resp.buttons.rows for b in row]
    assert "Max" not in labels


async def test_effort_selector_unsupported_provider_info_only(orch: Orchestrator) -> None:
    from ductor_bot.orchestrator.selectors.model_selector import effort_selector_start

    object.__setattr__(orch._process_registry, "kill_all", AsyncMock(return_value=0))
    await switch_model(orch, SessionKey(chat_id=1), "gemini-2.5-pro")
    resp = await effort_selector_start(orch, SessionKey(chat_id=1))
    assert resp.buttons is None  # info message only, no UI
    assert "gemini" in resp.text.lower()


# -- topic-session effort apply (Gate D) ------------------------------------


async def test_topic_effort_change_applies_runtime(orch: Orchestrator) -> None:
    """`/effort` (effort_only) in a TOPIC must update the runtime effort.

    Effort has no per-session field, so it lives in the service config; a topic
    change must still reach orch._config / CLIService (it previously no-op'd).
    """
    key = SessionKey(chat_id=1, topic_id=7)
    await orch._sessions.resolve_session(key, provider="claude", model="opus")
    object.__setattr__(orch._process_registry, "kill_all", AsyncMock(return_value=0))
    update_mock = MagicMock()
    object.__setattr__(orch._cli_service, "update_reasoning_effort", update_mock)

    await switch_model(orch, key, "opus", reasoning_effort="high")

    assert orch._config.reasoning_effort == "high"
    update_mock.assert_called_once_with("high")


async def test_topic_provider_switch_resets_invalid_effort(orch: Orchestrator) -> None:
    """Topic claude+max -> codex must reset effort to medium (not leave max)."""
    key = SessionKey(chat_id=1, topic_id=7)
    await orch._sessions.resolve_session(key, provider="claude", model="opus")
    object.__setattr__(orch._process_registry, "kill_all", AsyncMock(return_value=0))

    # Set effort to max on the claude topic session first.
    await switch_model(orch, key, "opus", reasoning_effort="max")
    assert orch._config.reasoning_effort == "max"

    orch._observers.codex_cache_obs = None  # exercise the fallback
    await switch_model(orch, key, "gpt-5.2-codex")
    assert orch._config.reasoning_effort == "medium"
