"""Single-message progress tracking for Telegram TaskHub workers."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from aiogram.exceptions import TelegramBadRequest, TelegramRetryAfter

if TYPE_CHECKING:
    from aiogram import Bot

logger = logging.getLogger(__name__)

TaskProgressKey = tuple[int, int | None, str]


@dataclass(frozen=True, slots=True)
class TaskProgressUpdate:
    """Authoritative lifecycle and streamed output for one background task."""

    chat_id: int
    topic_id: int | None
    task_id: str
    name: str
    stage: str
    elapsed_seconds: float
    output_text: str = ""
    tool_name: str = ""


@dataclass(slots=True)
class _ProgressState:
    key: TaskProgressKey
    chat_id: int
    topic_id: int | None
    task_id: str
    name: str
    stage: str
    started_at: float
    generation: int
    output_text: str = ""
    tool_name: str = ""
    message_id: int | None = None
    terminal: bool = False
    replacement_attempted: bool = False
    timer: asyncio.Task[None] | None = field(default=None, repr=False)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)


class TelegramTaskProgressTracker:
    """Own one editable Telegram status message for each active TaskHub run."""

    def __init__(
        self,
        bot: Bot,
        *,
        interval_seconds: float = 30.0,
        enabled: bool = True,
    ) -> None:
        self._bot = bot
        self._interval_seconds = interval_seconds
        self._enabled = enabled
        self._states: dict[TaskProgressKey, _ProgressState] = {}
        self._states_lock = asyncio.Lock()
        self._generation = 0
        self._closed = False

    async def update(self, update: TaskProgressUpdate) -> None:
        """Start or update one authoritative running/reviewing lifecycle state."""
        if not self._enabled or self._closed:
            return

        key = (update.chat_id, update.topic_id, update.task_id)
        if update.stage == "running":
            await self._start_generation(update)
            return

        async with self._states_lock:
            state = self._states.get(key)
        if state is None:
            await self._start_generation(update)
            return

        state.name = _display_name(update.name, update.task_id)
        state.stage = update.stage
        if update.output_text:
            state.output_text = update.output_text
        if update.tool_name:
            state.tool_name = update.tool_name
        await self._publish(state)

    async def finish(
        self,
        *,
        chat_id: int,
        topic_id: int | None,
        task_id: str,
        status: str,
    ) -> bool:
        """Stop a task heartbeat and replace its status message with a terminal state."""
        if not self._enabled:
            return False

        key = (chat_id, topic_id, task_id)
        async with self._states_lock:
            state = self._states.get(key)
            if state is None:
                return False
            state.terminal = True

        await self._cancel_timer(state)
        async with state.lock:
            text = self._terminal_text(state, status)
            updated = await self._write(state, text, terminal=True)

        async with self._states_lock:
            if self._states.get(key) is state:
                del self._states[key]
        return updated

    async def shutdown(self) -> None:
        """Cancel and await every heartbeat before the Telegram session closes."""
        if self._closed:
            return
        self._closed = True
        async with self._states_lock:
            states = list(self._states.values())
            self._states.clear()
            for state in states:
                state.terminal = True

        await asyncio.gather(*(self._cancel_timer(state) for state in states))
        for state in states:
            async with state.lock:
                pass

    async def _start_generation(self, update: TaskProgressUpdate) -> None:
        key = (update.chat_id, update.topic_id, update.task_id)
        loop = asyncio.get_running_loop()
        async with self._states_lock:
            previous = self._states.get(key)
            if previous is not None:
                previous.terminal = True
            self._generation += 1
            state = _ProgressState(
                key=key,
                chat_id=update.chat_id,
                topic_id=update.topic_id,
                task_id=update.task_id,
                name=_display_name(update.name, update.task_id),
                stage=update.stage,
                started_at=loop.time() - max(0.0, update.elapsed_seconds),
                generation=self._generation,
                output_text=update.output_text,
                tool_name=update.tool_name,
            )
            self._states[key] = state

        if previous is not None:
            await self._cancel_timer(previous)
        await self._publish(state)

        async with self._states_lock:
            if self._closed or state.terminal or self._states.get(key) is not state:
                return
            state.timer = asyncio.create_task(
                self._heartbeat_loop(state),
                name=f"task-progress:{update.task_id}:{state.generation}",
            )

    async def _heartbeat_loop(self, state: _ProgressState) -> None:
        try:
            while True:
                await asyncio.sleep(self._interval_seconds)
                if not self._is_current_active(state):
                    return
                await self._publish(state)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Task progress heartbeat failed task=%s", state.task_id)

    async def _publish(self, state: _ProgressState) -> bool:
        if not self._is_current_active(state):
            return False
        async with state.lock:
            if not self._is_current_active(state):
                return False
            return await self._write(state, self._active_text(state), terminal=False)

    async def _write(self, state: _ProgressState, text: str, *, terminal: bool) -> bool:
        if state.message_id is None:
            return await self._send(state, text)
        return await self._edit(state, text, terminal=terminal)

    async def _send(self, state: _ProgressState, text: str) -> bool:
        try:
            message = await self._bot.send_message(
                chat_id=state.chat_id,
                text=text,
                parse_mode=None,
                message_thread_id=state.topic_id,
            )
        except Exception:
            logger.warning(
                "Task progress send failed task=%s chat=%s topic=%s",
                state.task_id,
                state.chat_id,
                state.topic_id,
                exc_info=True,
            )
            return False

        message_id = getattr(message, "message_id", None)
        if not isinstance(message_id, int):
            logger.warning("Task progress send returned no message_id task=%s", state.task_id)
            return False
        state.message_id = message_id
        return True

    async def _edit(self, state: _ProgressState, text: str, *, terminal: bool) -> bool:
        error = await self._attempt_edit(state, text)
        if isinstance(error, TelegramRetryAfter):
            await asyncio.sleep(max(0.0, float(error.retry_after)))
            if not terminal and not self._is_current_active(state):
                return False
            error = await self._attempt_edit(state, text)
        if error is None:
            return True
        if (
            isinstance(error, TelegramBadRequest)
            and "message is not modified" in str(error).lower()
        ):
            return True
        if isinstance(error, TelegramRetryAfter):
            logger.warning("Task progress edit remained rate-limited task=%s", state.task_id)
            return False
        return await self._replace_once(state, text, error)

    async def _attempt_edit(self, state: _ProgressState, text: str) -> Exception | None:
        try:
            await self._edit_once(state, text)
        except Exception as exc:
            return exc
        else:
            return None

    async def _edit_once(self, state: _ProgressState, text: str) -> None:
        assert state.message_id is not None
        await self._bot.edit_message_text(
            chat_id=state.chat_id,
            message_id=state.message_id,
            text=text,
            parse_mode=None,
        )

    async def _replace_once(
        self,
        state: _ProgressState,
        text: str,
        error: Exception,
    ) -> bool:
        if state.replacement_attempted:
            logger.warning(
                "Task progress edit failed after replacement task=%s: %s",
                state.task_id,
                type(error).__name__,
            )
            return False
        state.replacement_attempted = True
        logger.warning(
            "Replacing uneditable task progress message task=%s: %s",
            state.task_id,
            type(error).__name__,
        )
        state.message_id = None
        return await self._send(state, text)

    async def _cancel_timer(self, state: _ProgressState) -> None:
        timer = state.timer
        if timer is None or timer.done():
            return
        timer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await timer

    def _is_current_active(self, state: _ProgressState) -> bool:
        return (
            self._enabled
            and not self._closed
            and not state.terminal
            and self._states.get(state.key) is state
        )

    def _active_text(self, state: _ProgressState) -> str:
        elapsed = self._elapsed(state)
        if state.stage == "reviewing":
            title = f'🔎 Task "{state.name}" is reviewing the worker result'
        else:
            title = f'⏳ Task "{state.name}" is running'
        parts = [title, f"ID: {state.task_id}", f"Elapsed: {elapsed}s"]
        if state.tool_name:
            parts.append(f"[TOOL: {state.tool_name}]")
        if state.output_text:
            parts.append("\n" + state.output_text[-3000:])
        return "\n".join(parts)

    def _terminal_text(self, state: _ProgressState, status: str) -> str:
        labels = {
            "done": ("✅", "completed"),
            "failed": ("❌", "failed"),
            "cancelled": ("⏹", "cancelled"),
            "waiting": ("❔", "waiting for input"),
            "timeout": ("⌛", "timed out"),
        }
        icon, label = labels.get(status, ("Info", f"finished ({status})"))
        text = (
            f'{icon} Task "{state.name}" {label}\n'
            f"ID: {state.task_id}\nElapsed: {self._elapsed(state)}s"
        )
        if state.tool_name:
            text += f"\n[TOOL: {state.tool_name}]"
        if state.output_text:
            text += "\n\n" + state.output_text[-3000:]
        return text

    @staticmethod
    def _elapsed(state: _ProgressState) -> int:
        return max(0, int(asyncio.get_running_loop().time() - state.started_at))


def _display_name(name: str, task_id: str) -> str:
    cleaned = " ".join(name.split()) or task_id
    return cleaned[:160]
