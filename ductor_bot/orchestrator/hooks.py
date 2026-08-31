"""Centralized message hook system for injecting prompts based on session state."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass

logger = logging.getLogger(__name__)

#: Placeholder for the workspace path in hook text. These prompts name ductor's
#: own files by path, and a conversation bound to a project directory does not
#: have them under its cwd — the agent then goes looking for a workspace it was
#: promised and cannot find.
_WORKSPACE = "{workspace}"


@dataclass(frozen=True, slots=True)
class HookContext:
    """Immutable snapshot of session state passed to hook conditions."""

    chat_id: int
    message_count: int
    is_new_session: bool
    provider: str
    model: str
    #: Absolute path to the shared workspace. Hook text names ductor's own
    #: files, which live there and not in whatever directory this conversation
    #: is bound to.
    workspace: str = ""


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
                # replace rather than format: hook text is prose and may grow
                # braces of its own, which str.format would choke on.
                suffixes.append(hook.suffix.replace(_WORKSPACE, ctx.workspace))
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


# ---------------------------------------------------------------------------
# Built-in hooks
# ---------------------------------------------------------------------------

MAINMEMORY_REMINDER = MessageHook(
    name="mainmemory_reminder",
    condition=every_n_messages(6),
    suffix=(
        "## MEMORY CHECK\n"
        "Silently review: {workspace}/memory_system/MAINMEMORY.md, "
        "{workspace}/user_tools/, {workspace}/cron_tasks/.\n"
        "Compare what you already know with this conversation so far.\n"
        "If something important is missing from memory (personality, preferences, "
        "decisions, facts) -- update MAINMEMORY.md silently.\n"
        "If you notice a gap that only the user can fill, ask ONE natural follow-up "
        "question that fits the current conversation. Do not interrogate."
    ),
)


# ---------------------------------------------------------------------------
# Built-in hook factories (cadence is user-configurable)
# ---------------------------------------------------------------------------


