"""Convert legacy result types to Envelope.

Each function maps a domain-specific result type to a unified
:class:`~ductor_bot.bus.envelope.Envelope` with the correct delivery,
lock, and injection flags.  The original result types are NOT replaced;
observers keep producing them and these adapters convert at the boundary.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from ductor_bot.bus.envelope import DeliveryMode, Envelope, LockMode, Origin

if TYPE_CHECKING:
    from ductor_bot.background.models import BackgroundResult
    from ductor_bot.multiagent.bus import AsyncInterAgentResult
    from ductor_bot.tasks.models import TaskResult
    from ductor_bot.webhook.models import WebhookResult


# -- Background tasks ----------------------------------------------------------


def from_background_result(result: BackgroundResult) -> Envelope:
    """Convert a ``BackgroundResult`` (named session or stateless)."""
    return Envelope(
        origin=Origin.BACKGROUND,
        chat_id=result.chat_id,
        topic_id=result.thread_id,
        transport=result.transport,
        prompt_preview=result.prompt_preview,
        result_text=result.result_text,
        status=result.status,
        is_error=result.status.startswith("error:"),
        delivery=DeliveryMode.UNICAST,
        lock_mode=LockMode.NONE,
        reply_to_message_id=result.message_id,
        thread_id=result.thread_id,
        elapsed_seconds=result.elapsed_seconds,
        provider=result.provider,
        model=result.model,
        session_name=result.session_name,
        session_id=result.session_id,
        metadata={"task_id": result.task_id},
    )


# -- Cron jobs -----------------------------------------------------------------


def from_cron_result(  # noqa: PLR0913
    title: str,
    result: str,
    status: str,
    *,
    chat_id: int = 0,
    topic_id: int | None = None,
    transport: str = "tg",
) -> Envelope:
    """Convert a cron job result (title, text, status triple).

    When *chat_id* is non-zero the envelope is unicast to that chat/topic.
    Otherwise it broadcasts to all users (legacy behaviour).
    """
    if chat_id:
        return Envelope(
            origin=Origin.CRON,
            chat_id=chat_id,
            topic_id=topic_id,
            transport=transport,
            result_text=result,
            status=status,
            delivery=DeliveryMode.UNICAST,
            lock_mode=LockMode.NONE,
            metadata={"title": title},
        )
    return Envelope(
        origin=Origin.CRON,
        chat_id=0,
        transport=transport,
        result_text=result,
        status=status,
        delivery=DeliveryMode.BROADCAST,
        lock_mode=LockMode.NONE,
        metadata={"title": title},
    )


# -- Heartbeat ----------------------------------------------------------------


def from_heartbeat(
    chat_id: int,
    text: str,
    topic_id: int | None = None,
    *,
    transport: str = "tg",
) -> Envelope:
    """Convert a heartbeat alert."""
    return Envelope(
        origin=Origin.HEARTBEAT,
        chat_id=chat_id,
        topic_id=topic_id,
        transport=transport,
        result_text=text,
        status="success",
        delivery=DeliveryMode.UNICAST,
        lock_mode=LockMode.NONE,
    )


# -- Webhooks ------------------------------------------------------------------


def from_webhook_cron_result(result: WebhookResult) -> Envelope:
    """Convert a webhook cron_task result (broadcast)."""
    return Envelope(
        origin=Origin.WEBHOOK_CRON,
        chat_id=0,
        result_text=result.result_text,
        status=result.status,
        delivery=DeliveryMode.BROADCAST,
        lock_mode=LockMode.NONE,
        metadata={
            "hook_id": result.hook_id,
            "hook_title": result.hook_title,
        },
    )


def from_webhook_wake(chat_id: int, prompt: str) -> Envelope:
    """Convert a webhook wake request (acquires lock, executes via orchestrator)."""
    return Envelope(
        origin=Origin.WEBHOOK_WAKE,
        chat_id=chat_id,
        prompt=prompt,
        delivery=DeliveryMode.UNICAST,
        lock_mode=LockMode.REQUIRED,
    )


# -- Inter-agent ---------------------------------------------------------------


def build_interagent_injection_prompt(
    result: AsyncInterAgentResult,
    *,
    agent_name: str,
    transport_label: str,
) -> str:
    """Build the prompt injected into the recipient agent's session.

    Used when an async inter-agent task returns a successful result.
    ``transport_label`` describes where the user reaches the recipient
    (e.g. ``"Telegram chat"``, ``"Matrix room"``).  Returns ``""`` when
    ``result.success`` is False — callers should treat an empty string as
    "no injection, deliver raw text only".
    """
    if not result.success:
        return ""
    recipient = result.recipient or result.sender
    session_hint = (
        f"\nThe recipient processed this in session `{result.session_name}`. "
        f"The user can continue this session in the recipient's {transport_label} "
        f"via `@{result.session_name} <message>`."
        if result.session_name
        else ""
    )
    task_context = (
        f"\n\nOriginal task you sent to '{recipient}':\n{result.original_message}"
        if result.original_message
        else ""
    )
    return (
        f"[ASYNC INTER-AGENT RESPONSE from '{recipient}'"
        f" (task {result.task_id})]\n"
        f"{result.result_text}\n"
        f"[END ASYNC INTER-AGENT RESPONSE]{session_hint}{task_context}\n\n"
        f"You are agent '{agent_name}'. Process this response from agent "
        f"'{recipient}' and communicate the relevant results to the user "
        f"in your {transport_label}."
    )


def from_interagent_result(
    result: AsyncInterAgentResult,
    chat_id: int,
    *,
    injection_prompt: str = "",
    transport: str | None = None,
) -> Envelope:
    """Convert an async inter-agent result.

    Uses ``result.chat_id`` / ``result.topic_id`` when available so that
    results are routed back to the originating group topic.  Falls back to
    the sender's default *chat_id*.

    Error results are delivered without lock or injection.
    Success results acquire the lock and inject into the active session.
    ``injection_prompt`` must be supplied by the caller (e.g.
    ``app.on_async_interagent_result``) so that ``bus._process`` can
    actually invoke the CLI injection step.  When empty, injection is
    skipped and only the raw ``result_text`` is delivered.
    """
    delivery_chat_id = result.chat_id or chat_id
    delivery_transport = transport or getattr(result, "transport", "") or "tg"
    meta = {
        "task_id": result.task_id,
        "sender": result.sender,
        "recipient": result.recipient,
        "error": result.error,
        "provider_switch_notice": result.provider_switch_notice,
        "original_message": result.original_message,
        "transport": delivery_transport,
    }

    if not result.success:
        return Envelope(
            origin=Origin.INTERAGENT,
            chat_id=delivery_chat_id,
            topic_id=result.topic_id,
            transport=delivery_transport,
            prompt_preview=result.message_preview,
            result_text=result.result_text,
            status="error",
            is_error=True,
            delivery=DeliveryMode.UNICAST,
            lock_mode=LockMode.NONE,
            elapsed_seconds=result.elapsed_seconds,
            session_name=result.session_name,
            metadata=meta,
        )

    return Envelope(
        origin=Origin.INTERAGENT,
        chat_id=delivery_chat_id,
        topic_id=result.topic_id,
        transport=delivery_transport,
        prompt=injection_prompt,
        prompt_preview=result.message_preview,
        result_text=result.result_text,
        status="success",
        delivery=DeliveryMode.UNICAST,
        lock_mode=LockMode.REQUIRED,
        needs_injection=bool(injection_prompt),
        elapsed_seconds=result.elapsed_seconds,
        session_name=result.session_name,
        metadata=meta,
    )


# -- Task results & questions --------------------------------------------------


def from_task_result(result: TaskResult) -> Envelope:
    """Convert a background task result.

    done/failed: acquire lock, inject into parent session.
    cancelled/timeout: unicast notification only (no injection).
    """
    needs_inject = result.status in ("done", "failed")
    prompt = _build_task_injection_prompt(result) if needs_inject else ""
    return Envelope(
        origin=Origin.TASK_RESULT,
        chat_id=result.chat_id,
        topic_id=result.thread_id,
        prompt=prompt,
        prompt_preview=result.prompt_preview,
        result_text=result.result_text,
        status=result.status,
        is_error=result.status == "failed",
        delivery=DeliveryMode.UNICAST,
        lock_mode=LockMode.REQUIRED if needs_inject else LockMode.NONE,
        needs_injection=needs_inject,
        elapsed_seconds=result.elapsed_seconds,
        provider=result.provider,
        model=result.model,
        session_id=result.session_id,
        metadata={
            "task_id": result.task_id,
            "name": result.name,
            "parent_agent": result.parent_agent,
            "error": result.error,
            "task_folder": result.task_folder,
        },
    )


def _build_task_injection_prompt(result: TaskResult) -> str:
    """Build the prompt injected into the parent agent's session."""
    task_id = result.task_id
    if result.status in ("failed", "timeout"):
        return (
            f"[BACKGROUND TASK FAILED: task_id='{task_id}' name='{result.name}']\n"
            f"Error: {result.error}\n"
            f"Provider: {result.provider}/{result.model} | "
            f"Duration: {result.elapsed_seconds:.0f}s\n\n"
            f"Original task: {result.original_prompt}\n\n"
            f"Inform the user that the background task '{result.name}' failed "
            f"and suggest next steps."
        )
    return (
        f"[BACKGROUND TASK COMPLETED: task_id='{task_id}' name='{result.name}']\n"
        f"Provider: {result.provider}/{result.model} | "
        f"Duration: {result.elapsed_seconds:.0f}s\n\n"
        f"{result.result_text}\n\n"
        f"[END TASK RESULT]\n\n"
        f"Original task: {result.original_prompt}\n\n"
        f"Review this result critically:\n"
        f"- Does it fully answer the original task?\n"
        f"- Is anything missing, incomplete, or unclear?\n"
        f"- If yes → resume the task with a follow-up "
        f"(see resume_task.py command above)\n"
        f"- If the result is complete → summarize findings for the user\n"
    )


def from_task_question(
    task_id: str,
    question: str,
    prompt_preview: str,
    chat_id: int,
    *,
    topic_id: int | None = None,
) -> Envelope:
    """Convert a task question (worker asks parent agent)."""
    return Envelope(
        origin=Origin.TASK_QUESTION,
        chat_id=chat_id,
        topic_id=topic_id,
        prompt=question,
        prompt_preview=prompt_preview,
        delivery=DeliveryMode.UNICAST,
        lock_mode=LockMode.REQUIRED,
        needs_injection=True,
        metadata={"task_id": task_id},
    )


# -- User / API messages (audit only) -----------------------------------------


def from_user_message(
    chat_id: int,
    text: str,
    *,
    topic_id: int | None = None,
    source: Origin = Origin.USER,
) -> Envelope:
    """Create an envelope for a user/API message (audit tracking only)."""
    return Envelope(
        origin=source,
        chat_id=chat_id,
        topic_id=topic_id,
        prompt=text,
        prompt_preview=text[:80] if text else "",
        delivery=DeliveryMode.UNICAST,
        lock_mode=LockMode.NONE,
    )
