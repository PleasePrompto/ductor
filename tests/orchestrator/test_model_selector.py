"""Tests for the interactive model selector wizard."""

from __future__ import annotations

import copy
import json
from contextlib import contextmanager
from datetime import UTC, datetime
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ductor_bot.cli.auth import AuthResult, AuthStatus
from ductor_bot.cli.codex_cache import CodexModelCache
from ductor_bot.cli.codex_discovery import CodexModelInfo
from ductor_bot.cli.types import AgentResponse
from ductor_bot.config import reset_gemini_models, set_gemini_models
from ductor_bot.orchestrator.core import Orchestrator
from ductor_bot.orchestrator.flows import normal
from ductor_bot.orchestrator.selectors.model_selector import (
    handle_model_callback,
    is_model_selector_callback,
    model_selector_start,
    switch_model,
)
from ductor_bot.session.key import SessionKey
from ductor_bot.session.manager import ProviderSessionData

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
    object.__setattr__(orch._process_registry, "kill_by_chat_topic", AsyncMock(return_value=0))
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
    object.__setattr__(orch._process_registry, "kill_by_chat_topic", AsyncMock(return_value=0))
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
    object.__setattr__(orch._process_registry, "kill_by_chat_topic", AsyncMock(return_value=0))
    resp = await handle_model_callback(orch, SessionKey(chat_id=1), "ms:r:high:gpt-5.2-codex")
    assert "gpt-5.2-codex" in resp.text
    assert "high" in resp.text.lower()
    assert resp.buttons is None


async def test_callback_reasoning_topic_without_session_targets_next_message(
    orch: Orchestrator,
) -> None:
    key = SessionKey(chat_id=-100, topic_id=42)
    object.__setattr__(orch._process_registry, "kill_by_chat_topic", AsyncMock(return_value=0))
    mock_execute = AsyncMock(
        return_value=AgentResponse(result="ok", session_id="codex-topic-session")
    )
    object.__setattr__(orch._cli_service, "execute", mock_execute)

    await handle_model_callback(orch, key, "ms:r:high:gpt-5.2-codex")
    await normal(orch, key, "hello", model_override=None)

    request = mock_execute.call_args.args[0]
    assert request.model_override == "gpt-5.2-codex"
    assert request.provider_override == "codex"


async def test_callback_reasoning_same_model_topic_without_session_persists_effort(
    orch: Orchestrator,
) -> None:
    orch._config.model = "opus"
    orch._config.provider = "claude"
    orch._config.reasoning_effort = "medium"
    main = SessionKey(chat_id=-100)
    await orch._sessions.resolve_session(
        main,
        provider="claude",
        model="opus",
        reasoning_effort="medium",
    )
    key = SessionKey(chat_id=-100, topic_id=44)
    mock_execute = AsyncMock(
        return_value=AgentResponse(result="ok", session_id="claude-topic-session")
    )
    object.__setattr__(orch._cli_service, "execute", mock_execute)

    await handle_model_callback(orch, key, "ms:r:xhigh:opus")

    topic = await orch._sessions.get_active(key)
    assert topic is not None
    assert topic.model == "opus"
    assert topic.reasoning_effort == "xhigh"
    assert orch._config.model == "opus"
    assert orch._config.reasoning_effort == "medium"
    main_after = await orch._sessions.get_active(main)
    assert main_after is not None
    assert main_after.reasoning_effort == "medium"

    await normal(orch, key, "hello", model_override=None)

    request = mock_execute.call_args.args[0]
    assert request.model_override == "opus"
    assert request.provider_override == "claude"
    assert request.effort_override == "xhigh"


async def test_callback_reasoning_stale_topic_targets_next_message(orch: Orchestrator) -> None:
    key = SessionKey(chat_id=-100, topic_id=43)
    orch._config.max_session_messages = 1
    stale, _ = await orch._sessions.resolve_session(key, provider="claude", model="opus")
    stale.session_id = "stale-claude-session"
    await orch._sessions.update_session(stale)

    object.__setattr__(orch._process_registry, "kill_by_chat_topic", AsyncMock(return_value=0))
    mock_execute = AsyncMock(
        return_value=AgentResponse(result="ok", session_id="fresh-codex-session")
    )
    object.__setattr__(orch._cli_service, "execute", mock_execute)

    await handle_model_callback(orch, key, "ms:r:high:gpt-5.2-codex")
    await normal(orch, key, "hello", model_override=None)

    request = mock_execute.call_args.args[0]
    assert request.model_override == "gpt-5.2-codex"
    assert request.provider_override == "codex"


async def test_callback_reasoning_stale_target_bucket_targets_next_message(
    orch: Orchestrator,
) -> None:
    key = SessionKey(chat_id=-100, topic_id=47)
    orch._config.max_session_messages = 3
    existing, _ = await orch._sessions.resolve_session(key, provider="claude", model="opus")
    existing.provider_sessions["claude"] = ProviderSessionData(
        session_id="fresh-claude-session",
        message_count=1,
    )
    existing.provider_sessions["codex"] = ProviderSessionData(
        session_id="stale-codex-session",
        message_count=4,
    )
    await orch._sessions.preserve_session_identity(existing)

    object.__setattr__(orch._process_registry, "kill_by_chat_topic", AsyncMock(return_value=0))
    mock_execute = AsyncMock(
        return_value=AgentResponse(result="ok", session_id="fresh-codex-session")
    )
    object.__setattr__(orch._cli_service, "execute", mock_execute)

    await handle_model_callback(orch, key, "ms:r:high:gpt-5.2-codex")
    replacement = await orch._sessions.get_active(key)
    assert replacement is not None
    # Only the maxed-out codex bucket is reset; the fresh claude history stays.
    assert "codex" not in replacement.provider_sessions
    assert replacement.provider_sessions["claude"].session_id == "fresh-claude-session"
    await normal(orch, key, "hello", model_override=None)

    request = mock_execute.call_args.args[0]
    assert request.model_override == "gpt-5.2-codex"
    assert request.provider_override == "codex"


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
    object.__setattr__(orch._process_registry, "kill_by_chat_topic", mock_kill)
    object.__setattr__(orch._sessions, "reset_provider_session", mock_reset)
    result = await switch_model(orch, SessionKey(chat_id=1), "sonnet")
    assert "opus" in result
    assert "sonnet" in result
    assert "Session reset" not in result
    assert "Resuming session" not in result
    assert orch._config.model == "sonnet"
    mock_kill.assert_called_once_with(1, None)
    mock_reset.assert_not_called()


async def test_switch_model_topic_does_not_change_global_defaults(orch: Orchestrator) -> None:
    key = SessionKey(chat_id=-100, topic_id=44)
    config_before = (
        orch._config.provider,
        orch._config.model,
        orch._config.reasoning_effort,
    )
    config_file_before = orch.paths.config_path.read_bytes()
    object.__setattr__(orch._process_registry, "kill_by_chat_topic", AsyncMock(return_value=0))

    await handle_model_callback(orch, key, "ms:r:high:gpt-5.2-codex")

    assert (
        orch._config.provider,
        orch._config.model,
        orch._config.reasoning_effort,
    ) == config_before
    assert orch.paths.config_path.read_bytes() == config_file_before
    orch._cli_service.update_default_model.assert_not_called()
    orch._cli_service.update_reasoning_effort.assert_not_called()


async def test_switch_model_dm_without_session_keeps_global_persistence(
    orch: Orchestrator,
) -> None:
    key = SessionKey(chat_id=1)
    object.__setattr__(orch._process_registry, "kill_by_chat_topic", AsyncMock(return_value=0))
    resolve_target = AsyncMock(wraps=orch._sessions.resolve_session_target)

    with patch.object(orch._sessions, "resolve_session_target", new=resolve_target):
        await switch_model(orch, key, "sonnet")

    resolve_target.assert_not_awaited()
    assert await orch._sessions.get_active(key) is None
    assert orch._config.model == "sonnet"
    saved = json.loads(orch.paths.config_path.read_text(encoding="utf-8"))
    assert saved["model"] == "sonnet"
    assert saved["provider"] == "claude"
    orch._cli_service.update_default_model.assert_called_once_with("sonnet")


async def test_switch_model_fresh_topic_preserves_all_provider_sessions(
    orch: Orchestrator,
) -> None:
    key = SessionKey(chat_id=-100, topic_id=45)
    session, _ = await orch._sessions.resolve_session(key, provider="claude", model="opus")
    session.provider_sessions["claude"] = ProviderSessionData(
        session_id="claude-session",
        message_count=4,
        total_cost_usd=0.4,
        total_tokens=400,
    )
    session.provider_sessions["codex"] = ProviderSessionData(
        session_id="codex-session",
        message_count=2,
        total_cost_usd=0.2,
        total_tokens=200,
    )
    await orch._sessions.preserve_session_identity(session)
    persisted = await orch._sessions.get_active(key)
    assert persisted is not None
    provider_sessions_before = copy.deepcopy(persisted.provider_sessions)
    object.__setattr__(orch._process_registry, "kill_by_chat_topic", AsyncMock(return_value=0))

    await switch_model(orch, key, "gpt-5.2-codex")

    retargeted = await orch._sessions.get_active(key)
    assert retargeted is not None
    assert retargeted.provider == "codex"
    assert retargeted.model == "gpt-5.2-codex"
    assert retargeted.provider_sessions == provider_sessions_before
    assert retargeted.session_id == "codex-session"


async def test_switch_model_stale_topic_does_not_show_resume_hint(
    orch: Orchestrator,
) -> None:
    key = SessionKey(chat_id=-100, topic_id=46)
    orch._config.max_session_messages = 1
    stale, _ = await orch._sessions.resolve_session(key, provider="claude", model="opus")
    stale.session_id = "stale-claude-session"
    stale.provider_sessions["codex"] = ProviderSessionData(
        session_id="stale-codex-session",
        message_count=7,
    )
    await orch._sessions.update_session(stale)
    object.__setattr__(orch._process_registry, "kill_by_chat_topic", AsyncMock(return_value=0))

    result = await switch_model(orch, key, "gpt-5.2-codex")

    assert "Resuming" not in result
    replaced = await orch._sessions.get_active(key)
    assert replaced is not None
    # The maxed-out codex target bucket is reset (so no resume hint), but the
    # session shell and the other provider's bucket are kept.
    assert "codex" not in replaced.provider_sessions
    assert replaced.provider_sessions["claude"].session_id == "stale-claude-session"


async def test_switch_model_opus_1m_persists(orch: Orchestrator) -> None:
    """opus[1m] is a valid Claude alias; switch_model persists it to config (#76)."""
    object.__setattr__(orch._process_registry, "kill_by_chat_topic", AsyncMock(return_value=0))
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
    object.__setattr__(orch._process_registry, "kill_by_chat_topic", AsyncMock(return_value=0))
    result = await switch_model(orch, SessionKey(chat_id=1), "sonnet", reasoning_effort="high")
    assert "high" in result.lower()
    assert orch._config.reasoning_effort == "high"
    saved = json.loads(orch.paths.config_path.read_text(encoding="utf-8"))
    assert saved["reasoning_effort"] == "high"


async def test_switch_model_persists_to_config(orch: Orchestrator) -> None:
    object.__setattr__(orch._process_registry, "kill_by_chat_topic", AsyncMock(return_value=0))
    await switch_model(orch, SessionKey(chat_id=1), "sonnet")
    saved = json.loads(orch.paths.config_path.read_text(encoding="utf-8"))
    assert saved["model"] == "sonnet"


async def test_switch_model_provider_change(orch: Orchestrator) -> None:
    mock_reset = AsyncMock()
    object.__setattr__(orch._process_registry, "kill_by_chat_topic", AsyncMock(return_value=0))
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

    object.__setattr__(orch._process_registry, "kill_by_chat_topic", AsyncMock(return_value=0))
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

    object.__setattr__(orch._process_registry, "kill_by_chat_topic", AsyncMock(return_value=0))
    result = await switch_model(orch, SessionKey(chat_id=1), "o3")

    assert "Resuming session `codex-xyz789`." in result
    assert "You have already sent 1 message in this provider session." in result
    assert "Current model: `o3`." in result
    assert "Use /new to start a fresh session." in result


async def test_switch_reasoning_only(orch: Orchestrator) -> None:
    """Changing only reasoning effort does not reset session."""
    mock_kill = AsyncMock(return_value=0)
    mock_reset = AsyncMock()
    object.__setattr__(orch._process_registry, "kill_by_chat_topic", mock_kill)
    object.__setattr__(orch._sessions, "reset_provider_session", mock_reset)
    result = await switch_model(orch, SessionKey(chat_id=1), "opus", reasoning_effort="high")
    assert "high" in result  # effort value shown in the message
    mock_kill.assert_not_called()
    mock_reset.assert_not_called()


async def test_switch_model_rejects_invalid_codex_reasoning_effort(orch: Orchestrator) -> None:
    from unittest.mock import MagicMock

    from ductor_bot.cli.codex_cache import CodexModelCache
    from ductor_bot.cli.codex_discovery import CodexModelInfo

    object.__setattr__(orch._process_registry, "kill_by_chat_topic", AsyncMock(return_value=0))
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
    object.__setattr__(orch._process_registry, "kill_by_chat_topic", AsyncMock(return_value=0))
    result = await switch_model(orch, SessionKey(chat_id=1), "opus", reasoning_effort="max")
    assert "Invalid reasoning effort" not in result
    assert orch._config.reasoning_effort == "max"


async def test_switch_model_codex_rejects_max_with_cache(orch: Orchestrator) -> None:
    object.__setattr__(orch._process_registry, "kill_by_chat_topic", AsyncMock(return_value=0))
    with _with_codex_cache(orch):
        result = await switch_model(
            orch, SessionKey(chat_id=1), "gpt-5.2-codex", reasoning_effort="max"
        )
    assert "Invalid reasoning effort" in result
    assert "max" in result


async def test_switch_model_codex_rejects_max_no_cache(orch: Orchestrator) -> None:
    """Even without a Codex cache, the fallback set rejects ``max``."""
    object.__setattr__(orch._process_registry, "kill_by_chat_topic", AsyncMock(return_value=0))
    orch._observers.codex_cache_obs = None
    result = await switch_model(
        orch, SessionKey(chat_id=1), "gpt-5.2-codex", reasoning_effort="max"
    )
    assert "Invalid reasoning effort" in result


async def test_provider_switch_resets_invalid_effort_to_medium(orch: Orchestrator) -> None:
    """Claude+max then /model to Codex must reset effort to medium (max not sent)."""
    object.__setattr__(orch._process_registry, "kill_by_chat_topic", AsyncMock(return_value=0))
    await switch_model(orch, SessionKey(chat_id=1), "opus", reasoning_effort="max")
    assert orch._config.reasoning_effort == "max"

    orch._observers.codex_cache_obs = None  # exercise the fallback path
    await switch_model(orch, SessionKey(chat_id=1), "gpt-5.2-codex")
    assert orch._config.reasoning_effort == "medium"
    saved = json.loads(orch.paths.config_path.read_text(encoding="utf-8"))
    assert saved["reasoning_effort"] == "medium"


async def test_provider_switch_keeps_valid_effort(orch: Orchestrator) -> None:
    """A carried-over effort valid for the new provider is left untouched."""
    object.__setattr__(orch._process_registry, "kill_by_chat_topic", AsyncMock(return_value=0))
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
    # /effort uses the dedicated per-session ms:e callback (no model in payload).
    assert "ms:e:max" in callbacks


async def test_effort_selector_codex_no_max(orch: Orchestrator) -> None:
    from ductor_bot.orchestrator.selectors.model_selector import effort_selector_start

    object.__setattr__(orch._process_registry, "kill_by_chat_topic", AsyncMock(return_value=0))
    await switch_model(orch, SessionKey(chat_id=1), "gpt-5.2-codex")
    orch._observers.codex_cache_obs = None
    resp = await effort_selector_start(orch, SessionKey(chat_id=1))
    assert resp.buttons is not None
    labels = [b.text for row in resp.buttons.rows for b in row]
    assert "Max" not in labels


async def test_effort_selector_unsupported_provider_info_only(orch: Orchestrator) -> None:
    from ductor_bot.orchestrator.selectors.model_selector import effort_selector_start

    object.__setattr__(orch._process_registry, "kill_by_chat_topic", AsyncMock(return_value=0))
    await switch_model(orch, SessionKey(chat_id=1), "gemini-2.5-pro")
    resp = await effort_selector_start(orch, SessionKey(chat_id=1))
    assert resp.buttons is None  # info message only, no UI
    assert "gemini" in resp.text.lower()


async def test_effort_selector_main_scopes_to_active_session_model(orch: Orchestrator) -> None:
    """/effort in main/DM scopes to the ACTIVE session model (opus -> Max range),
    independent of the configured default model."""
    from ductor_bot.orchestrator.selectors.model_selector import effort_selector_start

    orch._config.model = "gpt-5.2-codex"  # configured default differs from session
    session, _ = await orch._sessions.resolve_session(
        SessionKey(chat_id=1), provider="claude", model="opus"
    )
    await orch._sessions.update_session(session)

    resp = await effort_selector_start(orch, SessionKey(chat_id=1))

    labels = [b.text for row in resp.buttons.rows for b in row]
    assert "Max" in labels  # opus (claude) range, not codex's config default
    callbacks = [b.callback_data for row in resp.buttons.rows for b in row]
    assert "ms:e:max" in callbacks


async def test_effort_in_main_changes_session_only_not_config(orch: Orchestrator) -> None:
    """End-to-end: /effort in main changes only the active session's effort;
    config.model and config.reasoning_effort (the configured default) stay put."""
    orch._config.model = "opus"
    orch._config.reasoning_effort = "medium"
    session, _ = await orch._sessions.resolve_session(
        SessionKey(chat_id=1), provider="claude", model="opus"
    )
    await orch._sessions.update_session(session)
    model_mock = MagicMock()
    effort_mock = MagicMock()
    object.__setattr__(orch._cli_service, "update_default_model", model_mock)
    object.__setattr__(orch._cli_service, "update_reasoning_effort", effort_mock)

    result = await handle_model_callback(orch, SessionKey(chat_id=1), "ms:e:high")

    # Active session effort changed; configured default untouched.
    active = await orch._sessions.get_active(SessionKey(chat_id=1))
    assert active is not None
    assert active.reasoning_effort == "high"
    assert orch._config.model == "opus"
    assert orch._config.reasoning_effort == "medium"  # configured default unchanged
    model_mock.assert_not_called()
    effort_mock.assert_not_called()
    assert "high" in result.text  # applied effort value shown


async def test_effort_no_active_session_creates_session_no_config_change(
    orch: Orchestrator,
) -> None:
    """/effort on a fresh chat (no active session) records effort on a NEW
    session and never mutates the configured default (regression guard)."""
    orch._config.model = "opus"
    orch._config.reasoning_effort = "medium"
    model_mock = MagicMock()
    effort_mock = MagicMock()
    object.__setattr__(orch._cli_service, "update_default_model", model_mock)
    object.__setattr__(orch._cli_service, "update_reasoning_effort", effort_mock)
    assert await orch._sessions.get_active(SessionKey(chat_id=1)) is None  # fresh

    result = await handle_model_callback(orch, SessionKey(chat_id=1), "ms:e:high")

    # Configured default untouched.
    assert orch._config.model == "opus"
    assert orch._config.reasoning_effort == "medium"
    model_mock.assert_not_called()
    effort_mock.assert_not_called()
    # A session was created and carries the chosen effort.
    session = await orch._sessions.get_active(SessionKey(chat_id=1))
    assert session is not None
    assert session.reasoning_effort == "high"
    assert "high" in result.text  # applied effort value shown


async def test_effort_in_main_resets_unsupported_for_session_provider(
    orch: Orchestrator,
) -> None:
    """/effort applied to a codex session rejects a claude-only level (max->medium)."""
    session, _ = await orch._sessions.resolve_session(
        SessionKey(chat_id=1), provider="codex", model="gpt-5.2-codex"
    )
    await orch._sessions.update_session(session)
    orch._observers.codex_cache_obs = None  # codex fallback set (no max)

    await handle_model_callback(orch, SessionKey(chat_id=1), "ms:e:max")

    active = await orch._sessions.get_active(SessionKey(chat_id=1))
    assert active is not None
    assert active.reasoning_effort == "medium"


# -- topic-session effort apply (Gate D) ------------------------------------


async def test_topic_effort_change_is_session_scoped(orch: Orchestrator) -> None:
    """`/effort` in a TOPIC updates only that topic's session, not the global default."""
    key = SessionKey(chat_id=1, topic_id=7)
    await orch._sessions.resolve_session(key, provider="claude", model="opus")
    object.__setattr__(orch._process_registry, "kill_by_chat_topic", AsyncMock(return_value=0))
    update_mock = MagicMock()
    object.__setattr__(orch._cli_service, "update_reasoning_effort", update_mock)
    global_before = orch._config.reasoning_effort

    await switch_model(orch, key, "opus", reasoning_effort="high")

    session = await orch._sessions.get_active(key)
    assert session is not None
    assert session.reasoning_effort == "high"  # topic session updated
    assert orch._config.reasoning_effort == global_before  # global default unchanged
    update_mock.assert_not_called()  # no global update from a topic


async def test_topic_provider_switch_resets_invalid_effort_in_session(
    orch: Orchestrator,
) -> None:
    """Topic claude+max -> codex resets the SESSION effort to medium (global unchanged)."""
    key = SessionKey(chat_id=1, topic_id=7)
    await orch._sessions.resolve_session(key, provider="claude", model="opus")
    object.__setattr__(orch._process_registry, "kill_by_chat_topic", AsyncMock(return_value=0))
    global_before = orch._config.reasoning_effort

    await switch_model(orch, key, "opus", reasoning_effort="max")
    session = await orch._sessions.get_active(key)
    assert session is not None
    assert session.reasoning_effort == "max"

    orch._observers.codex_cache_obs = None  # exercise the fallback
    await switch_model(orch, key, "gpt-5.2-codex")
    session = await orch._sessions.get_active(key)
    assert session is not None
    assert session.reasoning_effort == "medium"  # session reset
    assert orch._config.reasoning_effort == global_before  # global default unchanged


# -- per-session effort isolation (regression coverage for #161) --------------


async def test_topic_effort_isolated_from_other_topics_and_main(orch: Orchestrator) -> None:
    """Changing effort in topic A must not affect topic B, topic C, or main."""
    object.__setattr__(orch._process_registry, "kill_by_chat_topic", AsyncMock(return_value=0))
    a = SessionKey(chat_id=1, topic_id=10)
    b = SessionKey(chat_id=1, topic_id=20)
    main = SessionKey(chat_id=1)
    for k in (a, b, main):
        await orch._sessions.resolve_session(k, provider="claude", model="opus")

    await switch_model(orch, a, "opus", reasoning_effort="high")

    sa = await orch._sessions.get_active(a)
    sb = await orch._sessions.get_active(b)
    sm = await orch._sessions.get_active(main)
    assert sa is not None
    assert sa.reasoning_effort == "high"
    # B/C/main keep their captured default (not "high")
    assert sb is not None
    assert sb.reasoning_effort != "high"
    assert sm is not None
    assert sm.reasoning_effort != "high"
    assert orch._config.reasoning_effort != "high"


async def test_main_effort_change_sets_global_default(orch: Orchestrator) -> None:
    """Main/DM effort change updates the global default + service config."""
    object.__setattr__(orch._process_registry, "kill_by_chat_topic", AsyncMock(return_value=0))
    update_mock = MagicMock()
    object.__setattr__(orch._cli_service, "update_reasoning_effort", update_mock)
    main = SessionKey(chat_id=1)
    await orch._sessions.resolve_session(main, provider="claude", model="opus")

    await switch_model(orch, main, "opus", reasoning_effort="high")

    assert orch._config.reasoning_effort == "high"
    update_mock.assert_called_once_with("high")


# -- /effort response shows the applied effort value --------------------------


async def test_effort_message_shows_applied_value_after_reset(
    orch: Orchestrator,
) -> None:
    """The /effort response shows the value actually applied to the session,
    i.e. the post-validation value (max -> medium on codex), not the request.
    """
    orch._config.model = "gpt-5.2-codex"
    orch._config.provider = "codex"
    key = SessionKey(chat_id=1)
    session, _ = await orch._sessions.resolve_session(key, provider="codex", model="gpt-5.2-codex")
    await orch._sessions.update_session(session)

    orch._observers.codex_cache_obs = None  # codex fallback: max unsupported
    result = await handle_model_callback(orch, key, "ms:e:max")

    active = await orch._sessions.get_active(key)
    assert active is not None
    assert active.reasoning_effort == "medium"  # max reset to codex-valid
    assert "medium" in result.text  # applied value shown
    assert "max" not in result.text  # not the rejected request


def test_effort_updated_locales_have_effort_placeholder() -> None:
    """Every locale defining model.effort_updated must include the {effort}
    placeholder so the applied value renders (i18n consistency)."""
    import tomllib
    from pathlib import Path

    i18n_dir = Path(__file__).resolve().parents[2] / "ductor_bot" / "i18n"
    found = 0
    for chat_toml in sorted(i18n_dir.glob("*/chat.toml")):
        data = tomllib.loads(chat_toml.read_text(encoding="utf-8"))
        template = data.get("model", {}).get("effort_updated")
        if template is None:
            continue
        found += 1
        assert "{effort}" in template, (
            f"{chat_toml.parent.name}/chat.toml effort_updated missing {{effort}}: {template!r}"
        )
    assert found > 0, "no locale defines model.effort_updated"


# -- /effort drops the pooled interactive REPL so the new effort takes hold ---


async def test_effort_kills_interactive_repl_when_available(
    orch: Orchestrator,
) -> None:
    """/effort must drop a pooled interactive REPL so the next message respawns
    it with the new effort (the REPL would otherwise keep its spawn-time
    effort). cli_service.kill_interactive_repl is called with the session key.
    """
    orch._config.model = "opus"
    key = SessionKey(chat_id=42, topic_id=7)
    session, _ = await orch._sessions.resolve_session(key, provider="claude", model="opus")
    await orch._sessions.update_session(session)
    repl_kill = MagicMock()
    object.__setattr__(orch._cli_service, "kill_interactive_repl", repl_kill)

    await handle_model_callback(orch, key, "ms:e:high")

    repl_kill.assert_called_once_with(key.transport, key.chat_id, key.topic_id)


async def test_effort_skips_repl_kill_when_unavailable(orch: Orchestrator) -> None:
    """When cli_service has no kill_interactive_repl (base predating #156),
    /effort still applies the effort without error (guarded no-op)."""
    orch._config.model = "opus"
    key = SessionKey(chat_id=1)
    session, _ = await orch._sessions.resolve_session(key, provider="claude", model="opus")
    await orch._sessions.update_session(session)
    # Ensure the attribute is absent on this cli_service instance.
    if hasattr(orch._cli_service, "kill_interactive_repl"):
        delattr(orch._cli_service, "kill_interactive_repl")
    assert not hasattr(orch._cli_service, "kill_interactive_repl")

    result = await handle_model_callback(orch, key, "ms:e:high")

    active = await orch._sessions.get_active(key)
    assert active is not None
    assert active.reasoning_effort == "high"  # effort applied, no AttributeError
    assert result is not None


async def test_effort_no_session_kills_interactive_repl_when_available(
    orch: Orchestrator,
) -> None:
    """The no-active-session /effort path also drops a pooled REPL when present."""
    orch._config.model = "opus"
    key = SessionKey(chat_id=99)
    assert await orch._sessions.get_active(key) is None
    repl_kill = MagicMock()
    object.__setattr__(orch._cli_service, "kill_interactive_repl", repl_kill)

    await handle_model_callback(orch, key, "ms:e:high")

    repl_kill.assert_called_once_with(key.transport, key.chat_id, key.topic_id)


# -- /effort & /model header show the session-first effort (display parity) ---


async def test_status_line_shows_session_effort_not_config(orch: Orchestrator) -> None:
    """The header reads the effective (session-first) effort, matching /status
    and the runtime — not config.reasoning_effort.

    Session opus/high, config medium -> header shows "high", not "medium".
    """
    from ductor_bot.orchestrator.selectors.model_selector import _status_line

    orch._config.reasoning_effort = "medium"
    main = SessionKey(chat_id=1)
    session, _ = await orch._sessions.resolve_session(
        main, provider="claude", model="opus", reasoning_effort="high"
    )
    await orch._sessions.update_session(session)

    header = await _status_line(orch, main)

    assert "high" in header
    assert "(medium)" not in header


async def test_status_line_falls_back_to_config_without_session(
    orch: Orchestrator,
) -> None:
    """Without an active session the header shows the config default effort."""
    from ductor_bot.orchestrator.selectors.model_selector import _status_line

    orch._config.reasoning_effort = "high"
    assert await orch._sessions.get_active(SessionKey(chat_id=1)) is None

    header = await _status_line(orch, SessionKey(chat_id=1))

    assert "high" in header


async def test_status_line_topic_shows_session_effort(orch: Orchestrator) -> None:
    """In a topic the header shows that topic session's effort."""
    from ductor_bot.orchestrator.selectors.model_selector import _status_line

    orch._config.reasoning_effort = "medium"
    key = SessionKey(chat_id=1, topic_id=7)
    session, _ = await orch._sessions.resolve_session(
        key, provider="claude", model="opus", reasoning_effort="high"
    )
    await orch._sessions.update_session(session)

    header = await _status_line(orch, key)

    assert "high" in header
    assert "(medium)" not in header


# -- session effort reset must not leak into a valid config default -----------


async def test_session_effort_reset_does_not_overwrite_valid_config(
    orch: Orchestrator,
) -> None:
    """A session-only effort reset (max -> medium on a provider switch) must not
    clobber an already-valid config default.

    config gpt-5.2-codex/codex/high (valid), session opus/claude/max.
    /model gpt-5.2-codex (no explicit effort): the session resets max->medium,
    but config.reasoning_effort must stay high (codex-valid), not become medium.
    """
    object.__setattr__(orch._process_registry, "kill_by_chat_topic", AsyncMock(return_value=0))
    orch._config.model = "gpt-5.2-codex"
    orch._config.provider = "codex"
    orch._config.reasoning_effort = "high"
    main = SessionKey(chat_id=1)
    session, _ = await orch._sessions.resolve_session(
        main, provider="claude", model="opus", reasoning_effort="max"
    )
    await orch._sessions.update_session(session)

    with _with_codex_cache(orch):
        await switch_model(orch, main, "gpt-5.2-codex")

    # config default unchanged -> not rewritten (in-memory state is the guard).
    assert orch._config.reasoning_effort == "high"  # valid config default kept
    synced = await orch._sessions.get_active(main)
    assert synced is not None
    assert synced.reasoning_effort == "medium"  # session reset (max invalid)


async def test_main_model_explicit_effort_sets_config(orch: Orchestrator) -> None:
    """An explicit effort (/model wizard reasoning step) sets the config
    default to exactly that value."""
    object.__setattr__(orch._process_registry, "kill_by_chat_topic", AsyncMock(return_value=0))
    orch._config.model = "opus"
    orch._config.provider = "claude"
    orch._config.reasoning_effort = "medium"
    main = SessionKey(chat_id=1)
    await orch._sessions.resolve_session(main, provider="claude", model="opus")

    await switch_model(orch, main, "sonnet", reasoning_effort="high")

    assert orch._config.reasoning_effort == "high"
    saved = json.loads(orch.paths.config_path.read_text(encoding="utf-8"))
    assert saved["reasoning_effort"] == "high"


# -- main /model keeps config.reasoning_effort consistent with config.model ---


async def test_main_model_resets_invalid_config_effort(orch: Orchestrator) -> None:
    """Main /model re-validates config.reasoning_effort against the new default
    model. config opus/claude/max, session gpt-5.2-codex; /model gpt-5.2-codex
    -> config becomes gpt-5.2-codex/codex/medium (max is codex-invalid).

    provider_changed is False here (session is already codex), so the session
    effort path leaves effort=None; the config block must still fix its own
    stale max independently.
    """
    object.__setattr__(orch._process_registry, "kill_by_chat_topic", AsyncMock(return_value=0))
    orch._config.model = "opus"
    orch._config.provider = "claude"
    orch._config.reasoning_effort = "max"
    main = SessionKey(chat_id=1)
    session, _ = await orch._sessions.resolve_session(
        main, provider="codex", model="gpt-5.2-codex", reasoning_effort="medium"
    )
    await orch._sessions.update_session(session)

    orch._observers.codex_cache_obs = None  # exercise the fallback efforts
    await switch_model(orch, main, "gpt-5.2-codex")

    assert orch._config.model == "gpt-5.2-codex"
    assert orch._config.provider == "codex"
    assert orch._config.reasoning_effort == "medium"  # max reset to codex-valid
    saved = json.loads(orch.paths.config_path.read_text(encoding="utf-8"))
    assert saved["reasoning_effort"] == "medium"
    synced = await orch._sessions.get_active(main)
    assert synced is not None
    assert synced.model == "gpt-5.2-codex"
    assert synced.reasoning_effort == "medium"  # session unchanged


async def test_main_model_keeps_valid_config_effort(orch: Orchestrator) -> None:
    """Main /model keeps config.reasoning_effort when it stays valid for the new
    default model. config codex/medium, session opus/claude/max; /model opus ->
    config becomes opus/claude/max (claude supports max, carried)."""
    object.__setattr__(orch._process_registry, "kill_by_chat_topic", AsyncMock(return_value=0))
    orch._config.model = "gpt-5.2-codex"
    orch._config.provider = "codex"
    orch._config.reasoning_effort = "max"
    main = SessionKey(chat_id=1)
    session, _ = await orch._sessions.resolve_session(
        main, provider="claude", model="opus", reasoning_effort="max"
    )
    await orch._sessions.update_session(session)

    await switch_model(orch, main, "opus")

    assert orch._config.model == "opus"
    assert orch._config.provider == "claude"
    assert orch._config.reasoning_effort == "max"  # claude supports max -> carried


async def test_topic_model_does_not_touch_config_effort(orch: Orchestrator) -> None:
    """Topic /model never mutates the global config.reasoning_effort."""
    object.__setattr__(orch._process_registry, "kill_by_chat_topic", AsyncMock(return_value=0))
    orch._config.model = "opus"
    orch._config.provider = "claude"
    orch._config.reasoning_effort = "max"
    key = SessionKey(chat_id=1, topic_id=7)
    await orch._sessions.resolve_session(key, provider="codex", model="gpt-5.2-codex")

    with _with_codex_cache(orch):
        await switch_model(orch, key, "gpt-5.2-codex")

    assert orch._config.reasoning_effort == "max"  # untouched from a topic
    assert orch._config.model == "opus"


# -- main /model keeps config.model and config.provider consistent ------------


async def test_main_model_realign_fixes_stale_provider(orch: Orchestrator) -> None:
    """Main /model re-aligns config.provider to the model's provider even when
    provider_changed is False (session-based) because config had diverged.

    Session opus/claude, config gpt-5.2-codex/codex; /model opus -> config.model
    AND config.provider must both land on opus/claude (no claude-model + codex-
    provider mismatch).
    """
    object.__setattr__(orch._process_registry, "kill_by_chat_topic", AsyncMock(return_value=0))
    orch._config.model = "gpt-5.2-codex"
    orch._config.provider = "codex"
    main = SessionKey(chat_id=1)
    session, _ = await orch._sessions.resolve_session(main, provider="claude", model="opus")
    await orch._sessions.update_session(session)

    await switch_model(orch, main, "opus")

    assert orch._config.model == "opus"
    assert orch._config.provider == "claude"  # provider realigned, not stale codex
    saved = json.loads(orch.paths.config_path.read_text(encoding="utf-8"))
    assert saved["model"] == "opus"
    assert saved["provider"] == "claude"  # persisted consistently
    synced = await orch._sessions.get_active(main)
    assert synced is not None
    assert synced.model == "opus"


async def test_main_model_normal_provider_switch_updates_both(
    orch: Orchestrator,
) -> None:
    """Main /model with session == config across providers updates config.model
    and config.provider (unchanged behavior)."""
    object.__setattr__(orch._process_registry, "kill_by_chat_topic", AsyncMock(return_value=0))
    orch._config.model = "opus"
    orch._config.provider = "claude"
    main = SessionKey(chat_id=1)
    session, _ = await orch._sessions.resolve_session(main, provider="claude", model="opus")
    await orch._sessions.update_session(session)

    await switch_model(orch, main, "o3")

    assert orch._config.model == "o3"
    assert orch._config.provider == "codex"


async def test_topic_model_does_not_touch_config_provider(
    orch: Orchestrator,
) -> None:
    """Topic /model never mutates the global config.provider."""
    object.__setattr__(orch._process_registry, "kill_by_chat_topic", AsyncMock(return_value=0))
    orch._config.model = "gpt-5.2-codex"
    orch._config.provider = "codex"
    key = SessionKey(chat_id=1, topic_id=7)
    await orch._sessions.resolve_session(key, provider="claude", model="opus")

    await switch_model(orch, key, "sonnet")

    assert orch._config.provider == "codex"  # untouched from a topic
    assert orch._config.model == "gpt-5.2-codex"


# -- main /model re-aligns the global default to a diverged session model -----


async def test_main_model_realigns_default_to_session_model(
    orch: Orchestrator,
) -> None:
    """Main /model <session's model> updates the global default even when the
    session already runs that model (config.model had diverged).

    Session opus, config gpt-5.2-codex; /model opus is not a no-op in the main
    chat because it still re-aligns config.model to opus.
    """
    object.__setattr__(orch._process_registry, "kill_by_chat_topic", AsyncMock(return_value=0))
    orch._config.model = "gpt-5.2-codex"
    orch._config.provider = "codex"
    main = SessionKey(chat_id=1)
    session, _ = await orch._sessions.resolve_session(main, provider="claude", model="opus")
    await orch._sessions.update_session(session)

    result = await switch_model(orch, main, "opus")

    assert "Already running" not in result
    assert orch._config.model == "opus"  # global default re-aligned
    synced = await orch._sessions.get_active(main)
    assert synced is not None
    assert synced.model == "opus"  # session unchanged


async def test_main_model_same_as_config_is_noop(orch: Orchestrator) -> None:
    """Main /model when session == config == model is a real no-op."""
    orch._config.model = "opus"
    main = SessionKey(chat_id=1)
    session, _ = await orch._sessions.resolve_session(main, provider="claude", model="opus")
    await orch._sessions.update_session(session)

    result = await switch_model(orch, main, "opus")

    assert "Already running" in result


async def test_topic_model_same_model_is_noop(orch: Orchestrator) -> None:
    """Topic /model with the session's current model is a no-op regardless of
    config.model (is_topic short-circuits on same_model alone)."""
    orch._config.model = "gpt-5.2-codex"
    key = SessionKey(chat_id=1, topic_id=7)
    await orch._sessions.resolve_session(key, provider="claude", model="opus")

    result = await switch_model(orch, key, "opus")

    assert "Already running" in result
    assert orch._config.model == "gpt-5.2-codex"  # config untouched from a topic


# -- main provider switch re-validates the SESSION effort (current_effort) -----


async def test_main_provider_switch_resets_diverged_session_effort(
    orch: Orchestrator,
) -> None:
    """Main /model provider switch re-validates the active session's effort, not
    just config's. Session claude/opus+max, config codex/medium; /model gpt-5.2-codex
    must reset the session effort to a codex-valid value (max is unsupported).

    Guards the is_topic gate on ``current_effort``: without it the session ``max``
    would survive into a codex session and reach the CLI as invalid.
    """
    object.__setattr__(orch._process_registry, "kill_by_chat_topic", AsyncMock(return_value=0))
    orch._config.model = "gpt-5.2-codex"
    orch._config.provider = "codex"
    orch._config.reasoning_effort = "medium"
    main = SessionKey(chat_id=1)
    session, _ = await orch._sessions.resolve_session(
        main, provider="claude", model="opus", reasoning_effort="max"
    )
    await orch._sessions.update_session(session)

    orch._observers.codex_cache_obs = None  # exercise the fallback efforts
    await switch_model(orch, main, "gpt-5.2-codex")

    synced = await orch._sessions.get_active(main)
    assert synced is not None
    assert synced.model == "gpt-5.2-codex"
    assert synced.reasoning_effort == "medium"  # max reset to a codex-valid value


async def test_main_same_provider_switch_carries_session_effort(
    orch: Orchestrator,
) -> None:
    """Main /model within the same provider (claude->claude) carries the session's
    effort unchanged (no provider switch -> no re-validation)."""
    object.__setattr__(orch._process_registry, "kill_by_chat_topic", AsyncMock(return_value=0))
    orch._config.model = "opus"
    main = SessionKey(chat_id=1)
    session, _ = await orch._sessions.resolve_session(
        main, provider="claude", model="opus", reasoning_effort="max"
    )
    await orch._sessions.update_session(session)

    await switch_model(orch, main, "sonnet")

    synced = await orch._sessions.get_active(main)
    assert synced is not None
    assert synced.model == "sonnet"
    assert synced.reasoning_effort == "max"  # carried, still claude-valid


# -- /model re-aligns a diverged main session (old computed from session) ------


async def test_main_model_realigns_diverged_session(orch: Orchestrator) -> None:
    """Main /model when the session model has diverged from config.model still
    syncs the session and keeps config.model at the requested target.

    Repro: session opus, config gpt-5.2-codex; /model gpt-5.2-codex must sync the
    session to gpt-5.2-codex (old is the session model, so not same_model) rather
    than no-op because old happened to equal config.model.
    """
    object.__setattr__(orch._process_registry, "kill_by_chat_topic", AsyncMock(return_value=0))
    orch._config.model = "gpt-5.2-codex"
    orch._config.provider = "codex"
    main = SessionKey(chat_id=1)
    session, _ = await orch._sessions.resolve_session(main, provider="claude", model="opus")
    await orch._sessions.update_session(session)

    result = await switch_model(orch, main, "gpt-5.2-codex")

    assert "Already running" not in result
    synced = await orch._sessions.get_active(main)
    assert synced is not None
    assert synced.model == "gpt-5.2-codex"  # session re-aligned
    assert orch._config.model == "gpt-5.2-codex"  # global default preserved


async def test_main_model_normal_switch_changes_both(orch: Orchestrator) -> None:
    """Main /model with session == config switches both the session and the
    global default (unchanged upstream behavior)."""
    object.__setattr__(orch._process_registry, "kill_by_chat_topic", AsyncMock(return_value=0))
    orch._config.model = "opus"
    main = SessionKey(chat_id=1)
    session, _ = await orch._sessions.resolve_session(main, provider="claude", model="opus")
    await orch._sessions.update_session(session)

    await switch_model(orch, main, "sonnet")

    synced = await orch._sessions.get_active(main)
    assert synced is not None
    assert synced.model == "sonnet"  # session changed
    assert orch._config.model == "sonnet"  # global default changed


async def test_topic_model_unchanged_by_old_fix(orch: Orchestrator) -> None:
    """Topic /model still syncs the topic session and never touches config."""
    object.__setattr__(orch._process_registry, "kill_by_chat_topic", AsyncMock(return_value=0))
    orch._config.model = "opus"
    key = SessionKey(chat_id=1, topic_id=7)
    await orch._sessions.resolve_session(key, provider="claude", model="opus")

    await switch_model(orch, key, "sonnet")

    session = await orch._sessions.get_active(key)
    assert session is not None
    assert session.model == "sonnet"  # topic session synced
    assert orch._config.model == "opus"  # global default untouched


async def test_codex_topic_effort_is_session_scoped(orch: Orchestrator) -> None:
    """Codex effort change in a topic is session-scoped too (not just claude)."""
    object.__setattr__(orch._process_registry, "kill_by_chat_topic", AsyncMock(return_value=0))
    key = SessionKey(chat_id=1, topic_id=7)
    await orch._sessions.resolve_session(key, provider="codex", model="gpt-5.2-codex")
    global_before = orch._config.reasoning_effort
    with _with_codex_cache(orch):
        await switch_model(orch, key, "gpt-5.2-codex", reasoning_effort="high")
    session = await orch._sessions.get_active(key)
    assert session is not None
    assert session.reasoning_effort == "high"
    assert orch._config.reasoning_effort == global_before
