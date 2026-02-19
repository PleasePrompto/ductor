"""Core conversation flows: normal message handling with session management."""

from __future__ import annotations

import asyncio
import logging
import signal
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from ductor_bot.cli.types import AgentRequest, AgentResponse
from ductor_bot.log_context import set_log_context
from ductor_bot.orchestrator.hooks import HookContext
from ductor_bot.orchestrator.registry import OrchestratorResult
from ductor_bot.session import SessionData
from ductor_bot.workspace.loader import read_mainmemory

if TYPE_CHECKING:
    from ductor_bot.orchestrator.core import Orchestrator

logger = logging.getLogger(__name__)


async def _prepare_normal(
    orch: Orchestrator,
    chat_id: int,
    text: str,
    *,
    model_override: str | None = None,
) -> tuple[AgentRequest, SessionData]:
    """Shared setup for normal() and normal_streaming().

    Returns (request, session) so the caller can update the session after the CLI call.
    """
    requested_model = model_override or orch._config.model
    req_model, req_provider = orch.resolve_runtime_target(requested_model)

    session, is_new = await orch._sessions.resolve_session(
        chat_id,
        provider=req_provider,
        model=req_model,
    )
    await orch._sessions.sync_session_target(
        session,
        provider=req_provider,
        model=req_model,
    )
    if session.session_id:
        set_log_context(session_id=session.session_id)
    logger.info(
        "Session resolved sid=%s new=%s msgs=%d",
        session.session_id[:8] if session.session_id else "<new>",
        is_new,
        session.message_count,
    )

    append_prompt = None
    if is_new:
        mainmemory = await asyncio.to_thread(read_mainmemory, orch.paths)
        if mainmemory.strip():
            append_prompt = mainmemory

    hook_ctx = HookContext(
        chat_id=chat_id,
        message_count=session.message_count,
        is_new_session=is_new,
        provider=req_provider,
        model=req_model,
    )
    prompt = orch._hook_registry.apply(text, hook_ctx)

    request = AgentRequest(
        prompt=prompt,
        append_system_prompt=append_prompt,
        model_override=req_model,
        provider_override=req_provider,
        chat_id=chat_id,
        resume_session=None if is_new else session.session_id,
        timeout_seconds=orch._config.cli_timeout,
    )
    return request, session


async def _update_session(
    orch: Orchestrator, session: SessionData, response: AgentResponse
) -> None:
    """Store the real CLI session_id and update metrics."""
    if response.session_id and response.session_id != session.session_id:
        logger.info(
            "Session ID updated: %s -> %s",
            session.session_id[:8] if session.session_id else "<new>",
            response.session_id[:8],
        )
        session.session_id = response.session_id
    await orch._sessions.update_session(
        session, cost_usd=response.cost_usd, tokens=response.total_tokens
    )


async def _reset_on_error(
    orch: Orchestrator,
    chat_id: int,
    *,
    model_name: str,
    provider_name: str,
) -> OrchestratorResult:
    """Kill processes, reset session, return user-facing error."""
    await orch._process_registry.kill_all(chat_id)
    await orch._sessions.reset_session(chat_id, provider=provider_name, model=model_name)
    logger.warning("Session error reset model=%s", model_name)
    return OrchestratorResult(
        text=f"[{model_name}] Session error. New session started.",
    )


def _sigkill_message(rc: int | None) -> str | None:
    """Return a clear message when CLI subprocess was killed by signal."""
    if rc is None:
        return None
    if rc == -signal.SIGKILL:
        return "Execution was interrupted. Please send the same request again."
    return None


def _is_sigkill(response: AgentResponse) -> bool:
    """Return True when the response indicates SIGKILL termination."""
    return response.is_error and response.returncode == -signal.SIGKILL


async def _recover_after_sigkill(  # noqa: PLR0913
    orch: Orchestrator,
    chat_id: int,
    text: str,
    *,
    model_override: str | None,
    streaming: bool,
    on_text_delta: Callable[[str], Awaitable[None]] | None = None,
    on_tool_activity: Callable[[str], Awaitable[None]] | None = None,
    on_system_status: Callable[[str | None], Awaitable[None]] | None = None,
) -> tuple[AgentRequest, SessionData, AgentResponse]:
    """Reset the session and retry once after SIGKILL."""
    logger.warning("recovery.sigkill chat=%s action=retry", chat_id)
    model_name = model_override or orch._config.model
    provider_name = orch._models.provider_for(model_name)
    await orch._process_registry.kill_all(chat_id)
    await orch._sessions.reset_session(chat_id, provider=provider_name, model=model_name)

    if on_system_status is not None:
        await on_system_status("recovering")

    request, session = await _prepare_normal(orch, chat_id, text, model_override=model_override)
    if streaming:
        response = await orch._cli_service.execute_streaming(
            request,
            on_text_delta=on_text_delta,
            on_tool_activity=on_tool_activity,
            on_system_status=on_system_status,
        )
    else:
        response = await orch._cli_service.execute(request)
    return request, session, response


def _request_target(orch: Orchestrator, request: AgentRequest) -> tuple[str, str]:
    """Return the effective model/provider target of a prepared request."""
    model_name = request.model_override or orch._config.model
    provider_name = request.provider_override or orch._models.provider_for(model_name)
    return model_name, provider_name


async def normal(
    orch: Orchestrator,
    chat_id: int,
    text: str,
    *,
    model_override: str | None = None,
) -> OrchestratorResult:
    """Handle normal conversation with session resume."""
    logger.info("Normal flow starting")
    request, session = await _prepare_normal(orch, chat_id, text, model_override=model_override)
    response = await orch._cli_service.execute(request)
    if orch._process_registry.was_aborted(chat_id):
        logger.info("Normal flow aborted by user")
        return OrchestratorResult(text="")
    if response.is_error and request.resume_session:
        # Resume failed -- reset session and retry with a fresh one.
        logger.warning(
            "Resume failed sid=%s, retrying fresh",
            request.resume_session[:8],
        )
        model_name, provider_name = _request_target(orch, request)
        await orch._sessions.reset_session(chat_id, provider=provider_name, model=model_name)
        request, session = await _prepare_normal(orch, chat_id, text, model_override=model_override)
        response = await orch._cli_service.execute(request)
    if _is_sigkill(response):
        request, session, response = await _recover_after_sigkill(
            orch,
            chat_id,
            text,
            model_override=model_override,
            streaming=False,
        )

    if response.is_error:
        sigmsg = _sigkill_message(response.returncode)
        if sigmsg is not None:
            logger.warning("recovery.sigkill chat=%s action=user-retry", chat_id)
            return OrchestratorResult(text=sigmsg)
        if orch._process_registry.was_aborted(chat_id):
            logger.info("Normal flow aborted by user (after retry)")
            return OrchestratorResult(text="")
        model_name, provider_name = _request_target(orch, request)
        return await _reset_on_error(
            orch,
            chat_id,
            model_name=model_name,
            provider_name=provider_name,
        )
    await _update_session(orch, session, response)
    logger.info("Normal flow completed")
    return _finish_normal(response, session, orch._config.session_age_warning_hours)


async def normal_streaming(  # noqa: PLR0913
    orch: Orchestrator,
    chat_id: int,
    text: str,
    *,
    model_override: str | None = None,
    on_text_delta: Callable[[str], Awaitable[None]] | None = None,
    on_tool_activity: Callable[[str], Awaitable[None]] | None = None,
    on_system_status: Callable[[str | None], Awaitable[None]] | None = None,
) -> OrchestratorResult:
    """Handle normal conversation with streaming output."""
    logger.info("Streaming flow starting")
    request, session = await _prepare_normal(orch, chat_id, text, model_override=model_override)
    response = await orch._cli_service.execute_streaming(
        request,
        on_text_delta=on_text_delta,
        on_tool_activity=on_tool_activity,
        on_system_status=on_system_status,
    )
    if orch._process_registry.was_aborted(chat_id):
        logger.info("Streaming flow aborted by user")
        return OrchestratorResult(text="")
    if response.is_error and request.resume_session:
        # Resume failed -- reset session and retry with a fresh one.
        logger.warning(
            "Resume failed sid=%s, retrying fresh",
            request.resume_session[:8],
        )
        model_name, provider_name = _request_target(orch, request)
        await orch._sessions.reset_session(chat_id, provider=provider_name, model=model_name)
        request, session = await _prepare_normal(orch, chat_id, text, model_override=model_override)
        response = await orch._cli_service.execute_streaming(
            request,
            on_text_delta=on_text_delta,
            on_tool_activity=on_tool_activity,
            on_system_status=on_system_status,
        )
    if _is_sigkill(response):
        request, session, response = await _recover_after_sigkill(
            orch,
            chat_id,
            text,
            model_override=model_override,
            streaming=True,
            on_text_delta=on_text_delta,
            on_tool_activity=on_tool_activity,
            on_system_status=on_system_status,
        )

    if response.is_error:
        sigmsg = _sigkill_message(response.returncode)
        if sigmsg is not None:
            logger.warning("recovery.sigkill chat=%s action=user-retry", chat_id)
            return OrchestratorResult(text=sigmsg)
        if orch._process_registry.was_aborted(chat_id):
            logger.info("Streaming flow aborted by user (after retry)")
            return OrchestratorResult(text="")
        model_name, provider_name = _request_target(orch, request)
        return await _reset_on_error(
            orch,
            chat_id,
            model_name=model_name,
            provider_name=provider_name,
        )
    await _update_session(orch, session, response)
    logger.info("Streaming flow completed")
    return _finish_normal(response, session, orch._config.session_age_warning_hours)


def _session_age_note(session: SessionData, warning_hours: int) -> str:
    """Return a short age warning if the session exceeds the configured threshold."""
    if warning_hours <= 0:
        return ""
    try:
        created = datetime.fromisoformat(session.created_at)
    except (ValueError, TypeError):
        return ""
    age_hours = (datetime.now(UTC) - created).total_seconds() / 3600
    if age_hours < warning_hours:
        return ""
    # Show once every 10 messages to avoid spam.
    if session.message_count % 10 != 0:
        return ""
    age_label = f"{int(age_hours)}h" if age_hours < 48 else f"{int(age_hours / 24)}d"
    return f"\n\n---\n[Session is {age_label} old. Use /new for a fresh start.]"


def _finish_normal(
    response: AgentResponse,
    session: SessionData | None = None,
    warning_hours: int = 0,
) -> OrchestratorResult:
    """Post-processing for normal() and normal_streaming()."""
    if response.is_error:
        if response.timed_out:
            return OrchestratorResult(text="Agent timed out. Please try again.")
        sigmsg = _sigkill_message(response.returncode)
        if sigmsg is not None:
            return OrchestratorResult(text=sigmsg)
        if response.result.strip():
            return OrchestratorResult(text=f"Error: {response.result[:500]}")
        return OrchestratorResult(text="Error: check logs for details.")

    text = response.result
    if session:
        text += _session_age_note(session, warning_hours)

    return OrchestratorResult(
        text=text,
        stream_fallback=response.stream_fallback,
    )


# ---------------------------------------------------------------------------
# Heartbeat flow
# ---------------------------------------------------------------------------


def _strip_ack_token(text: str, token: str) -> str:
    """Remove leading/trailing ack token from response text."""
    stripped = text.strip()
    if stripped == token:
        return ""
    if stripped.startswith(token):
        stripped = stripped[len(token) :].strip()
    if stripped.endswith(token):
        stripped = stripped[: -len(token)].strip()
    return stripped


async def heartbeat_flow(orch: Orchestrator, chat_id: int) -> str | None:
    """Run a heartbeat turn in the existing session.

    Returns the alert text if the model has something to say, or None if the
    response was a HEARTBEAT_OK acknowledgment. Does NOT update session state
    (last_active, message_count) for ack responses.
    """
    hb_cfg = orch._config.heartbeat
    req_model, req_provider = orch.resolve_runtime_target(orch._config.model)

    # Read-only check: never create/overwrite a session from the heartbeat path.
    session = await orch._sessions.get_active(chat_id)

    if not session or not session.session_id:
        logger.debug("Heartbeat skipped: no active session")
        return None

    set_log_context(session_id=session.session_id)

    if session.provider != req_provider:
        logger.debug(
            "Heartbeat skipped: provider mismatch session_provider=%s current=%s",
            session.provider,
            req_provider,
        )
        return None

    await orch._sessions.sync_session_target(session, model=req_model)

    idle_seconds = (datetime.now(UTC) - datetime.fromisoformat(session.last_active)).total_seconds()
    cooldown_seconds = hb_cfg.cooldown_minutes * 60
    if idle_seconds < cooldown_seconds:
        logger.debug(
            "Heartbeat skipped: idle=%ds cooldown=%ds",
            int(idle_seconds),
            cooldown_seconds,
        )
        return None

    request = AgentRequest(
        prompt=hb_cfg.prompt,
        model_override=req_model,
        provider_override=req_provider,
        chat_id=chat_id,
        resume_session=session.session_id,
        timeout_seconds=orch._config.cli_timeout,
    )

    response = await orch._cli_service.execute(request)
    if response.is_error:
        logger.warning("Heartbeat CLI error result=%s", response.result[:200])
        return None

    alert_text = _strip_ack_token(response.result, hb_cfg.ack_token)
    if not alert_text:
        logger.info("Heartbeat OK (suppressed)")
        return None

    await _update_session(orch, session, response)
    logger.info("Heartbeat alert chars=%d", len(alert_text))
    return alert_text
