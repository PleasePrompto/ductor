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


async def test_callback_model_claude_switches(orch: Orchestrator) -> None:
    object.__setattr__(orch._process_registry, "kill_all", AsyncMock(return_value=0))
    resp = await handle_model_callback(orch, SessionKey(chat_id=1), "ms:m:sonnet")
    assert "sonnet" in resp.text
    assert resp.buttons is None
    assert orch._config.model == "sonnet"


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


async def test_callback_reasoning_topic_without_session_targets_next_message(
    orch: Orchestrator,
) -> None:
    key = SessionKey(chat_id=-100, topic_id=42)
    object.__setattr__(orch._process_registry, "kill_all", AsyncMock(return_value=0))
    mock_execute = AsyncMock(
        return_value=AgentResponse(result="ok", session_id="codex-topic-session")
    )
    object.__setattr__(orch._cli_service, "execute", mock_execute)

    await handle_model_callback(orch, key, "ms:r:high:gpt-5.2-codex")
    await normal(orch, key, "hello", model_override=None)

    request = mock_execute.call_args.args[0]
    assert request.model_override == "gpt-5.2-codex"
    assert request.provider_override == "codex"


async def test_callback_reasoning_stale_topic_targets_next_message(orch: Orchestrator) -> None:
    key = SessionKey(chat_id=-100, topic_id=43)
    orch._config.max_session_messages = 1
    stale, _ = await orch._sessions.resolve_session(key, provider="claude", model="opus")
    stale.session_id = "stale-claude-session"
    await orch._sessions.update_session(stale)

    object.__setattr__(orch._process_registry, "kill_all", AsyncMock(return_value=0))
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

    object.__setattr__(orch._process_registry, "kill_all", AsyncMock(return_value=0))
    mock_execute = AsyncMock(
        return_value=AgentResponse(result="ok", session_id="fresh-codex-session")
    )
    object.__setattr__(orch._cli_service, "execute", mock_execute)

    await handle_model_callback(orch, key, "ms:r:high:gpt-5.2-codex")
    replacement = await orch._sessions.get_active(key)
    assert replacement is not None
    assert replacement.provider_sessions == {}
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


async def test_switch_model_topic_does_not_change_global_defaults(orch: Orchestrator) -> None:
    key = SessionKey(chat_id=-100, topic_id=44)
    config_before = (
        orch._config.provider,
        orch._config.model,
        orch._config.reasoning_effort,
    )
    config_file_before = orch.paths.config_path.read_bytes()
    object.__setattr__(orch._process_registry, "kill_all", AsyncMock(return_value=0))

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
    object.__setattr__(orch._process_registry, "kill_all", AsyncMock(return_value=0))
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
    object.__setattr__(orch._process_registry, "kill_all", AsyncMock(return_value=0))

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
    object.__setattr__(orch._process_registry, "kill_all", AsyncMock(return_value=0))

    result = await switch_model(orch, key, "gpt-5.2-codex")

    assert "Resuming" not in result
    replaced = await orch._sessions.get_active(key)
    assert replaced is not None
    assert replaced.provider_sessions == {}


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
