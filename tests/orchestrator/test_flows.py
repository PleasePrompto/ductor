"""Tests for conversation flows."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from ductor_bot.cli.types import AgentResponse
from ductor_bot.orchestrator.core import Orchestrator
from ductor_bot.orchestrator.flows import (
    _finish_normal,
    _strip_ack_token,
    _update_session,
    normal,
    normal_streaming,
)
from ductor_bot.orchestrator.registry import OrchestratorResult
from ductor_bot.session import SessionData


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
    await normal(orch, 1, "Setup")


# -- normal flow --


async def test_normal_returns_result(orch: Orchestrator) -> None:
    object.__setattr__(orch._cli_service, "execute", AsyncMock(return_value=_mock_response()))
    result = await normal(orch, 1, "Hello")
    assert result.text == "Hello from agent"
    assert not result.stream_fallback


async def test_normal_new_session_injects_mainmemory(orch: Orchestrator) -> None:
    orch.paths.mainmemory_path.write_text("# Important Context")
    mock_execute = AsyncMock(return_value=_mock_response())
    object.__setattr__(orch._cli_service, "execute", mock_execute)

    await normal(orch, 1, "Hello")

    call_args = mock_execute.call_args
    request = call_args[0][0]
    assert request.append_system_prompt is not None
    assert "Important Context" in request.append_system_prompt
    assert request.resume_session is None  # New session


async def test_normal_resume_session_no_append(orch: Orchestrator) -> None:
    mock_execute = AsyncMock(return_value=_mock_response())
    object.__setattr__(orch._cli_service, "execute", mock_execute)

    # First call creates session
    await normal(orch, 1, "Hello")
    # Second call resumes
    await normal(orch, 1, "Follow up")

    second_call = mock_execute.call_args_list[1]
    request = second_call[0][0]
    assert request.append_system_prompt is None
    assert request.resume_session is not None


async def test_normal_error_retries_then_resets(orch: Orchestrator) -> None:
    """On persistent error with existing session: retry once, then reset."""
    # Establish a session first so resume_session is set on subsequent calls
    await _establish_session(orch)

    mock_execute = AsyncMock(
        return_value=_mock_response(is_error=True, result="Rate limited"),
    )
    mock_kill = AsyncMock(return_value=0)
    mock_reset = AsyncMock()
    object.__setattr__(orch._cli_service, "execute", mock_execute)
    object.__setattr__(orch._process_registry, "kill_all", mock_kill)
    object.__setattr__(orch._sessions, "reset_session", mock_reset)

    result = await normal(orch, 1, "Hello")
    assert "Session error" in result.text
    assert "[opus]" in result.text
    # First execute fails (resume), reset_session, second _prepare_normal, second execute fails
    assert mock_execute.call_count == 2
    mock_kill.assert_called_once_with(1)


async def test_normal_timeout_retries_then_resets(orch: Orchestrator) -> None:
    """On persistent timeout with existing session: retry once, then reset."""
    await _establish_session(orch)

    mock_execute = AsyncMock(
        return_value=_mock_response(is_error=True, timed_out=True, result=""),
    )
    object.__setattr__(orch._cli_service, "execute", mock_execute)
    object.__setattr__(orch._process_registry, "kill_all", AsyncMock(return_value=0))
    object.__setattr__(orch._sessions, "reset_session", AsyncMock())

    result = await normal(orch, 1, "Hello")
    assert "Session error" in result.text
    assert mock_execute.call_count == 2


async def test_normal_retry_succeeds_on_second_attempt(orch: Orchestrator) -> None:
    """First call errors (resume), second succeeds -- no reset."""
    await _establish_session(orch)

    error_resp = _mock_response(is_error=True, result="Temporary error")
    success_resp = _mock_response(result="All good")
    mock_execute = AsyncMock(side_effect=[error_resp, success_resp])
    object.__setattr__(orch._cli_service, "execute", mock_execute)

    result = await normal(orch, 1, "Hello")
    assert result.text == "All good"
    assert mock_execute.call_count == 2


async def test_normal_model_override(orch: Orchestrator) -> None:
    mock_execute = AsyncMock(return_value=_mock_response())
    object.__setattr__(orch._cli_service, "execute", mock_execute)
    await normal(orch, 1, "Hello", model_override="sonnet")

    request = mock_execute.call_args[0][0]
    assert request.model_override == "sonnet"


async def test_normal_resolves_effective_model_for_available_provider(orch: Orchestrator) -> None:
    mock_execute = AsyncMock(return_value=_mock_response())
    object.__setattr__(orch._cli_service, "execute", mock_execute)
    object.__setattr__(orch, "_available_providers", frozenset({"codex"}))

    await normal(orch, 1, "Hello")

    request = mock_execute.call_args[0][0]
    assert request.model_override == "gpt-5.2-codex"
    assert request.provider_override == "codex"

    session = await orch._sessions.get_active(1)
    assert session is not None
    assert session.provider == "codex"
    assert session.model == "gpt-5.2-codex"


# -- streaming flow --


async def test_streaming_delegates_correctly(orch: Orchestrator) -> None:
    mock_streaming = AsyncMock(return_value=_mock_response())
    object.__setattr__(orch._cli_service, "execute_streaming", mock_streaming)
    on_delta = AsyncMock()

    result = await normal_streaming(orch, 1, "Hello", on_text_delta=on_delta)
    assert result.text == "Hello from agent"
    mock_streaming.assert_called_once()


async def test_streaming_fallback_flag(orch: Orchestrator) -> None:
    object.__setattr__(
        orch._cli_service,
        "execute_streaming",
        AsyncMock(return_value=_mock_response(stream_fallback=True)),
    )
    result = await normal_streaming(orch, 1, "Hello")
    assert result.stream_fallback is True


async def test_streaming_error_resets_session(orch: Orchestrator) -> None:
    """Streaming error triggers session reset."""
    object.__setattr__(
        orch._cli_service,
        "execute_streaming",
        AsyncMock(return_value=_mock_response(is_error=True, result="Stream failed")),
    )
    mock_kill = AsyncMock(return_value=0)
    mock_reset = AsyncMock()
    object.__setattr__(orch._process_registry, "kill_all", mock_kill)
    object.__setattr__(orch._sessions, "reset_session", mock_reset)

    result = await normal_streaming(orch, 1, "Hello")
    assert "Session error" in result.text
    assert "[opus]" in result.text
    mock_kill.assert_called_once_with(1)
    mock_reset.assert_called_once_with(1, provider="claude", model="opus")


async def test_streaming_error_with_model_override(orch: Orchestrator) -> None:
    """Streaming error shows the override model name."""
    object.__setattr__(
        orch._cli_service,
        "execute_streaming",
        AsyncMock(return_value=_mock_response(is_error=True, result="Error")),
    )
    object.__setattr__(orch._process_registry, "kill_all", AsyncMock(return_value=0))
    object.__setattr__(orch._sessions, "reset_session", AsyncMock())

    result = await normal_streaming(orch, 1, "Hello", model_override="sonnet")
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
# Gap 3: Retry symmetry -- both normal and streaming retry on resume failure
# ---------------------------------------------------------------------------


async def test_normal_retries_once_on_resume_failure(orch: Orchestrator) -> None:
    """normal() retries once when resume fails, then resets on second failure."""
    # Establish session so resume_session is set
    await _establish_session(orch)

    mock_execute = AsyncMock(
        return_value=_mock_response(is_error=True, result="Resume failed"),
    )
    mock_kill = AsyncMock(return_value=0)
    object.__setattr__(orch._cli_service, "execute", mock_execute)
    object.__setattr__(orch._process_registry, "kill_all", mock_kill)
    object.__setattr__(orch._sessions, "reset_session", AsyncMock())

    await normal(orch, 1, "Hello")
    # First execute (resume) fails -> reset_session -> _prepare_normal -> second execute fails
    assert mock_execute.call_count == 2


async def test_streaming_retries_once_on_resume_failure(orch: Orchestrator) -> None:
    """normal_streaming() also retries once when resume fails, matching normal()."""
    # Establish session so resume_session is set
    mock_exec = AsyncMock(return_value=_mock_response())
    object.__setattr__(orch._cli_service, "execute_streaming", mock_exec)
    await normal_streaming(orch, 1, "Setup")

    mock_streaming = AsyncMock(
        return_value=_mock_response(is_error=True, result="Resume failed"),
    )
    mock_kill = AsyncMock(return_value=0)
    object.__setattr__(orch._cli_service, "execute_streaming", mock_streaming)
    object.__setattr__(orch._process_registry, "kill_all", mock_kill)
    object.__setattr__(orch._sessions, "reset_session", AsyncMock())

    await normal_streaming(orch, 1, "Hello")
    # Streaming has the same retry-on-resume pattern as normal
    assert mock_streaming.call_count == 2


async def test_normal_no_retry_on_new_session_error(orch: Orchestrator) -> None:
    """normal() does NOT retry when error occurs on a brand-new session (no resume)."""
    mock_execute = AsyncMock(
        return_value=_mock_response(is_error=True, result="Error"),
    )
    mock_kill = AsyncMock(return_value=0)
    object.__setattr__(orch._cli_service, "execute", mock_execute)
    object.__setattr__(orch._process_registry, "kill_all", mock_kill)
    object.__setattr__(orch._sessions, "reset_session", AsyncMock())

    await normal(orch, 1, "Hello")
    # No resume_session on new session, so no retry -- execute called once
    assert mock_execute.call_count == 1


async def test_streaming_no_retry_on_new_session_error(orch: Orchestrator) -> None:
    """normal_streaming() does NOT retry when error occurs on a brand-new session."""
    mock_streaming = AsyncMock(
        return_value=_mock_response(is_error=True, result="Error"),
    )
    mock_kill = AsyncMock(return_value=0)
    object.__setattr__(orch._cli_service, "execute_streaming", mock_streaming)
    object.__setattr__(orch._process_registry, "kill_all", mock_kill)
    object.__setattr__(orch._sessions, "reset_session", AsyncMock())

    await normal_streaming(orch, 1, "Hello")
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

    result = await normal(orch, 1, "Hello")
    assert result.text == ""
    assert mock_execute.call_count == 1  # No retry


async def test_streaming_abort_skips_retry(orch: Orchestrator) -> None:
    """When process is aborted (via /stop), normal_streaming() returns empty instead of retrying."""
    mock_exec = AsyncMock(return_value=_mock_response())
    object.__setattr__(orch._cli_service, "execute_streaming", mock_exec)
    await normal_streaming(orch, 1, "Setup")

    mock_streaming = AsyncMock(
        return_value=_mock_response(is_error=True, result="killed"),
    )
    object.__setattr__(orch._cli_service, "execute_streaming", mock_streaming)
    orch._process_registry._aborted.add(1)

    result = await normal_streaming(orch, 1, "Hello")
    assert result.text == ""
    assert mock_streaming.call_count == 1  # No retry


async def test_normal_abort_discards_successful_response(orch: Orchestrator) -> None:
    """Even when CLI responds successfully, abort flag causes empty result."""
    mock_execute = AsyncMock(
        return_value=_mock_response(is_error=False, result="Agent replied"),
    )
    object.__setattr__(orch._cli_service, "execute", mock_execute)
    orch._process_registry._aborted.add(1)

    result = await normal(orch, 1, "Hello")
    assert result.text == ""


async def test_streaming_abort_discards_successful_response(orch: Orchestrator) -> None:
    """Even when streaming CLI responds successfully, abort flag causes empty result."""
    mock_streaming = AsyncMock(
        return_value=_mock_response(is_error=False, result="Agent replied via stream"),
    )
    object.__setattr__(orch._cli_service, "execute_streaming", mock_streaming)
    orch._process_registry._aborted.add(1)

    result = await normal_streaming(orch, 1, "Hello")
    assert result.text == ""


async def test_normal_abort_on_new_session_returns_empty(orch: Orchestrator) -> None:
    """Abort on a new session (no resume) also returns empty, not reset error."""
    mock_execute = AsyncMock(
        return_value=_mock_response(is_error=True, result="killed"),
    )
    object.__setattr__(orch._cli_service, "execute", mock_execute)
    object.__setattr__(orch._process_registry, "kill_all", AsyncMock(return_value=0))
    object.__setattr__(orch._sessions, "reset_session", AsyncMock())
    orch._process_registry._aborted.add(1)

    result = await normal(orch, 1, "Hello")
    assert result.text == ""
