"""Error-handling tests: keep sessions on CLI errors and avoid auto-retries."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

from ductor_bot.cli.types import AgentResponse
from ductor_bot.orchestrator.core import Orchestrator
from ductor_bot.orchestrator.flows import normal, normal_streaming


def _mock_response(**kwargs: Any) -> AgentResponse:
    defaults: dict[str, Any] = {
        "result": "Hello from agent",
        "session_id": "sess-123",
        "is_error": False,
        "cost_usd": 0.01,
        "total_tokens": 500,
    }
    defaults.update(kwargs)
    return AgentResponse(**defaults)


async def _establish_session(orch: Orchestrator, sid: str = "sess-keep") -> None:
    object.__setattr__(
        orch._cli_service, "execute", AsyncMock(return_value=_mock_response(session_id=sid))
    )
    await normal(orch, 1, "Setup")


async def test_error_preserves_session_id(orch: Orchestrator) -> None:
    await _establish_session(orch, sid="sess-keep")

    object.__setattr__(
        orch._cli_service,
        "execute",
        AsyncMock(
            return_value=_mock_response(is_error=True, result="Token limit", session_id="sess-keep")
        ),
    )
    object.__setattr__(orch._process_registry, "kill_all", AsyncMock(return_value=0))

    result = await normal(orch, 1, "Fail once")

    assert "Session Error" in result.text
    session = await orch._sessions.get_active(1)
    assert session is not None
    assert session.session_id == "sess-keep"
    assert session.message_count == 1


async def test_error_message_contains_new_hint(orch: Orchestrator) -> None:
    object.__setattr__(
        orch._cli_service,
        "execute",
        AsyncMock(return_value=_mock_response(is_error=True, result="Error")),
    )
    object.__setattr__(orch._process_registry, "kill_all", AsyncMock(return_value=0))

    result = await normal(orch, 1, "Broken")

    assert "Your session has been preserved" in result.text
    assert "Use /new" in result.text


async def test_no_auto_retry_on_resume_failure(orch: Orchestrator) -> None:
    await _establish_session(orch, sid="sess-keep")

    mock_execute = AsyncMock(return_value=_mock_response(is_error=True, result="Resume failed"))
    object.__setattr__(orch._cli_service, "execute", mock_execute)
    object.__setattr__(orch._process_registry, "kill_all", AsyncMock(return_value=0))

    await normal(orch, 1, "Retry")

    assert mock_execute.call_count == 1
    request = mock_execute.call_args[0][0]
    assert request.resume_session == "sess-keep"


async def test_next_message_after_error_resumes_same_session(orch: Orchestrator) -> None:
    await _establish_session(orch, sid="sess-keep")

    error_resp = _mock_response(is_error=True, result="Resume failed", session_id="sess-keep")
    success_resp = _mock_response(result="Recovered", session_id="sess-keep")
    mock_execute = AsyncMock(side_effect=[error_resp, success_resp])
    object.__setattr__(orch._cli_service, "execute", mock_execute)
    object.__setattr__(orch._process_registry, "kill_all", AsyncMock(return_value=0))

    first = await normal(orch, 1, "Flaky")
    second = await normal(orch, 1, "Try again")

    assert "Session Error" in first.text
    assert second.text == "Recovered"
    second_request = mock_execute.call_args_list[1][0][0]
    assert second_request.resume_session == "sess-keep"

    session = await orch._sessions.get_active(1)
    assert session is not None
    assert session.session_id == "sess-keep"
    assert session.message_count == 2


async def test_sigkill_still_auto_recovers(orch: Orchestrator) -> None:
    sigkill_resp = _mock_response(is_error=True, result="killed", returncode=-9)
    success_resp = _mock_response(result="Recovered", session_id="sess-keep")
    mock_execute = AsyncMock(side_effect=[sigkill_resp, success_resp])
    mock_reset_provider = AsyncMock()

    object.__setattr__(orch._cli_service, "execute", mock_execute)
    object.__setattr__(orch._process_registry, "kill_all", AsyncMock(return_value=0))
    object.__setattr__(orch._sessions, "reset_provider_session", mock_reset_provider)

    result = await normal(orch, 1, "Run")

    assert result.text == "Recovered"
    assert mock_execute.call_count == 2
    mock_reset_provider.assert_called_once_with(1, provider="claude", model="opus")


async def test_sigkill_resets_only_affected_provider(orch: Orchestrator) -> None:
    await _establish_session(orch, sid="claude-sid")

    codex, _ = await orch._sessions.resolve_session(1, provider="codex", model="gpt-5.2-codex")
    codex.session_id = "codex-sid"
    await orch._sessions.update_session(codex)

    sigkill_resp = _mock_response(is_error=True, result="killed", returncode=-9)
    object.__setattr__(
        orch._cli_service, "execute", AsyncMock(side_effect=[sigkill_resp, sigkill_resp])
    )
    object.__setattr__(orch._process_registry, "kill_all", AsyncMock(return_value=0))

    result = await normal(orch, 1, "Run")

    assert "Execution was interrupted" in result.text
    session = await orch._sessions.get_active(1)
    assert session is not None
    assert session.provider == "claude"
    assert session.session_id == ""
    assert "claude" not in session.provider_sessions
    assert session.provider_sessions["codex"].session_id == "codex-sid"


async def test_streaming_error_preserves_session(orch: Orchestrator) -> None:
    await _establish_session(orch, sid="sess-keep")

    mock_streaming = AsyncMock(return_value=_mock_response(is_error=True, result="Stream error"))
    object.__setattr__(orch._cli_service, "execute_streaming", mock_streaming)
    object.__setattr__(orch._process_registry, "kill_all", AsyncMock(return_value=0))

    result = await normal_streaming(orch, 1, "Broken stream")

    assert "Session Error" in result.text
    assert mock_streaming.call_count == 1

    session = await orch._sessions.get_active(1)
    assert session is not None
    assert session.session_id == "sess-keep"


async def test_streaming_no_auto_retry_on_resume_failure(orch: Orchestrator) -> None:
    await _establish_session(orch, sid="sess-keep")

    mock_streaming = AsyncMock(return_value=_mock_response(is_error=True, result="Resume failed"))
    object.__setattr__(orch._cli_service, "execute_streaming", mock_streaming)
    object.__setattr__(orch._process_registry, "kill_all", AsyncMock(return_value=0))

    await normal_streaming(orch, 1, "Retry")

    assert mock_streaming.call_count == 1
    request = mock_streaming.call_args[0][0]
    assert request.resume_session == "sess-keep"
