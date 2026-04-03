"""Centralized message hook system for injecting prompts based on session state."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class HookContext:
    """Immutable snapshot of session state passed to hook conditions."""

    chat_id: str
    message_count: int
    is_new_session: bool
    provider: str
    model: str


@dataclass(frozen=True, slots=True)
class MessageHook:
    """A named hook that appends text to the prompt when its condition is met."""

    name: str
    condition: Callable[[HookContext], bool]
    suffix: str


class MessageHookRegistry:
    """Registry of message hooks. Applied before each CLI call."""

    def __init__(self) -> None:
        self._hooks: list[MessageHook] = []

    def register(self, hook: MessageHook) -> None:
        """Register a new message hook."""
        self._hooks.append(hook)
        logger.debug("Hook registered: %s", hook.name)

    def apply(self, prompt: str, ctx: HookContext) -> str:
        """Evaluate all hooks and append matching suffixes to the prompt."""
        suffixes: list[str] = []
        for hook in self._hooks:
            if hook.condition(ctx):
                logger.info("Hook fired: %s msgs=%d", hook.name, ctx.message_count)
                suffixes.append(hook.suffix)
        if not suffixes:
            return prompt
        return prompt + "\n\n" + "\n\n".join(suffixes)


# ---------------------------------------------------------------------------
# Reusable condition factories
# ---------------------------------------------------------------------------


def every_n_messages(n: int) -> Callable[[HookContext], bool]:
    """Fire on every n-th message (6th, 12th, 18th, ...). Never on first message."""

    def _check(ctx: HookContext) -> bool:
        # message_count is pre-increment (0-indexed at call time).
        # count=5 means this is the 6th message about to be sent.
        effective = ctx.message_count + 1
        return effective >= n and effective % n == 0

    return _check


def on_new_session(ctx: HookContext) -> bool:
    """Fire only on the very first message of a new session."""
    return ctx.is_new_session


def _is_delegation_reminder_due(ctx: HookContext) -> bool:
    """Fire every 15th message, but not on new sessions (DELEGATION_BRIEF covers those)."""
    if ctx.is_new_session:
        return False
    effective = ctx.message_count + 1
    return effective >= 15 and effective % 15 == 0


# ---------------------------------------------------------------------------
# Built-in hooks
# ---------------------------------------------------------------------------

MAINMEMORY_REMINDER = MessageHook(
    name="mainmemory_reminder",
    condition=every_n_messages(6),
    suffix=(
        "## MEMORY CHECK\n"
        "Silently review: memory_system/MAINMEMORY.md, user_tools/, cron_tasks/.\n"
        "Compare what you already know with this conversation so far.\n"
        "If something important is missing from memory (personality, preferences, "
        "decisions, facts) -- update MAINMEMORY.md silently.\n"
        "If you notice a gap that only the user can fill, ask ONE natural follow-up "
        "question that fits the current conversation. Do not interrogate."
    ),
)

DELEGATION_BRIEF = MessageHook(
    name="delegation_brief",
    condition=on_new_session,
    suffix=(
        "## BACKGROUND TASKS\n"
        "You have background workers that execute tasks for you autonomously. "
        "Any work that will likely take >30 seconds — delegate it. "
        "The worker gets your instructions, runs independently, and reports back. "
        "You keep chatting with the user while it works.\n"
        '- **Create**: tools/task_tools/create_task.py --name "..." "prompt with ALL context"\n'
        "- **Cancel**: tools/task_tools/cancel_task.py TASK_ID\n"
        '- **Resume**: tools/task_tools/resume_task.py TASK_ID "follow-up"\n'
        "  Resume keeps the worker's full context — use for refining results, "
        "follow-ups, or delivering answers after a worker question.\n"
        "- **Worker questions**: If a worker asks you something and you don't know "
        "→ ask the user → resume the task with the answer.\n"
        "Full docs: tools/task_tools/CLAUDE/GEMINI/AGENTS.md."
    ),
)

DELEGATION_REMINDER = MessageHook(
    name="delegation_reminder",
    condition=_is_delegation_reminder_due,
    suffix=(
        "## TASK REMINDER\n"
        "Delegate work >30s to background tasks. Resume completed tasks for follow-ups "
        "instead of creating new ones (keeps context). Docs: tools/task_tools/CLAUDE/GEMINI/AGENTS.md."
    ),
)
