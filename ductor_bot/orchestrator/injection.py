"""Session injection: routes inter-agent messages and task questions through CLIService.

Extracts the common "build prompt → get active session → execute → update"
pattern from the Orchestrator into reusable helpers.

Note: task *results* are injected via the MessageBus (see ``bus.adapters``).
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

from ductor_bot.cli.types import AgentRequest
from ductor_bot.orchestrator.flows import _update_session
from ductor_bot.session.key import SessionKey
from ductor_bot.session.named import NamedSession

if TYPE_CHECKING:
    from ductor_bot.multiagent.bus import AsyncInterAgentResult
    from ductor_bot.orchestrator.core import Orchestrator

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Shared injection helper
# ---------------------------------------------------------------------------


async def _inject_prompt(  # noqa: PLR0913
    orch: Orchestrator,
    prompt: str,
    chat_id: str,
    process_label: str,
    *,
    topic_id: str | None = None,
    transport: str = "tg",
) -> str:
    """Execute *prompt* in the current active session and update session state.

    Shared by ``handle_async_interagent_result`` and ``inject_prompt``.
    """
    key = SessionKey(transport=transport, chat_id=chat_id, topic_id=topic_id)
    active = await orch._sessions.get_active(key)
    resume_id = active.session_id if active else None

    request = AgentRequest(
        prompt=prompt,
        chat_id=chat_id,
        topic_id=topic_id,
        process_label=process_label,
        resume_session=resume_id,
        timeout_seconds=orch._config.cli_timeout,
    )

    response = await orch._cli_service.execute(request)

    if active and response:
        await _update_session(orch, active, response)

    return response.result if response else ""


# ---------------------------------------------------------------------------
# Inter-agent session helpers
# ---------------------------------------------------------------------------


def _interagent_chat_id(orch: Orchestrator) -> str:
    """Return the real Telegram chat_id for inter-agent sessions."""
    if not orch._config.allowed_user_ids:
        logger.warning("No allowed_user_ids configured — inter-agent sessions use chat_id=''")
        return ""
    return orch._config.allowed_user_ids[0]


def _get_or_create_interagent_session(
    orch: Orchestrator,
    sender: str,
    *,
    new_session: bool = False,
) -> tuple[NamedSession, bool, str]:
    """Get or create a Named Session for an inter-agent conversation.

    Uses a deterministic name ``ia-{sender}`` so follow-up messages from
    the same sender automatically resume the same session.

    If *new_session* is True, any existing session for this sender is
    ended first so a fresh one is created.

    If the active provider/model has changed since the session was created,
    the old session is ended automatically (the CLI session ID is not
    portable across providers) and a provider-switch notice is returned.

    Returns ``(session, is_new, provider_switch_notice)``.
    """
    chat_id = _interagent_chat_id(orch)
    session_name = f"ia-{sender}"
    provider_switch_notice = ""

    if new_session and orch._named_sessions.end_session(chat_id, session_name):
        logger.info("Inter-agent session reset: %s (sender=%s)", session_name, sender)

    model_name, provider_name = orch.resolve_runtime_target(orch._config.model)

    ns = orch._named_sessions.get(chat_id, session_name)
    if ns is not None and ns.status != "ended":
        # Detect provider/model mismatch → session ID is not portable
        if ns.provider != provider_name:
            old_provider = ns.provider
            orch._named_sessions.end_session(chat_id, session_name)
            logger.info(
                "Inter-agent session %s reset: provider changed %s -> %s",
                session_name,
                old_provider,
                provider_name,
            )
            provider_switch_notice = (
                f"Agent `{orch._cli_service._config.agent_name}` switched "
                f"provider from `{old_provider}` to `{provider_name}`.\n"
                f"The previous inter-agent session `{session_name}` is no longer "
                f"resumable and has been ended.\n"
                f"A new session `{session_name}` was started with `{provider_name}`."
            )
        else:
            return ns, False, ""

    ns = NamedSession(
        name=session_name,
        chat_id=chat_id,
        provider=provider_name,
        model=model_name,
        session_id="",
        prompt_preview=f"Inter-agent session with {sender}",
        status="running",
        created_at=time.time(),
    )
    orch._named_sessions.add(ns)
    logger.info("Inter-agent named session created: %s (sender=%s)", session_name, sender)
    return ns, True, provider_switch_notice


# ---------------------------------------------------------------------------
# Public handlers (called by Orchestrator as thin delegations)
# ---------------------------------------------------------------------------


async def handle_interagent_message(
    orch: Orchestrator,
    sender: str,
    message: str,
    *,
    new_session: bool = False,
) -> tuple[str, str, str]:
    """Process a message from another agent via the InterAgentBus.

    Uses a Named Session per sender so that context is preserved across
    multiple inter-agent interactions.  The session can also be resumed
    manually from Telegram via ``@ia-{sender} <message>``.

    Returns ``(result_text, session_name, provider_switch_notice)``.
    The *provider_switch_notice* is non-empty when a provider change
    caused an automatic session reset — callers should notify the user.
    """
    own_name = orch._cli_service._config.agent_name
    chat_id = _interagent_chat_id(orch)
    ns, _is_new, provider_switch_notice = _get_or_create_interagent_session(
        orch,
        sender,
        new_session=new_session,
    )

    prompt = (
        f"[INTER-AGENT MESSAGE from '{sender}' to '{own_name}']\n"
        f"{message}\n"
        f"[END INTER-AGENT MESSAGE]\n\n"
        f"You are agent '{own_name}'. Respond to this inter-agent request "
        f"from '{sender}'. Be direct and concise."
    )

    ns.status = "running"
    request = AgentRequest(
        prompt=prompt,
        chat_id=chat_id,
        process_label=f"interagent:{sender}",
        resume_session=ns.session_id or None,
        timeout_seconds=orch._config.cli_timeout,
    )

    try:
        response = await orch._cli_service.execute(request)
    except Exception:
        ns.status = "idle"
        logger.exception("Inter-agent message handling failed (from=%s)", sender)
        return (
            f"Error processing inter-agent message from '{sender}'",
            ns.name,
            provider_switch_notice,
        )
    else:
        if response and response.session_id:
            orch._named_sessions.update_after_response(
                chat_id, ns.name, response.session_id, status="idle"
            )
        else:
            ns.status = "idle"
        return (response.result if response else ""), ns.name, provider_switch_notice


async def handle_async_interagent_result(
    orch: Orchestrator,
    result: AsyncInterAgentResult,
    *,
    chat_id: str = "",
) -> str:
    """Inject an async inter-agent result into the current active session.

    Called when another agent completes an async request we sent.
    Resumes the *current* active session (not the one that was active when
    the task was dispatched) so the agent has full conversation context.

    The prompt is self-contained: it includes both the original task
    description and the sub-agent's response, so the agent can process
    the result even if the session changed (``/new``, provider switch).

    Caller must hold the per-chat lock to prevent concurrent session access.
    """
    own_name = orch._cli_service._config.agent_name
    recipient = result.recipient
    task_id = result.task_id

    session_hint = (
        f"\nThe recipient processed this in session `{result.session_name}`. "
        f"The user can continue this session in the recipient's Telegram chat "
        f"via `@{result.session_name} <message>`."
        if result.session_name
        else ""
    )

    task_context = (
        f"\n\nOriginal task you sent to '{recipient}':\n{result.original_message}"
        if result.original_message
        else ""
    )

    prompt = (
        f"[ASYNC INTER-AGENT RESPONSE from '{recipient}' (task {task_id})]\n"
        f"{result.result_text}\n"
        f"[END ASYNC INTER-AGENT RESPONSE]{session_hint}{task_context}\n\n"
        f"You are agent '{own_name}'. Process this response from agent "
        f"'{recipient}' and communicate the relevant results to the user "
        f"in your Telegram chat."
    )

    logger.debug(
        "Injecting async result into main session: task=%s from=%s "
        "resume_session=%s original_msg_len=%d",
        task_id,
        recipient,
        "<pending>",
        len(result.original_message),
    )

    try:
        return await _inject_prompt(orch, prompt, chat_id, f"interagent-async:{recipient}")
    except Exception:
        logger.exception(
            "Async inter-agent result handling failed (from=%s)",
            recipient,
        )
        return f"Error processing async result from '{recipient}'"
