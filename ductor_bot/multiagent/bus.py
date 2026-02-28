"""InterAgentBus: in-memory async message passing between agents."""

from __future__ import annotations

import asyncio
import logging
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ductor_bot.multiagent.stack import AgentStack

logger = logging.getLogger(__name__)

_DEFAULT_TIMEOUT = 300.0  # 5 minutes — for synchronous sends
_ASYNC_TIMEOUT = 3600.0  # 1 hour — async tasks may run complex multi-step work
_MAX_LOG_SIZE = 100  # Keep last N messages in log


@dataclass(slots=True)
class InterAgentMessage:
    """A message sent between agents."""

    sender: str
    recipient: str
    message: str
    timestamp: float = field(default_factory=time.time)


@dataclass(slots=True)
class InterAgentResponse:
    """Response from an inter-agent message."""

    sender: str
    text: str
    success: bool = True
    error: str | None = None


@dataclass(slots=True)
class AsyncInterAgentTask:
    """Tracks an in-flight async inter-agent request."""

    task_id: str
    sender: str
    recipient: str
    message: str
    timestamp: float = field(default_factory=time.time)
    asyncio_task: asyncio.Task[None] | None = field(default=None, repr=False)


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


AsyncResultCallback = Callable[["AsyncInterAgentResult"], Awaitable[None]]


class InterAgentBus:
    """In-memory async bus for agent-to-agent communication.

    All agents in the same process share this bus. Messages are handled
    by calling the target agent's Orchestrator.handle_interagent_message().
    """

    def __init__(self) -> None:
        self._agents: dict[str, AgentStack] = {}
        self._message_log: list[InterAgentMessage] = []
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

    async def send(
        self,
        sender: str,
        recipient: str,
        message: str,
        *,
        send_timeout: float = _DEFAULT_TIMEOUT,
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
        msg = InterAgentMessage(sender=sender, recipient=recipient, message=message)
        self._message_log.append(msg)

        # Trim log to prevent unbounded growth
        if len(self._message_log) > _MAX_LOG_SIZE:
            self._message_log = self._message_log[-_MAX_LOG_SIZE:]

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

            result = await asyncio.wait_for(
                orch.handle_interagent_message(sender, message),
                timeout=send_timeout,
            )
            logger.info(
                "Bus: %s -> %s completed (%d chars response)",
                sender,
                recipient,
                len(result),
            )
            return InterAgentResponse(sender=recipient, text=result)

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
    ) -> str | None:
        """Send a message to another agent asynchronously.

        Returns a task_id immediately. The response will be delivered to the
        sender agent's registered callback when the target agent finishes.
        Returns None if the recipient is not found.
        """
        if recipient not in self._agents:
            return None

        task_id = secrets.token_hex(6)
        task = AsyncInterAgentTask(
            task_id=task_id,
            sender=sender,
            recipient=recipient,
            message=message,
        )
        atask = asyncio.create_task(
            self._run_async(task),
            name=f"ia-async:{sender}->{recipient}:{task_id}",
        )
        task.asyncio_task = atask
        atask.add_done_callback(lambda _: self._async_tasks.pop(task_id, None))
        self._async_tasks[task_id] = task

        msg = InterAgentMessage(sender=sender, recipient=recipient, message=message)
        self._message_log.append(msg)
        if len(self._message_log) > _MAX_LOG_SIZE:
            self._message_log = self._message_log[-_MAX_LOG_SIZE:]

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
                    AsyncInterAgentResult(
                        task_id=task.task_id,
                        sender=task.sender,
                        recipient=task.recipient,
                        message_preview=task.message[:60],
                        result_text="",
                        success=False,
                        error=f"Agent '{task.recipient}' orchestrator not initialized",
                        elapsed_seconds=time.time() - t0,
                    )
                )
                return

            # Notify the recipient agent's Telegram chat about the incoming task
            await self._notify_recipient(task)

            result_text = await asyncio.wait_for(
                orch.handle_interagent_message(task.sender, task.message),
                timeout=_ASYNC_TIMEOUT,
            )
            logger.info(
                "Bus async: %s -> %s task=%s completed (%d chars, %.1fs)",
                task.sender,
                task.recipient,
                task.task_id,
                len(result_text),
                time.time() - t0,
            )
            await self._deliver_async_result(
                AsyncInterAgentResult(
                    task_id=task.task_id,
                    sender=task.sender,
                    recipient=task.recipient,
                    message_preview=task.message[:60],
                    result_text=result_text,
                    success=True,
                    elapsed_seconds=time.time() - t0,
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
                AsyncInterAgentResult(
                    task_id=task.task_id,
                    sender=task.sender,
                    recipient=task.recipient,
                    message_preview=task.message[:60],
                    result_text="",
                    success=False,
                    error=f"Timeout after {_ASYNC_TIMEOUT:.0f}s",
                    elapsed_seconds=time.time() - t0,
                )
            )

        except Exception as exc:
            logger.exception("Bus async: %s -> %s failed", task.sender, task.recipient)
            await self._deliver_async_result(
                AsyncInterAgentResult(
                    task_id=task.task_id,
                    sender=task.sender,
                    recipient=task.recipient,
                    message_preview=task.message[:60],
                    result_text="",
                    success=False,
                    error=f"{type(exc).__name__}: {exc}",
                    elapsed_seconds=time.time() - t0,
                )
            )

    async def _notify_recipient(self, task: AsyncInterAgentTask) -> None:
        """Send a short notification to the recipient agent's Telegram chat.

        This makes async task delegation visible — the recipient's user sees
        what task was received and from whom before processing begins.
        Best-effort: failures are logged but never block execution.
        """
        try:
            target = self._agents.get(task.recipient)
            if target is None:
                return
            config = target.bot._config
            chat_id = config.allowed_user_ids[0] if config.allowed_user_ids else 0
            if not chat_id:
                return

            # Truncate long messages for the preview
            preview = task.message if len(task.message) <= 200 else task.message[:200] + "…"
            text = (
                f"📥 **Async task received** from `{task.sender}`\n"
                f"Task ID: `{task.task_id}`\n\n"
                f"_{preview}_"
            )

            from ductor_bot.bot.sender import SendRichOpts, send_rich

            await send_rich(target.bot._bot, chat_id, text, SendRichOpts())
        except Exception:
            logger.debug(
                "Failed to notify recipient '%s' about async task %s (non-critical)",
                task.recipient,
                task.task_id,
                exc_info=True,
            )

    async def _deliver_async_result(self, result: AsyncInterAgentResult) -> None:
        """Deliver an async result to the sender agent's callback handler."""
        handler = self._async_result_handlers.get(result.sender)
        if handler is None:
            logger.warning(
                "No async result handler for sender '%s' task=%s — result lost",
                result.sender,
                result.task_id,
            )
            return
        try:
            await handler(result)
        except Exception:
            logger.exception(
                "Error delivering async result task=%s to '%s' — result lost",
                result.task_id,
                result.sender,
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
