"""Core conversation flows: normal message handling with session management."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from ductor_bot.cli.stream_events import ToolUseEvent
from ductor_bot.cli.timeout_controller import TimeoutConfig as TCConfig
from ductor_bot.cli.timeout_controller import TimeoutController
from ductor_bot.cli.types import AgentRequest, AgentResponse
from ductor_bot.config import NULLISH_TEXT_VALUES, resolve_timeout
from ductor_bot.i18n import t
from ductor_bot.infra.inflight import InflightTurn
from ductor_bot.log_context import set_log_context
from ductor_bot.orchestrator.hooks import HookContext
from ductor_bot.orchestrator.registry import OrchestratorResult
from ductor_bot.session import SessionData, SessionKey
from ductor_bot.text.response_format import session_error_text, timeout_error_text
from ductor_bot.workspace.loader import build_appended_files_block, read_mainmemory

if TYPE_CHECKING:
    from ductor_bot.orchestrator.core import Orchestrator

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class StreamingCallbacks:
    """Bundle of optional streaming callbacks passed through flow functions."""

    on_text_delta: Callable[[str], Awaitable[None]] | None = field(default=None)
    on_thinking_delta: Callable[[str], Awaitable[None]] | None = field(default=None)
    on_tool_activity: Callable[[ToolUseEvent], Awaitable[None]] | None = field(default=None)
    on_system_status: Callable[[str | None], Awaitable[None]] | None = field(default=None)
    on_reasoning_delta: Callable[[str], Awaitable[None]] | None = field(default=None)


def _consume_background_result(task: asyncio.Task[None]) -> None:
    """Consume task cancellation so fire-and-forget tasks stay quiet."""
    with contextlib.suppress(asyncio.CancelledError):
        task.result()


def _schedule_memory_flush(orch: Orchestrator, key: SessionKey, session: SessionData) -> None:
    """Run memory maintenance after the current locked user turn can release."""
    flusher = orch._memory_flusher
    if flusher is None:
        return

    async def _run() -> None:
        try:
            await flusher.maybe_flush(key, session)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Memory flush task failed chat=%d", key.chat_id)

    topic = "main" if key.topic_id is None else str(key.topic_id)
    task = asyncio.create_task(_run(), name=f"memory-flush:{key.chat_id}:{topic}")
    task.add_done_callback(_consume_background_result)


def _make_timeout_controller(orch: Orchestrator, kind: str) -> TimeoutController | None:
    """Create a TimeoutController when extended timeout features are configured."""
    cfg = orch._config.timeouts
    if not cfg.warning_intervals and not cfg.extend_on_activity:
        return None
    return TimeoutController(
        TCConfig(
            timeout_seconds=resolve_timeout(orch._config, kind),
            warning_intervals=cfg.warning_intervals,
            extend_on_activity=cfg.extend_on_activity,
            activity_extension=cfg.activity_extension,
            max_extensions=cfg.max_extensions,
        ),
    )


async def _prepare_normal(
    orch: Orchestrator,
    key: SessionKey,
    text: str,
    *,
    model_override: str | None = None,
) -> tuple[AgentRequest, SessionData]:
    """Shared setup for normal() and normal_streaming().

    Returns (request, session) so the caller can update the session after the CLI call.
    """
    requested_model = model_override or orch._config.model
    req_model, req_provider = orch.resolve_runtime_target(requested_model)
    requested_effort = orch._config.reasoning_effort

    session, is_new = await orch._sessions.resolve_session(
        key,
        provider=req_provider,
        model=req_model,
        reasoning_effort=requested_effort,
        preserve_existing_target=model_override is None,
    )
    req_model = session.model
    req_provider = session.provider
    req_effort = session.reasoning_effort
    await orch._sessions.sync_session_target(
        session,
        provider=req_provider,
        model=req_model,
        reasoning_effort=req_effort,
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

        roster = _build_agent_roster(orch)
        if roster:
            append_prompt = f"{append_prompt}\n\n{roster}" if append_prompt else roster

    files_block = await build_appended_files_block(
        orch.paths, orch._config.append_system_prompt_files
    )
    if files_block:
        append_prompt = f"{append_prompt}\n\n{files_block}" if append_prompt else files_block

    hook_ctx = HookContext(
        chat_id=key.chat_id,
        message_count=session.message_count,
        is_new_session=is_new,
        provider=req_provider,
        model=req_model,
    )
    prompt = orch._hook_registry.apply(text, hook_ctx)

    timeout_secs = resolve_timeout(orch._config, "normal")
    request = AgentRequest(
        prompt=prompt,
        append_system_prompt=append_prompt,
        model_override=req_model,
        provider_override=req_provider,
        effort_override=req_effort,
        chat_id=key.chat_id,
        topic_id=key.topic_id,
        transport=key.transport,
        resume_session=None if is_new else session.session_id,
        timeout_seconds=timeout_secs,
        timeout_controller=_make_timeout_controller(orch, "normal"),
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


async def _preserve_session_from_response(
    orch: Orchestrator,
    session: SessionData,
    response: AgentResponse,
    *,
    reason: str,
) -> None:
    """Persist a first response session id even when the turn ends early."""
    if response.session_id and not session.session_id:
        logger.debug("%s: preserving session_id %s for resume", reason, response.session_id[:8])
        session.session_id = response.session_id
    elif response.session_id and response.session_id != session.session_id:
        logger.debug(
            "%s: keeping existing session_id %s over response session_id %s",
            reason,
            session.session_id[:8],
            response.session_id[:8],
        )
    await orch._sessions.preserve_session_identity(session)


async def _reset_on_error(
    orch: Orchestrator,
    key: SessionKey,
    *,
    model_name: str,
    provider_name: str,
    cli_detail: str = "",
) -> OrchestratorResult:
    """Kill processes, preserve session, return user-facing error."""
    await orch._process_registry.kill_by_chat_topic(key.chat_id, key.topic_id)
    logger.warning("Session error preserved model=%s provider=%s", model_name, provider_name)
    return OrchestratorResult(
        text=session_error_text(model_name, cli_detail),
    )


async def _handle_timeout(
    orch: Orchestrator,
    key: SessionKey,
    session: SessionData,
    response: AgentResponse,
    request: AgentRequest,
) -> OrchestratorResult:
    """Preserve session after timeout and return a clear user-facing message.

    Unlike ``_reset_on_error``, this persists the session_id from the response
    so that the next user message can ``--resume`` the timed-out session.
    """
    model_name, _provider_name = _request_target(orch, request)
    await orch._process_registry.kill_by_chat_topic(key.chat_id, key.topic_id)

    # Persist the session_id captured from SystemInitEvent so resume works.
    if response.session_id and response.session_id != session.session_id:
        logger.info(
            "Timeout: preserving session_id %s for resume",
            response.session_id[:8],
        )
        session.session_id = response.session_id
    await orch._sessions.update_session(
        session, cost_usd=response.cost_usd, tokens=response.total_tokens
    )

    timeout_s = request.timeout_seconds or 0
    logger.warning("Session timed out after %.0fs model=%s", timeout_s, model_name)
    return OrchestratorResult(text=timeout_error_text(model_name, timeout_s))


def _sigkill_user_msg() -> str:
    return t("session.sigkill")


def _session_recovered_msg() -> str:
    return t("session.recovered")


def _session_recovery_failed_msg() -> str:
    return t("session.recovery_failed")


def _is_sigkill(response: AgentResponse) -> bool:
    """Return True when the response indicates SIGKILL termination."""
    return response.is_error and response.returncode == -getattr(signal, "SIGKILL", 9)


_INVALID_SESSION_MARKERS = (
    "invalid session",
    "session not found",
    # #81: Claude CLI after a version bump / cache clear emits this phrasing
    # for resumed sessions whose cached session_id has expired. Keeping all
    # known phrasings here lets foreground + inter-agent paths share detection.
    "no conversation found",
    "no rollout found",
    "thread/resume failed",
)


def _is_invalid_session(response: AgentResponse) -> bool:
    """Return True when the CLI rejected a ``--resume`` session ID.

    Happens when sessions created on host are resumed inside Docker
    (or vice-versa) because working directories differ.
    """
    if not response.is_error:
        return False
    lower = (response.result or "").lower()
    return any(marker in lower for marker in _INVALID_SESSION_MARKERS)


def _needs_session_recovery(response: AgentResponse) -> bool:
    """Return True when the response warrants an automatic session reset + retry."""
    return _is_sigkill(response) or _is_invalid_session(response)


@dataclass(slots=True)
class _RecoveryContext:
    """Context for session recovery."""

    reason: str
    model_override: str | None
    streaming: bool = False
    cbs: StreamingCallbacks = field(default_factory=StreamingCallbacks)


@dataclass(slots=True)
class _RecoveryOutcome:
    """Result of the one-shot session-recovery gate.

    ``retry_performed`` is True when a fresh-session retry actually ran.
    ``session_recovered`` is True only when that retry succeeded after an
    invalid-session rejection (used to prepend the user-facing notice).
    ``failed_result`` is non-None when the retry still returned stale-session
    and callers must short-circuit with it (MED #8 circuit breaker).
    """

    request: AgentRequest
    session: SessionData
    response: AgentResponse
    retry_performed: bool
    session_recovered: bool
    failed_result: OrchestratorResult | None


async def _maybe_recover_session(  # noqa: PLR0913
    orch: Orchestrator,
    key: SessionKey,
    text: str,
    request: AgentRequest,
    session: SessionData,
    response: AgentResponse,
    *,
    model_override: str | None,
    streaming: bool = False,
    cbs: StreamingCallbacks | None = None,
) -> _RecoveryOutcome:
    """Run the one-shot recovery gate shared by normal() and normal_streaming().

    If the CLI reported a recoverable failure (SIGKILL or stale session) AND
    the user did not abort/interrupt, retry exactly once with a fresh session.
    If the retry ALSO returns stale-session, emit a clear error and surface
    ``failed_result`` so callers can short-circuit (MED #8 hard cap).
    """
    _reg = orch._process_registry
    if (
        _reg.was_aborted(key.chat_id)
        or _reg.was_aborted_topic(key.chat_id, key.topic_id)
        or _reg.was_interrupted(key.chat_id)
        or not _needs_session_recovery(response)
    ):
        return _RecoveryOutcome(
            request=request,
            session=session,
            response=response,
            retry_performed=False,
            session_recovered=False,
            failed_result=None,
        )

    session_recovered = _is_invalid_session(response)
    reason = "invalid_session" if session_recovered else "sigkill"
    ctx = _RecoveryContext(
        reason=reason,
        model_override=model_override,
        streaming=streaming,
        cbs=cbs or StreamingCallbacks(),
    )
    request, session, response = await _recover_session(orch, key, text, ctx)
    failed_result: OrchestratorResult | None = None
    if _is_invalid_session(response):
        logger.error("Session recovery failed on retry for chat_id=%s", key.chat_id)
        failed_result = OrchestratorResult(text=_session_recovery_failed_msg())
    return _RecoveryOutcome(
        request=request,
        session=session,
        response=response,
        retry_performed=True,
        session_recovered=session_recovered,
        failed_result=failed_result,
    )


async def _recover_session(
    orch: Orchestrator,
    key: SessionKey,
    text: str,
    ctx: _RecoveryContext,
) -> tuple[AgentRequest, SessionData, AgentResponse]:
    """Reset the active provider session and retry once.

    When callbacks are set in *ctx.cbs*, the retry uses streaming execution.
    """
    logger.warning("recovery.%s chat=%s action=retry", ctx.reason, key.chat_id)
    model_name = ctx.model_override or orch._config.model
    provider_name = orch.models.provider_for(model_name)
    await orch._process_registry.kill_by_chat_topic(key.chat_id, key.topic_id)
    orch._process_registry.clear_topic_abort(key.chat_id, key.topic_id)
    await orch._sessions.reset_provider_session(key, provider=provider_name, model=model_name)

    cb = ctx.cbs
    if ctx.reason == "invalid_session" and cb.on_text_delta is not None:
        await cb.on_text_delta(f"{_session_recovered_msg()}\n\n")
    elif cb.on_system_status is not None:
        await cb.on_system_status("recovering")

    request, session = await _prepare_normal(orch, key, text, model_override=ctx.model_override)
    if ctx.streaming:
        response = await orch._cli_service.execute_streaming(
            request,
            on_text_delta=cb.on_text_delta,
            on_thinking_delta=cb.on_thinking_delta,
            on_tool_activity=cb.on_tool_activity,
            on_system_status=cb.on_system_status,
        )
    else:
        response = await orch._cli_service.execute(request)
    return request, session, response


def _request_target(orch: Orchestrator, request: AgentRequest) -> tuple[str, str]:
    """Return the effective model/provider target of a prepared request."""
    model_name = request.model_override or orch._config.model
    provider_name = request.provider_override or orch.models.provider_for(model_name)
    return model_name, provider_name


def _begin_inflight(
    orch: Orchestrator,
    request: AgentRequest,
    session: SessionData,
    *,
    is_recovery: bool = False,
) -> None:
    """Record an in-flight turn for crash recovery."""
    model_name, provider_name = _request_target(orch, request)
    orch._inflight_tracker.begin(
        InflightTurn(
            chat_id=request.chat_id,
            provider=provider_name,
            model=model_name,
            session_id=session.session_id or "",
            prompt_preview=request.prompt[:100],
            started_at=datetime.now(UTC).isoformat(),
            is_recovery=is_recovery,
            path="normal",
        )
    )


async def _gemini_missing_config_key_warning(
    orch: Orchestrator,
    request: AgentRequest,
) -> OrchestratorResult | None:
    """Warn when Gemini API-key mode is selected but Ductor config key is empty."""
    _model_name, provider_name = _request_target(orch, request)
    if provider_name != "gemini":
        return None

    api_key_mode = orch.gemini_api_key_mode
    if not api_key_mode:
        return None

    key = (orch._config.gemini_api_key or "").strip()
    if key and key.lower() not in NULLISH_TEXT_VALUES:
        return None

    # Before emitting the warning, re-read settings.json: the user may have
    # flipped auth mode (e.g. to oauth-personal) since the cache was seeded at
    # startup. This avoids forcing a ductor restart for a simple auth flip.
    if not orch.refresh_gemini_api_key_mode():
        return None

    return OrchestratorResult(text=t("gemini.missing_key"))


async def _finalize_turn(  # noqa: PLR0913
    orch: Orchestrator,
    key: SessionKey,
    request: AgentRequest,
    session: SessionData,
    response: AgentResponse,
    *,
    flow_label: str,
    schedule_memory_flush: bool = False,
    session_recovered: bool = False,
) -> OrchestratorResult:
    """Shared post-execute tail of normal() and normal_streaming().

    Handles abort/interrupt, timeout, and error short-circuits, then updates
    the session and builds the final result. *schedule_memory_flush* is set by
    the streaming flow only; *session_recovered* prepends the recovery notice
    for the non-streaming flow (streaming already emitted it as a text delta).
    """
    _reg = orch._process_registry
    if (
        _reg.was_aborted(key.chat_id)
        or _reg.was_aborted_topic(key.chat_id, key.topic_id)
        or _reg.was_interrupted(key.chat_id)
    ):
        _reg.clear_interrupt(key.chat_id)
        await _preserve_session_from_response(orch, session, response, reason="abort")
        logger.info("%s flow aborted/interrupted by user", flow_label)
        return OrchestratorResult(text="")
    if response.timed_out:
        return await _handle_timeout(orch, key, session, response, request)
    if response.is_error:
        if _is_sigkill(response):
            logger.warning("recovery.sigkill chat=%s action=user-retry", key.chat_id)
            return OrchestratorResult(text=_sigkill_user_msg(), stream_fallback=True)
        model_name, provider_name = _request_target(orch, request)
        await _preserve_session_from_response(orch, session, response, reason="error")
        return await _reset_on_error(
            orch,
            key,
            model_name=model_name,
            provider_name=provider_name,
            cli_detail=response.result,
        )
    await _update_session(orch, session, response)
    if schedule_memory_flush:
        _schedule_memory_flush(orch, key, session)
    logger.info("%s flow completed", flow_label)
    req_model, _prov = _request_target(orch, request)
    result = _finish_normal(
        response, session, orch._config.session_age_warning_hours, model_name=req_model
    )
    if session_recovered:
        result.text = f"{_session_recovered_msg()}\n\n{result.text}"
    return result


async def normal(
    orch: Orchestrator,
    key: SessionKey,
    text: str,
    *,
    model_override: str | None = None,
    is_recovery: bool = False,
) -> OrchestratorResult:
    """Handle normal conversation with session resume."""
    logger.info("Normal flow starting")
    request, session = await _prepare_normal(orch, key, text, model_override=model_override)
    warning = await _gemini_missing_config_key_warning(orch, request)
    if warning is not None:
        logger.warning("Gemini API-key mode without configured ductor key")
        return warning

    _begin_inflight(orch, request, session, is_recovery=is_recovery)
    try:
        response = await orch._cli_service.execute(request)
        outcome = await _maybe_recover_session(
            orch, key, text, request, session, response, model_override=model_override
        )
        if outcome.failed_result is not None:
            return outcome.failed_result
        return await _finalize_turn(
            orch,
            key,
            outcome.request,
            outcome.session,
            outcome.response,
            flow_label="Normal",
            session_recovered=outcome.session_recovered,
        )
    finally:
        orch._inflight_tracker.complete(key.chat_id)


async def normal_streaming(
    orch: Orchestrator,
    key: SessionKey,
    text: str,
    *,
    model_override: str | None = None,
    cbs: StreamingCallbacks | None = None,
) -> OrchestratorResult:
    """Handle normal conversation with streaming output."""
    logger.info("Streaming flow starting")
    request, session = await _prepare_normal(orch, key, text, model_override=model_override)
    warning = await _gemini_missing_config_key_warning(orch, request)
    if warning is not None:
        logger.warning("Gemini API-key mode without configured ductor key")
        return warning

    _begin_inflight(orch, request, session, is_recovery=False)
    try:
        cb = cbs or StreamingCallbacks()

        async def _on_compact() -> None:
            if orch._memory_flusher is not None:
                orch._memory_flusher.mark_boundary(key)

        response = await orch._cli_service.execute_streaming(
            request,
            on_text_delta=cb.on_text_delta,
            on_thinking_delta=cb.on_thinking_delta,
            on_tool_activity=cb.on_tool_activity,
            on_system_status=cb.on_system_status,
            on_reasoning_delta=cb.on_reasoning_delta,
            on_compact_boundary=_on_compact if orch._memory_flusher is not None else None,
        )
        outcome = await _maybe_recover_session(
            orch,
            key,
            text,
            request,
            session,
            response,
            model_override=model_override,
            streaming=True,
            cbs=cb,
        )
        if outcome.failed_result is not None:
            return outcome.failed_result
        return await _finalize_turn(
            orch,
            key,
            outcome.request,
            outcome.session,
            outcome.response,
            flow_label="Streaming",
            schedule_memory_flush=True,
        )
    finally:
        orch._inflight_tracker.complete(key.chat_id)


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
    return "\n\n---\n" + t("session.age_warning", age=age_label)


def _finish_normal(
    response: AgentResponse,
    session: SessionData | None = None,
    warning_hours: int = 0,
    *,
    model_name: str = "",
) -> OrchestratorResult:
    """Post-processing for normal() and normal_streaming()."""
    if response.is_error:
        if response.timed_out:
            return OrchestratorResult(text=t("timeout.generic"))
        if response.result.strip():
            return OrchestratorResult(text=t("error.generic", detail=response.result[:500]))
        return OrchestratorResult(text=t("error.check_logs"))

    # #84: tool-only turns (agent silently updates memory/files without
    # generating text) used to return an empty string here -- Telegram's
    # send_rich then silently dropped the empty message so the user saw
    # nothing. Substitute a neutral status line so every successful turn
    # produces visible output. Strategic memory redesign lives in Phase 4.
    text = response.result if response.result.strip() else t("session.empty_turn")
    if session:
        text += _session_age_note(session, warning_hours)

    return OrchestratorResult(
        text=text,
        stream_fallback=response.stream_fallback,
        model_name=model_name,
        total_tokens=response.total_tokens,
        input_tokens=response.input_tokens,
        cost_usd=response.cost_usd,
        duration_ms=response.duration_ms,
    )


# ---------------------------------------------------------------------------
# Dynamic agent roster
# ---------------------------------------------------------------------------


def _build_agent_roster(orch: Orchestrator) -> str:
    """Build a dynamic agent roster string from the supervisor's bus.

    Returns empty string if no supervisor or only one agent is online.
    """
    supervisor = orch._supervisor
    if supervisor is None:
        return ""

    bus = supervisor.bus
    if bus is None:
        return ""

    agents = bus.list_agents()
    if not agents or len(agents) <= 1:
        return ""

    own_name = orch._cli_service._config.agent_name
    peers = [a for a in agents if a != own_name]

    lines = [
        "## Active Agent Roster",
        f"Your name: `{own_name}`",
        f"Other agents online: {', '.join(f'`{a}`' for a in peers)}",
        "",
        "Use `ask_agent.py` (sync) or `ask_agent_async.py` (async) to communicate.",
    ]
    return "\n".join(lines)


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


async def named_session_flow(
    orch: Orchestrator,
    key: SessionKey,
    session_name: str,
    text: str,
) -> OrchestratorResult:
    """Handle a foreground follow-up to a named session (non-streaming)."""
    ns = orch._named_sessions.get(key.chat_id, session_name)
    if ns is None:
        return OrchestratorResult(text=t("session.not_found", name=session_name))
    if ns.status == "ended":
        return OrchestratorResult(text=t("session.ended", name=session_name))
    if ns.status == "running":
        return OrchestratorResult(text=t("session.still_running", name=session_name))

    tag = f"**[{session_name} | {ns.provider}]**\n"
    orch._named_sessions.mark_running(
        key.chat_id,
        session_name,
        text,
        transport=key.transport,
        topic_id=key.topic_id,
    )
    files_block = await build_appended_files_block(
        orch.paths, orch._config.append_system_prompt_files
    )
    request = AgentRequest(
        prompt=text,
        append_system_prompt=files_block,
        model_override=ns.model,
        provider_override=ns.provider,
        effort_override=ns.reasoning_effort or None,
        chat_id=key.chat_id,
        topic_id=key.topic_id,
        transport=key.transport,
        process_label=f"ns:{session_name}",
        resume_session=ns.session_id or None,
        timeout_seconds=resolve_timeout(orch._config, "normal"),
        timeout_controller=_make_timeout_controller(orch, "normal"),
    )
    response = await orch._cli_service.execute(request)

    _reg = orch._process_registry
    if (
        _reg.was_aborted(key.chat_id)
        or _reg.was_aborted_topic(key.chat_id, key.topic_id)
        or _reg.was_interrupted(key.chat_id)
    ):
        _reg.clear_interrupt(key.chat_id)
        ns.status = "idle"
        return OrchestratorResult(text="")
    if response.is_error:
        ns.status = "idle"
        return OrchestratorResult(text=f"{tag}{t('error.generic', detail=response.result[:500])}")

    orch._named_sessions.update_after_response(key.chat_id, session_name, response.session_id or "")
    return OrchestratorResult(text=f"{tag}{response.result}")


async def named_session_streaming(
    orch: Orchestrator,
    key: SessionKey,
    session_name: str,
    text: str,
    *,
    cbs: StreamingCallbacks | None = None,
) -> OrchestratorResult:
    """Handle a foreground streaming follow-up to a named session."""
    ns = orch._named_sessions.get(key.chat_id, session_name)
    if ns is None:
        return OrchestratorResult(text=t("session.not_found", name=session_name))
    if ns.status == "ended":
        return OrchestratorResult(text=t("session.ended", name=session_name))
    if ns.status == "running":
        return OrchestratorResult(text=t("session.still_running", name=session_name))

    cb = cbs or StreamingCallbacks()
    tag = f"**[{session_name} | {ns.provider}]**\n"
    orch._named_sessions.mark_running(
        key.chat_id,
        session_name,
        text,
        transport=key.transport,
        topic_id=key.topic_id,
    )
    files_block = await build_appended_files_block(
        orch.paths, orch._config.append_system_prompt_files
    )
    request = AgentRequest(
        prompt=text,
        append_system_prompt=files_block,
        model_override=ns.model,
        provider_override=ns.provider,
        effort_override=ns.reasoning_effort or None,
        chat_id=key.chat_id,
        topic_id=key.topic_id,
        transport=key.transport,
        process_label=f"ns:{session_name}",
        resume_session=ns.session_id or None,
        timeout_seconds=resolve_timeout(orch._config, "normal"),
        timeout_controller=_make_timeout_controller(orch, "normal"),
    )

    tag_sent = False

    async def _tagged_text_delta(chunk: str) -> None:
        nonlocal tag_sent
        if cb.on_text_delta is not None:
            if not tag_sent:
                await cb.on_text_delta(tag)
                tag_sent = True
            await cb.on_text_delta(chunk)

    response = await orch._cli_service.execute_streaming(
        request,
        on_text_delta=_tagged_text_delta,
        on_thinking_delta=cb.on_thinking_delta,
        on_tool_activity=cb.on_tool_activity,
        on_system_status=cb.on_system_status,
        on_reasoning_delta=cb.on_reasoning_delta,
    )

    _reg2 = orch._process_registry
    if (
        _reg2.was_aborted(key.chat_id)
        or _reg2.was_aborted_topic(key.chat_id, key.topic_id)
        or _reg2.was_interrupted(key.chat_id)
    ):
        _reg2.clear_interrupt(key.chat_id)
        ns.status = "idle"
        return OrchestratorResult(text="")
    if response.is_error:
        ns.status = "idle"
        return OrchestratorResult(text=f"{tag}{t('error.generic', detail=response.result[:500])}")

    orch._named_sessions.update_after_response(key.chat_id, session_name, response.session_id or "")
    return OrchestratorResult(text=f"{tag}{response.result}")


# ---------------------------------------------------------------------------
# Heartbeat flow
# ---------------------------------------------------------------------------


async def heartbeat_flow(
    orch: Orchestrator,
    key: SessionKey,
    *,
    prompt: str | None = None,
    ack_token: str | None = None,
) -> str | None:
    """Run a heartbeat turn in the existing session.

    Returns the alert text if the model has something to say, or None if the
    response was a HEARTBEAT_OK acknowledgment. Does NOT update session state
    (last_active, message_count) for ack responses.

    *prompt* and *ack_token* override the global heartbeat config when set
    (used by per-target overrides in HeartbeatObserver).
    """
    hb_cfg = orch._config.heartbeat
    effective_prompt = prompt or hb_cfg.prompt
    effective_ack = ack_token or hb_cfg.ack_token
    req_model, req_provider = orch.resolve_runtime_target(orch._config.model)

    # Read-only check: never create/overwrite a session from the heartbeat path.
    session = await orch._sessions.get_active(key)

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
        prompt=effective_prompt,
        model_override=req_model,
        provider_override=req_provider,
        effort_override=session.reasoning_effort or None,
        chat_id=key.chat_id,
        topic_id=key.topic_id,
        transport=key.transport,
        resume_session=session.session_id,
        timeout_seconds=resolve_timeout(orch._config, "normal"),
        timeout_controller=_make_timeout_controller(orch, "normal"),
    )

    response = await orch._cli_service.execute(request)
    if response.is_error:
        logger.warning("Heartbeat CLI error result=%s", response.result[:200])
        return None

    alert_text = _strip_ack_token(response.result, effective_ack)
    if not alert_text:
        logger.info("Heartbeat OK (suppressed)")
        return None

    await _update_session(orch, session, response)
    logger.info("Heartbeat alert chars=%d", len(alert_text))
    return alert_text
