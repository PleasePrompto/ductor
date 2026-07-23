"""InterAgentBus: in-memory async message passing between agents."""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ductor_bot.i18n import t
from ductor_bot.session.named import interagent_session_name

if TYPE_CHECKING:
    from ductor_bot.multiagent.stack import AgentStack

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 300.0  # 5 minutes — for synchronous sends
_ASYNC_TIMEOUT = 3600.0  # 1 hour — async tasks may run complex multi-step work


@dataclass(slots=True)
class InterAgentResponse:
    """Response from an inter-agent message."""

    sender: str
    text: str
    success: bool = True
    error: str | None = None


@dataclass(frozen=True, slots=True)
class AsyncSendOptions:
    """Optional metadata for :meth:`InterAgentBus.send_async`.

    Bundles keyword-only parameters that control session handling and
    Telegram routing so the public API stays within the argument limit.

    *new_session*: end any existing inter-agent session before processing.
    *summary*: notification preview shown in the recipient's Telegram chat.
    *chat_id* / *topic_id*: originating transport chat/topic context so
    that results are delivered back to the correct thread.
    *transport*: short transport id (``"tg"`` / ``"mx"``) for multi-transport
    result routing.
    """

    new_session: bool = False
    summary: str = ""
    chat_id: int = 0
    topic_id: int | None = None
    transport: str = ""
    # #86: when non-empty, the async result handler is resolved against this
    # agent name instead of ``sender`` — lets cross-server SSH pipelines
    # route replies to a specific agent even when ``sender`` is "unknown".
    reply_to: str = ""
    # #86: suppress the ``_notify_recipient`` "Async task received from X"
    # broadcast; useful in automated code-review-style pipelines where the
    # user only wants to see the final result.
    silent: bool = False


@dataclass(slots=True)
class AsyncInterAgentTask:
    """Tracks an in-flight async inter-agent request."""

    task_id: str
    sender: str
    recipient: str
    message: str
    new_session: bool = False
    summary: str = ""
    timestamp: float = field(default_factory=time.time)
    asyncio_task: asyncio.Task[None] | None = field(default=None, repr=False)
    chat_id: int = 0
    topic_id: int | None = None
    transport: str = ""
    reply_to: str = ""  # #86
    silent: bool = False  # #86


@dataclass(slots=True)
class AsyncInterAgentResult:
    """Result delivered to the sender agent after async processing completes."""

    task_id: str
    sender: str
    recipient: str
    message_preview: str
    result_text: str
    success: bool = True
    error: str | None = None
    elapsed_seconds: float = 0.0
    session_name: str = ""
    provider_switch_notice: str = ""
    original_message: str = ""
    chat_id: int = 0
    topic_id: int | None = None
    transport: str = ""
    reply_to: str = ""  # #86 — overrides sender for handler lookup when set


AsyncResultCallback = Callable[["AsyncInterAgentResult"], Awaitable[None]]


def _make_result(  # noqa: PLR0913  -- result fields have natural multi-arity
    task: AsyncInterAgentTask,
    t0: float,
    *,
    result_text: str = "",
    success: bool = True,
    error: str | None = None,
    session_name: str = "",
    provider_switch_notice: str = "",
) -> AsyncInterAgentResult:
    """Build an :class:`AsyncInterAgentResult` carrying the task's routing fields."""
    return AsyncInterAgentResult(
        task_id=task.task_id,
        sender=task.sender,
        recipient=task.recipient,
        message_preview=task.message[:60],
        result_text=result_text,
        success=success,
        error=error,
        elapsed_seconds=time.time() - t0,
        session_name=session_name,
        provider_switch_notice=provider_switch_notice,
        original_message=task.message,
        chat_id=task.chat_id,
        topic_id=task.topic_id,
        transport=task.transport,
        reply_to=task.reply_to,
    )


class InterAgentBus:
    """In-memory async bus for agent-to-agent communication.

    All agents in the same process share this bus. Messages are handled
    by calling the target agent's Orchestrator.handle_interagent_message().
    """

    def __init__(self) -> None:
        self._agents: dict[str, AgentStack] = {}
        self._async_tasks: dict[str, AsyncInterAgentTask] = {}
        self._async_result_handlers: dict[str, AsyncResultCallback] = {}

    def register(self, name: str, stack: AgentStack) -> None:
        """Register an agent on the bus."""
        self._agents[name] = stack
        logger.debug("Bus: registered agent '%s'", name)

    def unregister(self, name: str) -> None:
        """Unregister an agent from the bus."""
        self._agents.pop(name, None)
        logger.debug("Bus: unregistered agent '%s'", name)

    def list_agents(self) -> list[str]:
        """List all registered agent names."""
        return list(self._agents.keys())

    async def send(  # noqa: PLR0913
        self,
        sender: str,
        recipient: str,
        message: str,
        *,
        send_timeout: float = _DEFAULT_TIMEOUT,
        new_session: bool = False,
        chat_id: int = 0,
        topic_id: int | None = None,
    ) -> InterAgentResponse:
        """Send a message to another agent and wait for the response.

        The target agent's Orchestrator runs a one-shot CLI turn to process
        the message. Returns the response text or an error.
        """
        if recipient not in self._agents:
            available = ", ".join(self._agents.keys()) or "(none)"
            return InterAgentResponse(
                sender=recipient,
                text="",
                success=False,
                error=f"Agent '{recipient}' not found. Available: {available}",
            )

        target = self._agents[recipient]
        logger.info("Bus: %s -> %s (%d chars)", sender, recipient, len(message))

        try:
            orch = target.bot.orchestrator
            if orch is None:
                return InterAgentResponse(
                    sender=recipient,
                    text="",
                    success=False,
                    error=f"Agent '{recipient}' orchestrator not initialized",
                )

            if chat_id or topic_id is not None:
                execution = orch.handle_interagent_message(
                    sender,
                    message,
                    new_session=new_session,
                    source_chat_id=chat_id,
                    source_topic_id=topic_id,
                )
            else:
                execution = orch.handle_interagent_message(
                    sender,
                    message,
                    new_session=new_session,
                )
            result_text, _session_name, _notice = await asyncio.wait_for(
                execution,
                timeout=send_timeout,
            )
            logger.info(
                "Bus: %s -> %s completed (%d chars response)",
                sender,
                recipient,
                len(result_text),
            )
            return InterAgentResponse(sender=recipient, text=result_text)

        except TimeoutError:
            logger.warning("Bus: %s -> %s timed out after %.0fs", sender, recipient, send_timeout)
            return InterAgentResponse(
                sender=recipient,
                text="",
                success=False,
                error=f"Timeout after {send_timeout:.0f}s",
            )
        except Exception as exc:
            logger.exception("Bus: %s -> %s failed", sender, recipient)
            return InterAgentResponse(
                sender=recipient,
                text="",
                success=False,
                error=f"{type(exc).__name__}: {exc}",
            )

    # -- Async (fire-and-forget) communication ---------------------------------

    def set_async_result_handler(
        self,
        agent_name: str,
        handler: AsyncResultCallback,
    ) -> None:
        """Register callback for delivering async results back to a sender agent."""
        self._async_result_handlers[agent_name] = handler

    def send_async(
        self,
        sender: str,
        recipient: str,
        message: str,
        *,
        opts: AsyncSendOptions | None = None,
    ) -> str | None:
        """Send a message to another agent asynchronously.

        Returns a task_id immediately. The response will be delivered to the
        sender agent's registered callback when the target agent finishes.
        Returns None if the recipient is not found.

        Optional *opts* controls session handling and Telegram routing.
        See :class:`AsyncSendOptions` for details.
        """
        if recipient not in self._agents:
            return None

        o = opts or AsyncSendOptions()
        task_id = secrets.token_hex(6)
        task = AsyncInterAgentTask(
            task_id=task_id,
            sender=sender,
            recipient=recipient,
            message=message,
            new_session=o.new_session,
            summary=o.summary,
            chat_id=o.chat_id,
            topic_id=o.topic_id,
            transport=o.transport,
            reply_to=o.reply_to,
            silent=o.silent,
        )
        atask = asyncio.create_task(
            self._run_async(task),
            name=f"ia-async:{sender}->{recipient}:{task_id}",
        )
        task.asyncio_task = atask
        atask.add_done_callback(lambda _: self._async_tasks.pop(task_id, None))
        self._async_tasks[task_id] = task

        logger.info(
            "Bus async: %s -> %s task=%s (%d chars)",
            sender,
            recipient,
            task_id,
            len(message),
        )
        return task_id

    async def _run_async(self, task: AsyncInterAgentTask) -> None:
        """Execute the async inter-agent message and deliver result to sender."""
        t0 = time.time()
        try:
            target = self._agents[task.recipient]
            orch = target.bot.orchestrator
            if orch is None:
                await self._deliver_async_result(
                    _make_result(
                        task,
                        t0,
                        success=False,
                        error=f"Agent '{task.recipient}' orchestrator not initialized",
                    )
                )
                return

            # Notify the recipient agent's Telegram chat about the incoming task
            await self._notify_recipient(task)

            if task.chat_id or task.topic_id is not None:
                execution = orch.handle_interagent_message(
                    task.sender,
                    task.message,
                    new_session=task.new_session,
                    source_chat_id=task.chat_id,
                    source_topic_id=task.topic_id,
                )
            else:
                execution = orch.handle_interagent_message(
                    task.sender,
                    task.message,
                    new_session=task.new_session,
                )
            result_text, session_name, provider_notice = await asyncio.wait_for(
                execution,
                timeout=_ASYNC_TIMEOUT,
            )
            logger.info(
                "Bus async: %s -> %s task=%s session=%s completed (%d chars, %.1fs)",
                task.sender,
                task.recipient,
                task.task_id,
                session_name,
                len(result_text),
                time.time() - t0,
            )
            await self._deliver_async_result(
                _make_result(
                    task,
                    t0,
                    result_text=result_text,
                    session_name=session_name,
                    provider_switch_notice=provider_notice,
                )
            )

        except TimeoutError:
            logger.warning(
                "Bus async: %s -> %s task=%s timed out",
                task.sender,
                task.recipient,
                task.task_id,
            )
            await self._deliver_async_result(
                _make_result(
                    task,
                    t0,
                    success=False,
                    error=f"Timeout after {_ASYNC_TIMEOUT:.0f}s",
                )
            )

        except Exception as exc:
            logger.exception("Bus async: %s -> %s failed", task.sender, task.recipient)
            await self._deliver_async_result(
                _make_result(
                    task,
                    t0,
                    success=False,
                    error=f"{type(exc).__name__}: {exc}",
                )
            )

    async def _notify_recipient(self, task: AsyncInterAgentTask) -> None:
        """Send a short notification to the recipient agent's chat.

        This makes async task delegation visible — the recipient's user sees
        what task was received and from whom before processing begins.
        Best-effort: failures are logged but never block execution.

        #86: skipped entirely when ``task.silent`` is set or when
        ``task.reply_to`` is set (automated pipelines route replies
        explicitly and don't want routing-noise notifications).
        """
        if task.silent or task.reply_to:
            return
        try:
            target = self._agents.get(task.recipient)
            if target is None:
                return

            ns = target.bot.notification_service

            # Use explicit summary if provided, otherwise truncate message
            if task.summary:
                preview = task.summary
            else:
                preview = task.message if len(task.message) <= 200 else task.message[:200] + "…"
            session_name = interagent_session_name(task.sender, task.chat_id, task.topic_id)
            text = t(
                "multiagent.async_task_received",
                sender=task.sender,
                session=session_name,
                task_id=task.task_id,
                preview=preview,
            )

            chat_id = target.config.allowed_user_ids[0] if target.config.allowed_user_ids else 0
            if chat_id:
                await ns.notify(chat_id, text)
            else:
                await ns.notify_all(text)
        except Exception:
            logger.debug(
                "Failed to notify recipient '%s' about async task %s (non-critical)",
                task.recipient,
                task.task_id,
                exc_info=True,
            )

    async def _deliver_async_result(self, result: AsyncInterAgentResult) -> None:
        """Deliver an async result to the sender agent's callback handler.

        #86: when ``result.reply_to`` is non-empty we resolve the handler
        against it instead of ``result.sender`` — SSH / cross-server
        pipelines can ship replies to an explicit agent even when the
        original sender field was "unknown".
        """
        handler_key = result.reply_to or result.sender
        handler = self._async_result_handlers.get(handler_key)
        if handler is None:
            logger.warning(
                "No async result handler for '%s' task=%s — result lost",
                handler_key,
                result.task_id,
            )
            return
        try:
            await handler(result)
        except Exception:
            logger.exception(
                "Error delivering async result task=%s to '%s' — result lost",
                result.task_id,
                handler_key,
            )

    async def cancel_all_async(self) -> int:
        """Cancel all in-flight async tasks. Returns the number cancelled."""
        tasks = list(self._async_tasks.values())
        cancelled = 0
        for task in tasks:
            if task.asyncio_task and not task.asyncio_task.done():
                task.asyncio_task.cancel()
                cancelled += 1
                logger.warning(
                    "Cancelled in-flight async task=%s (%s -> %s)",
                    task.task_id,
                    task.sender,
                    task.recipient,
                )
        self._async_tasks.clear()
        return cancelled
