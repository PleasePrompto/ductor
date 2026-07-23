"""Tests for conversation flows."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from ductor_bot.cli.types import AgentResponse
from ductor_bot.orchestrator.core import Orchestrator
from ductor_bot.orchestrator.flows import (
    StreamingCallbacks,
    _finish_normal,
    _strip_ack_token,
    _update_session,
    heartbeat_flow,
    named_session_flow,
    normal,
    normal_streaming,
)
from ductor_bot.orchestrator.registry import OrchestratorResult
from ductor_bot.session import SessionData
from ductor_bot.session.key import SessionKey


@pytest.fixture
def orch(orch: Orchestrator) -> Orchestrator:
    """Re-export with default mock setup."""
    return orch


def _mock_response(**kwargs: object) -> AgentResponse:
    defaults: dict[str, object] = {
        "result": "Hello from agent",
        "session_id": "sess-123",
        "is_error": False,
        "cost_usd": 0.01,
        "total_tokens": 500,
    }
    defaults.update(kwargs)
    return AgentResponse(**defaults)  # type: ignore[arg-type]


async def _establish_session(orch: Orchestrator) -> None:
    """Run a successful normal() call so the session has a real session_id."""
    mock_exec = AsyncMock(return_value=_mock_response())
    object.__setattr__(orch._cli_service, "execute", mock_exec)
    await normal(orch, SessionKey(chat_id=1), "Setup")


# -- normal flow --


async def test_normal_returns_result(orch: Orchestrator) -> None:
    object.__setattr__(orch._cli_service, "execute", AsyncMock(return_value=_mock_response()))
    result = await normal(orch, SessionKey(chat_id=1), "Hello")
    assert result.text == "Hello from agent"
    assert not result.stream_fallback


async def test_normal_new_session_injects_mainmemory(orch: Orchestrator) -> None:
    orch.paths.mainmemory_path.write_text("# Important Context")
    mock_execute = AsyncMock(return_value=_mock_response())
    object.__setattr__(orch._cli_service, "execute", mock_execute)

    await normal(orch, SessionKey(chat_id=1), "Hello")

    call_args = mock_execute.call_args
    request = call_args[0][0]
    assert request.append_system_prompt is not None
    assert "Important Context" in request.append_system_prompt
    assert request.resume_session is None  # New session


async def test_normal_resume_session_no_append(orch: Orchestrator) -> None:
    mock_execute = AsyncMock(return_value=_mock_response())
    object.__setattr__(orch._cli_service, "execute", mock_execute)

    # First call creates session
    await normal(orch, SessionKey(chat_id=1), "Hello")
    # Second call resumes
    await normal(orch, SessionKey(chat_id=1), "Follow up")

    second_call = mock_execute.call_args_list[1]
    request = second_call[0][0]
    assert request.append_system_prompt is None
    assert request.resume_session is not None


async def test_normal_error_preserves_session(orch: Orchestrator) -> None:
    """On persistent error with existing session: keep session, no auto-retry."""
    # Establish a session first so resume_session is set on subsequent calls
    await _establish_session(orch)

    mock_execute = AsyncMock(
        return_value=_mock_response(is_error=True, result="Rate limited"),
    )
    mock_kill = AsyncMock(return_value=0)
    object.__setattr__(orch._cli_service, "execute", mock_execute)
    object.__setattr__(orch._process_registry, "kill_by_chat_topic", mock_kill)

    result = await normal(orch, SessionKey(chat_id=1), "Hello")
    assert "Session Error" in result.text
    assert "[opus]" in result.text
    assert "/new" in result.text
    assert mock_execute.call_count == 1
    mock_kill.assert_called_once_with(1, None)


async def test_normal_timeout_preserves_session(orch: Orchestrator) -> None:
    """On persistent timeout with existing session: keep session, no auto-retry."""
    await _establish_session(orch)

    mock_execute = AsyncMock(
        return_value=_mock_response(is_error=True, timed_out=True, result=""),
    )
    object.__setattr__(orch._cli_service, "execute", mock_execute)
    object.__setattr__(orch._process_registry, "kill_by_chat_topic", AsyncMock(return_value=0))

    result = await normal(orch, SessionKey(chat_id=1), "Hello")
    assert "Timeout" in result.text
    assert "session has been preserved" in result.text
    assert mock_execute.call_count == 1


async def test_normal_next_message_can_succeed_after_error(orch: Orchestrator) -> None:
    """No auto-retry, but a follow-up user message can succeed with same session."""
    await _establish_session(orch)

    error_resp = _mock_response(is_error=True, result="Temporary error")
    success_resp = _mock_response(result="All good")
    mock_execute = AsyncMock(side_effect=[error_resp, success_resp])
    object.__setattr__(orch._cli_service, "execute", mock_execute)
    object.__setattr__(orch._process_registry, "kill_by_chat_topic", AsyncMock(return_value=0))

    first = await normal(orch, SessionKey(chat_id=1), "Hello")
    second = await normal(orch, SessionKey(chat_id=1), "Hello again")
    assert "Session Error" in first.text
    assert second.text == "All good"
    assert mock_execute.call_count == 2


async def test_normal_model_override(orch: Orchestrator) -> None:
    mock_execute = AsyncMock(return_value=_mock_response())
    object.__setattr__(orch._cli_service, "execute", mock_execute)
    await normal(orch, SessionKey(chat_id=1), "Hello", model_override="sonnet")

    request = mock_execute.call_args[0][0]
    assert request.model_override == "sonnet"


async def test_normal_sigkill_recovers_once_then_succeeds(orch: Orchestrator) -> None:
    """SIGKILL triggers one recovery retry before returning success."""
    sigkill_resp = _mock_response(is_error=True, result="killed", returncode=-9)
    success_resp = _mock_response(result="Recovered")
    mock_execute = AsyncMock(side_effect=[sigkill_resp, success_resp])
    mock_reset_provider = AsyncMock()
    object.__setattr__(orch._cli_service, "execute", mock_execute)
    object.__setattr__(orch._process_registry, "kill_by_chat_topic", AsyncMock(return_value=0))
    object.__setattr__(orch._sessions, "reset_provider_session", mock_reset_provider)

    result = await normal(orch, SessionKey(chat_id=1), "Hello")
    assert result.text == "Recovered"
    assert mock_execute.call_count == 2
    mock_reset_provider.assert_called_once_with(
        SessionKey(chat_id=1), provider="claude", model="opus"
    )


async def test_normal_sigkill_recovers_once_then_asks_user_retry(orch: Orchestrator) -> None:
    """If recovery retry also SIGKILLs, return explicit user guidance."""
    sigkill_resp = _mock_response(is_error=True, result="killed", returncode=-9)
    mock_execute = AsyncMock(side_effect=[sigkill_resp, sigkill_resp])
    mock_reset_provider = AsyncMock()
    object.__setattr__(orch._cli_service, "execute", mock_execute)
    object.__setattr__(orch._process_registry, "kill_by_chat_topic", AsyncMock(return_value=0))
    object.__setattr__(orch._sessions, "reset_provider_session", mock_reset_provider)

    result = await normal(orch, SessionKey(chat_id=1), "Hello")
    assert result.text == "Execution was interrupted. Please send the same request again."
    assert mock_execute.call_count == 2
    mock_reset_provider.assert_called_once_with(
        SessionKey(chat_id=1), provider="claude", model="opus"
    )


async def test_normal_recovery_clears_topic_abort_marker(orch: Orchestrator) -> None:
    """Recovery retry clears only the invoking topic abort marker."""
    key = SessionKey(chat_id=1, topic_id=42)
    sigkill_resp = _mock_response(is_error=True, result="killed", returncode=-9)
    success_resp = _mock_response(result="Recovered")
    mock_execute = AsyncMock(side_effect=[sigkill_resp, success_resp])
    mock_kill = AsyncMock(return_value=1)
    mock_clear_topic_abort = MagicMock()
    mock_reset_provider = AsyncMock()

    object.__setattr__(orch._cli_service, "execute", mock_execute)
    object.__setattr__(orch._process_registry, "kill_by_chat_topic", mock_kill)
    object.__setattr__(orch._process_registry, "clear_topic_abort", mock_clear_topic_abort)
    object.__setattr__(orch._sessions, "reset_provider_session", mock_reset_provider)

    result = await normal(orch, key, "Hello")

    assert result.text == "Recovered"
    mock_kill.assert_awaited_once_with(1, 42)
    mock_clear_topic_abort.assert_called_once_with(1, 42)
    mock_reset_provider.assert_called_once_with(key, provider="claude", model="opus")


async def test_normal_stale_session_recovery_failed_circuit_breaker(orch: Orchestrator) -> None:
    """If the fresh-session retry is ALSO stale, stop cascading and surface
    a clear user-facing error instead of the misleading 'recovered' notice.

    Regression for v0.16.1 MED #8: cap recovery at one retry, log error,
    and return session.recovery_failed rather than prepending recovery
    notice onto another stale-session response.
    """
    stale_resp = _mock_response(
        is_error=True, result="Invalid session ID: session not found", returncode=1
    )
    mock_execute = AsyncMock(side_effect=[stale_resp, stale_resp])
    mock_reset_provider = AsyncMock()
    object.__setattr__(orch._cli_service, "execute", mock_execute)
    object.__setattr__(orch._process_registry, "kill_by_chat_topic", AsyncMock(return_value=0))
    object.__setattr__(orch._sessions, "reset_provider_session", mock_reset_provider)

    result = await normal(orch, SessionKey(chat_id=1), "Hello")
    assert result.text == (
        "Could not recover your session after a fresh start. "
        "Please try again or use /new to start over."
    )
    # Hard cap: one retry. Must be exactly two calls, never cascade further.
    assert mock_execute.call_count == 2
    mock_reset_provider.assert_called_once_with(
        SessionKey(chat_id=1), provider="claude", model="opus"
    )


async def test_normal_does_not_auto_fallback_provider(orch: Orchestrator) -> None:
    mock_execute = AsyncMock(return_value=_mock_response())
    object.__setattr__(orch._cli_service, "execute", mock_execute)
    orch._providers._available_providers = frozenset({"codex"})

    await normal(orch, SessionKey(chat_id=1), "Hello")

    request = mock_execute.call_args[0][0]
    assert request.model_override == "opus"
    assert request.provider_override == "claude"

    session = await orch._sessions.get_active(SessionKey(chat_id=1))
    assert session is not None
    assert session.provider == "claude"
    assert session.model == "opus"


async def test_normal_preserves_existing_session_target_on_restart(orch: Orchestrator) -> None:
    orch._providers._available_providers = frozenset({"codex"})
    await orch._sessions.reset_provider_session(
        SessionKey(chat_id=1),
        provider="gemini",
        model="gemini-3-pro-preview",
    )
    existing = await orch._sessions.get_active(SessionKey(chat_id=1))
    assert existing is not None
    existing.session_id = "sess-gemini-1"
    await orch._sessions.sync_session_target(
        existing,
        provider="gemini",
        model="gemini-3-pro-preview",
    )

    mock_execute = AsyncMock(return_value=_mock_response())
    object.__setattr__(orch._cli_service, "execute", mock_execute)

    orch._providers._gemini_api_key_mode = False
    await normal(orch, SessionKey(chat_id=1), "Hello")

    request = mock_execute.call_args[0][0]
    assert request.provider_override == "gemini"
    assert request.model_override == "gemini-3-pro-preview"


async def test_normal_warns_for_gemini_api_key_mode_without_ductor_key(
    orch: Orchestrator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_execute = AsyncMock(return_value=_mock_response())
    object.__setattr__(orch._cli_service, "execute", mock_execute)
    orch._config.gemini_api_key = "null"

    orch._providers._gemini_api_key_mode = True
    monkeypatch.setattr("ductor_bot.cli.auth.gemini_uses_api_key_mode", lambda: True)
    result = await normal(
        orch, SessionKey(chat_id=1), "Hello", model_override="gemini-3-pro-preview"
    )

    assert "Gemini is set to API-key auth mode" in result.text
    assert "gemini_api_key" in result.text
    mock_execute.assert_not_awaited()


async def test_streaming_warns_for_gemini_api_key_mode_without_ductor_key(
    orch: Orchestrator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock_streaming = AsyncMock(return_value=_mock_response())
    object.__setattr__(orch._cli_service, "execute_streaming", mock_streaming)
    orch._config.gemini_api_key = "null"

    orch._providers._gemini_api_key_mode = True
    monkeypatch.setattr("ductor_bot.cli.auth.gemini_uses_api_key_mode", lambda: True)
    result = await normal_streaming(
        orch, SessionKey(chat_id=1), "Hello", model_override="gemini-3-pro-preview"
    )

    assert "Gemini is set to API-key auth mode" in result.text
    mock_streaming.assert_not_awaited()


async def test_normal_allows_gemini_api_key_mode_with_configured_key(orch: Orchestrator) -> None:
    mock_execute = AsyncMock(return_value=_mock_response(result="Gemini OK"))
    object.__setattr__(orch._cli_service, "execute", mock_execute)
    orch._config.gemini_api_key = "cfg-key-123"

    orch._providers._gemini_api_key_mode = True
    result = await normal(
        orch, SessionKey(chat_id=1), "Hello", model_override="gemini-3-pro-preview"
    )

    assert result.text == "Gemini OK"
    mock_execute.assert_awaited_once()


async def test_normal_recovers_when_gemini_auth_flipped_mid_session(
    orch: Orchestrator,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """User switches Gemini CLI to oauth-personal without restarting ductor.

    The cached api_key_mode is stale (True). When the warning path would fire,
    we re-read settings.json; if the live value is False now, skip the warning
    and let the provider call proceed — no restart required.
    """
    mock_execute = AsyncMock(return_value=_mock_response(result="Gemini OK"))
    object.__setattr__(orch._cli_service, "execute", mock_execute)
    orch._config.gemini_api_key = "null"

    orch._providers._gemini_api_key_mode = True  # stale cache
    monkeypatch.setattr("ductor_bot.cli.auth.gemini_uses_api_key_mode", lambda: False)

    result = await normal(
        orch, SessionKey(chat_id=1), "Hello", model_override="gemini-3-pro-preview"
    )

    assert result.text == "Gemini OK"
    mock_execute.assert_awaited_once()
    assert orch._providers._gemini_api_key_mode is False  # cache refreshed


# -- streaming flow --


async def test_streaming_delegates_correctly(orch: Orchestrator) -> None:
    mock_streaming = AsyncMock(return_value=_mock_response())
    object.__setattr__(orch._cli_service, "execute_streaming", mock_streaming)
    on_delta = AsyncMock()

    result = await normal_streaming(
        orch, SessionKey(chat_id=1), "Hello", cbs=StreamingCallbacks(on_text_delta=on_delta)
    )
    assert result.text == "Hello from agent"
    mock_streaming.assert_called_once()


async def test_streaming_fallback_flag(orch: Orchestrator) -> None:
    object.__setattr__(
        orch._cli_service,
        "execute_streaming",
        AsyncMock(return_value=_mock_response(stream_fallback=True)),
    )
    result = await normal_streaming(orch, SessionKey(chat_id=1), "Hello")
    assert result.stream_fallback is True


async def test_streaming_schedules_memory_flush_without_blocking_reply(
    orch: Orchestrator,
) -> None:
    started = asyncio.Event()
    release = asyncio.Event()

    async def _maybe_flush(_key: SessionKey, _session: SessionData) -> None:
        started.set()
        await release.wait()

    memory_flusher = AsyncMock()
    memory_flusher.maybe_flush = AsyncMock(side_effect=_maybe_flush)
    object.__setattr__(orch, "_memory_flusher", memory_flusher)
    object.__setattr__(
        orch._cli_service,
        "execute_streaming",
        AsyncMock(return_value=_mock_response()),
    )

    result = await asyncio.wait_for(
        normal_streaming(orch, SessionKey(chat_id=1), "Hello"),
        timeout=0.5,
    )

    assert result.text == "Hello from agent"
    await asyncio.wait_for(started.wait(), timeout=0.5)
    release.set()
    await asyncio.sleep(0)
    memory_flusher.maybe_flush.assert_awaited_once()


async def test_streaming_sigkill_recovers_once_then_succeeds(orch: Orchestrator) -> None:
    """Streaming path also retries once after SIGKILL."""
    sigkill_resp = _mock_response(is_error=True, result="killed", returncode=-9)
    success_resp = _mock_response(result="Recovered stream")
    mock_streaming = AsyncMock(side_effect=[sigkill_resp, success_resp])
    mock_reset_provider = AsyncMock()
    object.__setattr__(orch._cli_service, "execute_streaming", mock_streaming)
    object.__setattr__(orch._process_registry, "kill_by_chat_topic", AsyncMock(return_value=0))
    object.__setattr__(orch._sessions, "reset_provider_session", mock_reset_provider)

    result = await normal_streaming(orch, SessionKey(chat_id=1), "Hello")
    assert result.text == "Recovered stream"
    assert mock_streaming.call_count == 2
    mock_reset_provider.assert_called_once_with(
        SessionKey(chat_id=1), provider="claude", model="opus"
    )


async def test_streaming_error_preserves_session(orch: Orchestrator) -> None:
    """Streaming error keeps the session and advises /new."""
    object.__setattr__(
        orch._cli_service,
        "execute_streaming",
        AsyncMock(return_value=_mock_response(is_error=True, result="Stream failed")),
    )
    mock_kill = AsyncMock(return_value=0)
    object.__setattr__(orch._process_registry, "kill_by_chat_topic", mock_kill)

    result = await normal_streaming(orch, SessionKey(chat_id=1), "Hello")
    assert "Session Error" in result.text
    assert "[opus]" in result.text
    mock_kill.assert_called_once_with(1, None)


async def test_streaming_error_with_model_override(orch: Orchestrator) -> None:
    """Streaming error shows the override model name."""
    object.__setattr__(
        orch._cli_service,
        "execute_streaming",
        AsyncMock(return_value=_mock_response(is_error=True, result="Error")),
    )
    object.__setattr__(orch._process_registry, "kill_by_chat_topic", AsyncMock(return_value=0))

    result = await normal_streaming(orch, SessionKey(chat_id=1), "Hello", model_override="sonnet")
    assert "[sonnet]" in result.text


# ---------------------------------------------------------------------------
# Gap 1: _strip_ack_token() unit tests
# ---------------------------------------------------------------------------


def test_strip_ack_token_exact_match() -> None:
    """Exact token match returns empty string."""
    assert _strip_ack_token("HEARTBEAT_OK", "HEARTBEAT_OK") == ""


def test_strip_ack_token_exact_match_with_whitespace() -> None:
    """Token surrounded by whitespace still matches exactly after strip()."""
    assert _strip_ack_token("  HEARTBEAT_OK  ", "HEARTBEAT_OK") == ""


def test_strip_ack_token_starts_with_token() -> None:
    """Token at start is removed, remainder returned."""
    result = _strip_ack_token("HEARTBEAT_OK but also this alert", "HEARTBEAT_OK")
    assert result == "but also this alert"


def test_strip_ack_token_ends_with_token() -> None:
    """Token at end is removed, remainder returned."""
    result = _strip_ack_token("Alert happened HEARTBEAT_OK", "HEARTBEAT_OK")
    assert result == "Alert happened"


def test_strip_ack_token_both_start_and_end() -> None:
    """Token at both start and end -- both are stripped."""
    result = _strip_ack_token("HEARTBEAT_OK middle text HEARTBEAT_OK", "HEARTBEAT_OK")
    assert result == "middle text"


def test_strip_ack_token_mid_text_passthrough() -> None:
    """Token in the middle (not at start or end) passes through unchanged."""
    result = _strip_ack_token("Hello HEARTBEAT_OK World", "HEARTBEAT_OK")
    assert result == "Hello HEARTBEAT_OK World"


def test_strip_ack_token_no_token() -> None:
    """Text without token passes through unchanged."""
    assert _strip_ack_token("Regular alert text", "HEARTBEAT_OK") == "Regular alert text"


def test_strip_ack_token_empty_string() -> None:
    """Empty string returns empty string (not exact match since stripped == token is false)."""
    assert _strip_ack_token("", "HEARTBEAT_OK") == ""


def test_strip_ack_token_custom_token() -> None:
    """Works with a different ack_token value."""
    assert _strip_ack_token("ACK", "ACK") == ""
    assert _strip_ack_token("ACK alert text", "ACK") == "alert text"
    assert _strip_ack_token("alert text ACK", "ACK") == "alert text"


# ---------------------------------------------------------------------------
# Gap 2: _finish_normal() direct unit tests
# ---------------------------------------------------------------------------


def test_finish_normal_happy_path() -> None:
    """Non-error response returns result text."""
    resp = AgentResponse(result="Hello", is_error=False)
    result = _finish_normal(resp)
    assert result.text == "Hello"
    assert not result.stream_fallback


def test_finish_normal_with_stream_fallback() -> None:
    """Non-error response preserves stream_fallback flag."""
    resp = AgentResponse(result="Hello", is_error=False, stream_fallback=True)
    result = _finish_normal(resp)
    assert result.text == "Hello"
    assert result.stream_fallback is True


def test_finish_normal_timed_out() -> None:
    """Timed-out error returns timeout message."""
    resp = AgentResponse(result="", is_error=True, timed_out=True)
    result = _finish_normal(resp)
    assert result.text == "Agent timed out. Please try again."


def test_finish_normal_error_with_details() -> None:
    """Error with non-empty result shows error details."""
    resp = AgentResponse(result="Rate limit exceeded", is_error=True)
    result = _finish_normal(resp)
    assert result.text == "Error: Rate limit exceeded"


def test_finish_normal_error_empty_result() -> None:
    """Error with empty/whitespace result shows generic message."""
    resp = AgentResponse(result="", is_error=True)
    result = _finish_normal(resp)
    assert result.text == "Error: check logs for details."


def test_finish_normal_error_whitespace_only_result() -> None:
    """Error with whitespace-only result shows generic message."""
    resp = AgentResponse(result="   ", is_error=True)
    result = _finish_normal(resp)
    assert result.text == "Error: check logs for details."


def test_finish_normal_error_truncates_long_result() -> None:
    """Error result is truncated to 500 chars."""
    long_msg = "x" * 600
    resp = AgentResponse(result=long_msg, is_error=True)
    result = _finish_normal(resp)
    assert result.text == f"Error: {'x' * 500}"
    assert len(result.text) == 507  # "Error: " (7) + 500


def test_finish_normal_returns_orchestrator_result() -> None:
    """All branches return OrchestratorResult instances."""
    cases = [
        AgentResponse(result="ok", is_error=False),
        AgentResponse(result="", is_error=True, timed_out=True),
        AgentResponse(result="err", is_error=True),
        AgentResponse(result="", is_error=True),
    ]
    for resp in cases:
        result = _finish_normal(resp)
        assert isinstance(result, OrchestratorResult)


# ---------------------------------------------------------------------------
# Gap 3: No auto-retry on resume failure (user-controlled retry)
# ---------------------------------------------------------------------------


async def test_normal_no_auto_retry_on_resume_failure(orch: Orchestrator) -> None:
    """normal() no longer auto-retries on resume failure."""
    # Establish session so resume_session is set
    await _establish_session(orch)

    mock_execute = AsyncMock(
        return_value=_mock_response(is_error=True, result="Resume failed"),
    )
    mock_kill = AsyncMock(return_value=0)
    object.__setattr__(orch._cli_service, "execute", mock_execute)
    object.__setattr__(orch._process_registry, "kill_by_chat_topic", mock_kill)

    await normal(orch, SessionKey(chat_id=1), "Hello")
    assert mock_execute.call_count == 1


async def test_streaming_no_auto_retry_on_resume_failure(orch: Orchestrator) -> None:
    """normal_streaming() no longer auto-retries on resume failure."""
    # Establish session so resume_session is set
    mock_exec = AsyncMock(return_value=_mock_response())
    object.__setattr__(orch._cli_service, "execute_streaming", mock_exec)
    await normal_streaming(orch, SessionKey(chat_id=1), "Setup")

    mock_streaming = AsyncMock(
        return_value=_mock_response(is_error=True, result="Resume failed"),
    )
    mock_kill = AsyncMock(return_value=0)
    object.__setattr__(orch._cli_service, "execute_streaming", mock_streaming)
    object.__setattr__(orch._process_registry, "kill_by_chat_topic", mock_kill)

    await normal_streaming(orch, SessionKey(chat_id=1), "Hello")
    assert mock_streaming.call_count == 1


async def test_normal_no_retry_on_new_session_error(orch: Orchestrator) -> None:
    """normal() does NOT retry when error occurs on a brand-new session (no resume)."""
    mock_execute = AsyncMock(
        return_value=_mock_response(is_error=True, result="Error"),
    )
    mock_kill = AsyncMock(return_value=0)
    object.__setattr__(orch._cli_service, "execute", mock_execute)
    object.__setattr__(orch._process_registry, "kill_by_chat_topic", mock_kill)

    await normal(orch, SessionKey(chat_id=1), "Hello")
    # No resume_session on new session, so no retry -- execute called once
    assert mock_execute.call_count == 1


async def test_streaming_no_retry_on_new_session_error(orch: Orchestrator) -> None:
    """normal_streaming() does NOT retry when error occurs on a brand-new session."""
    mock_streaming = AsyncMock(
        return_value=_mock_response(is_error=True, result="Error"),
    )
    mock_kill = AsyncMock(return_value=0)
    object.__setattr__(orch._cli_service, "execute_streaming", mock_streaming)
    object.__setattr__(orch._process_registry, "kill_by_chat_topic", mock_kill)

    await normal_streaming(orch, SessionKey(chat_id=1), "Hello")
    # No resume_session on new session, so no retry -- called once
    assert mock_streaming.call_count == 1


# ---------------------------------------------------------------------------
# Gap 4: _update_session() session_id change
# ---------------------------------------------------------------------------


async def test_update_session_changes_session_id(orch: Orchestrator) -> None:
    """When CLI returns a different session_id, _update_session updates it."""
    session = SessionData(session_id="old-sess", chat_id=1)
    response = AgentResponse(
        result="ok",
        session_id="new-sess",
        cost_usd=0.01,
        total_tokens=100,
    )
    await _update_session(orch, session, response)
    assert session.session_id == "new-sess"


async def test_update_session_preserves_same_session_id(orch: Orchestrator) -> None:
    """When CLI returns the same session_id, it stays unchanged."""
    session = SessionData(session_id="same-sess", chat_id=1)
    response = AgentResponse(
        result="ok",
        session_id="same-sess",
        cost_usd=0.01,
        total_tokens=100,
    )
    await _update_session(orch, session, response)
    assert session.session_id == "same-sess"


async def test_update_session_no_session_id_in_response(orch: Orchestrator) -> None:
    """When CLI returns no session_id, the original is preserved."""
    session = SessionData(session_id="original", chat_id=1)
    response = AgentResponse(
        result="ok",
        session_id=None,
        cost_usd=0.0,
        total_tokens=0,
    )
    await _update_session(orch, session, response)
    assert session.session_id == "original"


# ---------------------------------------------------------------------------
# Gap 5: Abort discards response -- /stop should suppress ALL output
# ---------------------------------------------------------------------------


async def test_normal_abort_skips_retry(orch: Orchestrator) -> None:
    """When process is aborted (via /stop), normal() returns empty instead of retrying."""
    await _establish_session(orch)

    mock_execute = AsyncMock(
        return_value=_mock_response(is_error=True, result="killed"),
    )
    object.__setattr__(orch._cli_service, "execute", mock_execute)
    orch._process_registry._aborted.add(1)

    result = await normal(orch, SessionKey(chat_id=1), "Hello")
    assert result.text == ""
    assert mock_execute.call_count == 1  # No retry


async def test_topic_abort_skips_recovery(orch: Orchestrator) -> None:
    """Topic-scoped /stop returns empty instead of session recovery."""
    key = SessionKey(chat_id=1, topic_id=42)
    mock_execute = AsyncMock(
        return_value=_mock_response(is_error=True, result="killed", returncode=-9),
    )
    object.__setattr__(orch._cli_service, "execute", mock_execute)
    orch._process_registry._aborted_topics.add((1, 42))

    result = await normal(orch, key, "Hello")
    assert result.text == ""
    assert mock_execute.call_count == 1


async def test_topic_abort_error_response_skips_topic_reset(orch: Orchestrator) -> None:
    """Non-SIGKILL error after a topic /stop must not fall through to reset."""
    key = SessionKey(chat_id=1, topic_id=42)
    mock_execute = AsyncMock(
        return_value=_mock_response(is_error=True, result="boom", returncode=1),
    )
    object.__setattr__(orch._cli_service, "execute", mock_execute)
    kill_by_topic = AsyncMock(return_value=0)
    object.__setattr__(orch._process_registry, "kill_by_chat_topic", kill_by_topic)
    orch._process_registry._aborted_topics.add((1, 42))

    result = await normal(orch, key, "Hello")
    assert result.text == ""
    kill_by_topic.assert_not_awaited()


async def test_streaming_abort_skips_retry(orch: Orchestrator) -> None:
    """When process is aborted (via /stop), normal_streaming() returns empty instead of retrying."""
    mock_exec = AsyncMock(return_value=_mock_response())
    object.__setattr__(orch._cli_service, "execute_streaming", mock_exec)
    await normal_streaming(orch, SessionKey(chat_id=1), "Setup")

    mock_streaming = AsyncMock(
        return_value=_mock_response(is_error=True, result="killed"),
    )
    object.__setattr__(orch._cli_service, "execute_streaming", mock_streaming)
    orch._process_registry._aborted.add(1)

    result = await normal_streaming(orch, SessionKey(chat_id=1), "Hello")
    assert result.text == ""
    assert mock_streaming.call_count == 1  # No retry


async def test_normal_abort_discards_successful_response(orch: Orchestrator) -> None:
    """Even when CLI responds successfully, abort flag causes empty result."""
    mock_execute = AsyncMock(
        return_value=_mock_response(is_error=False, result="Agent replied"),
    )
    object.__setattr__(orch._cli_service, "execute", mock_execute)
    orch._process_registry._aborted.add(1)

    result = await normal(orch, SessionKey(chat_id=1), "Hello")
    assert result.text == ""


async def test_named_session_topic_abort(orch: Orchestrator) -> None:
    """Named-session follow-up honors topic-scoped abort markers."""
    ns = orch._named_sessions.create(1, "claude", "opus", "Setup")
    orch._named_sessions.update_after_response(1, ns.name, "sess-named")
    mock_execute = AsyncMock(return_value=_mock_response(result="Agent replied"))
    object.__setattr__(orch._cli_service, "execute", mock_execute)
    orch._process_registry._aborted_topics.add((1, 42))

    result = await named_session_flow(orch, SessionKey(chat_id=1, topic_id=42), ns.name, "Hello")
    assert result.text == ""
    assert ns.status == "idle"


async def test_named_session_flow_uses_ns_process_label(orch: Orchestrator) -> None:
    ns = orch._named_sessions.create(1, "claude", "opus", "Setup")
    orch._named_sessions.update_after_response(1, ns.name, "sess-named")
    mock_execute = AsyncMock(return_value=_mock_response(result="Agent replied"))
    object.__setattr__(orch._cli_service, "execute", mock_execute)

    await named_session_flow(orch, SessionKey(chat_id=1, topic_id=42), ns.name, "Hello")

    request = mock_execute.call_args[0][0]
    assert request.process_label == f"ns:{ns.name}"


async def test_streaming_abort_discards_successful_response(orch: Orchestrator) -> None:
    """Even when streaming CLI responds successfully, abort flag causes empty result."""
    mock_streaming = AsyncMock(
        return_value=_mock_response(is_error=False, result="Agent replied via stream"),
    )
    object.__setattr__(orch._cli_service, "execute_streaming", mock_streaming)
    orch._process_registry._aborted.add(1)

    result = await normal_streaming(orch, SessionKey(chat_id=1), "Hello")
    assert result.text == ""


async def test_normal_abort_on_new_session_returns_empty(orch: Orchestrator) -> None:
    """Abort on a new session (no resume) also returns empty, not reset error."""
    mock_execute = AsyncMock(
        return_value=_mock_response(is_error=True, result="killed"),
    )
    object.__setattr__(orch._cli_service, "execute", mock_execute)
    object.__setattr__(orch._process_registry, "kill_by_chat_topic", AsyncMock(return_value=0))
    orch._process_registry._aborted.add(1)

    result = await normal(orch, SessionKey(chat_id=1), "Hello")
    assert result.text == ""


def test_is_invalid_session_matches_no_conversation_found() -> None:
    """#81: Claude CLI post-update error 'No conversation found with session ID'
    must be detected as a stale session so recovery/retry can kick in.

    Regression for the marker extension in ``_INVALID_SESSION_MARKERS``."""
    from ductor_bot.orchestrator.flows import _is_invalid_session

    response = AgentResponse(
        result="No conversation found with session ID: abc123",
        is_error=True,
    )
    assert _is_invalid_session(response) is True


def test_is_invalid_session_case_insensitive() -> None:
    """#81 defensive: marker match is lowercase-normalized."""
    from ductor_bot.orchestrator.flows import _is_invalid_session

    response = AgentResponse(
        result="NO CONVERSATION FOUND with session ID: XYZ",
        is_error=True,
    )
    assert _is_invalid_session(response) is True


def test_is_invalid_session_matches_codex_resume_rollout_error() -> None:
    """Codex can report stale resume state as a missing rollout/thread resume failure."""
    from ductor_bot.orchestrator.flows import _is_invalid_session

    response = AgentResponse(
        result="Error: thread/resume failed: no rollout found for thread id abc",
        is_error=True,
    )
    assert _is_invalid_session(response) is True


def test_finish_normal_substitutes_empty_success_with_fallback() -> None:
    """#84: successful turn with empty result -- e.g. agent spent the turn
    writing to memory -- must yield a non-empty visible status message so
    Telegram's send_rich doesn't silently drop the message."""
    from ductor_bot.i18n import t
    from ductor_bot.orchestrator.flows import _finish_normal

    response = AgentResponse(result="", is_error=False)
    result = _finish_normal(response)
    assert result.text == t("session.empty_turn")
    assert result.text  # non-empty


def test_finish_normal_whitespace_only_substitutes_fallback() -> None:
    """#84 defensive: whitespace-only result is also treated as empty."""
    from ductor_bot.i18n import t
    from ductor_bot.orchestrator.flows import _finish_normal

    response = AgentResponse(result="   \n  \t", is_error=False)
    result = _finish_normal(response)
    assert result.text == t("session.empty_turn")


def test_finish_normal_non_empty_success_unchanged() -> None:
    """#84 non-regression: non-empty successful response passes through."""
    from ductor_bot.orchestrator.flows import _finish_normal

    response = AgentResponse(result="Hello world", is_error=False)
    result = _finish_normal(response)
    assert result.text == "Hello world"


# -- append_system_prompt_files injection --


async def test_normal_injects_appended_files_every_turn(orch: Orchestrator) -> None:
    orch._config.append_system_prompt_files = ["PERSONA.md"]
    (orch.paths.workspace / "PERSONA.md").write_text("You are helpful.")
    captured: list[object] = []

    async def mock_execute(req: object) -> AgentResponse:
        captured.append(req)
        return _mock_response()

    object.__setattr__(orch._cli_service, "execute", mock_execute)
    await normal(orch, SessionKey(chat_id=1), "Hello")  # new session
    await normal(orch, SessionKey(chat_id=1), "Again")  # resume session

    assert len(captured) == 2
    for req in captured:
        assert req.append_system_prompt is not None  # type: ignore[attr-defined]
        assert "You are helpful." in req.append_system_prompt  # type: ignore[attr-defined]


async def test_normal_no_files_configured_leaves_resume_append_none(orch: Orchestrator) -> None:
    # Default config (empty list) -> behavior unchanged: resume turn has no append.
    mock_execute = AsyncMock(return_value=_mock_response())
    object.__setattr__(orch._cli_service, "execute", mock_execute)
    await normal(orch, SessionKey(chat_id=1), "Hello")
    await normal(orch, SessionKey(chat_id=1), "Again")
    request = mock_execute.call_args[0][0]
    assert request.append_system_prompt is None


async def test_heartbeat_excludes_appended_files(orch: Orchestrator) -> None:
    """Regression: the heartbeat path must NOT inject append_system_prompt_files."""
    orch._config.append_system_prompt_files = ["PERSONA.md"]
    (orch.paths.workspace / "PERSONA.md").write_text("You are helpful.")
    orch._config.heartbeat.cooldown_minutes = 0
    await _establish_session(orch)

    mock_execute = AsyncMock(return_value=_mock_response(result="HEARTBEAT_OK"))
    object.__setattr__(orch._cli_service, "execute", mock_execute)
    await heartbeat_flow(orch, SessionKey(chat_id=1))

    assert mock_execute.await_count == 1
    request = mock_execute.await_args[0][0]
    assert request.append_system_prompt is None


# -- per-session effort: capture-on-first-use + creation-time fixed ----------


async def test_effort_passed_as_override_and_captured(orch: Orchestrator) -> None:
    """A normal turn captures the global default effort into the session and
    passes it as effort_override."""
    orch._config.reasoning_effort = "high"
    mock_execute = AsyncMock(return_value=_mock_response())
    object.__setattr__(orch._cli_service, "execute", mock_execute)

    await normal(orch, SessionKey(chat_id=1), "Hello")

    request = mock_execute.call_args[0][0]
    assert request.effort_override == "high"
    session = await orch._sessions.get_active(SessionKey(chat_id=1))
    assert session is not None
    assert session.reasoning_effort == "high"


async def test_existing_topic_effort_fixed_when_global_default_changes(
    orch: Orchestrator,
) -> None:
    """An existing topic session keeps its captured effort; a NEW topic inherits
    the (changed) global default."""
    mock_execute = AsyncMock(return_value=_mock_response())
    object.__setattr__(orch._cli_service, "execute", mock_execute)

    orch._config.reasoning_effort = "low"
    existing = SessionKey(chat_id=1, topic_id=10)
    await normal(orch, existing, "first")  # captures "low"

    orch._config.reasoning_effort = "high"  # global default changes
    await normal(orch, existing, "again")  # existing topic must stay "low"
    fresh = SessionKey(chat_id=1, topic_id=20)
    await normal(orch, fresh, "new topic")  # new topic captures "high"

    se = await orch._sessions.get_active(existing)
    sf = await orch._sessions.get_active(fresh)
    assert se is not None
    assert se.reasoning_effort == "low"
    assert sf is not None
    assert sf.reasoning_effort == "high"
