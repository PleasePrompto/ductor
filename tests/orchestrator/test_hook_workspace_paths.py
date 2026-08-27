"""Injected prompts must not name ductor's files by relative path.

The bug this exists for: a conversation bound to a project directory runs with
that directory as cwd, but the hooks told the agent its tools were at
``tools/task_tools/``. They are not — they are in the shared workspace. The
agent went looking for a workspace it had been promised and could not find,
which reads to the user as "the folder binding is broken".

Nothing failed. No exception, no log line; the agent simply answered from a
directory it did not understand.
"""

from __future__ import annotations

import re

import pytest

from ductor_bot.orchestrator.hooks import (
    DELEGATION_BRIEF,
    DELEGATION_REMINDER,
    MAINMEMORY_REMINDER,
    HookContext,
    MessageHookRegistry,
)

WORKSPACE = "/home/ductor/.ductor/workspace"

#: Directories that exist in the shared workspace and nowhere else.
_WORKSPACE_DIRS = ("tools/", "memory_system/", "user_tools/", "cron_tasks/")

ALL_HOOKS = [MAINMEMORY_REMINDER, DELEGATION_BRIEF, DELEGATION_REMINDER]


def _ctx(*, new_session: bool = True, messages: int = 0) -> HookContext:
    return HookContext(
        chat_id=-100,
        message_count=messages,
        is_new_session=new_session,
        provider="claude",
        model="sonnet",
        workspace=WORKSPACE,
    )


def _unanchored(text: str, anchor: str) -> list[str]:
    """Occurrences of a workspace directory not preceded by *anchor*.

    Checked per occurrence rather than per line: "tools/" is a substring of
    "user_tools/", so a naive membership test calls an anchored line unanchored.
    """
    found: list[str] = []
    for directory in _WORKSPACE_DIRS:
        for match in re.finditer(rf"(?<![\w/]){re.escape(directory)}", text):
            prefix = text[: match.start()]
            if not prefix.endswith(anchor + "/"):
                line_start = text.rfind("\n", 0, match.start()) + 1
                found.append(text[line_start : match.end()].strip())
    return found


@pytest.mark.parametrize("hook", ALL_HOOKS, ids=lambda h: h.name)
def test_no_hook_names_a_workspace_path_relatively(hook) -> None:
    """Every mention must be anchored, or it resolves inside the user's repo."""
    offenders = _unanchored(hook.suffix, "{workspace}")
    assert not offenders, (
        f"{hook.name} names workspace paths relative to cwd:\n  " + "\n  ".join(offenders)
    )


def test_placeholders_are_substituted_when_applied() -> None:
    registry = MessageHookRegistry()
    registry.register(DELEGATION_BRIEF)

    prompt = registry.apply("do the thing", _ctx())

    assert "{workspace}" not in prompt, "an unsubstituted placeholder reaches the agent as text"
    assert f"{WORKSPACE}/tools/task_tools/create_task.py" in prompt


def test_applied_text_has_no_relative_workspace_paths() -> None:
    """The end-to-end property: what the agent actually receives."""
    registry = MessageHookRegistry()
    for hook in ALL_HOOKS:
        registry.register(hook)

    prompt = registry.apply("hello", _ctx(messages=29, new_session=True))

    offenders = _unanchored(prompt, WORKSPACE)
    assert not offenders, "unanchored workspace paths reach the agent:\n  " + "\n  ".join(
        offenders
    )


def test_hook_text_survives_braces() -> None:
    """Substitution is a replace, not a format: prose may grow braces."""
    from ductor_bot.orchestrator.hooks import MessageHook

    registry = MessageHookRegistry()
    registry.register(
        MessageHook(
            name="braces",
            condition=lambda _c: True,
            suffix='Use {"json": true} and {workspace}/tools/',
        )
    )

    prompt = registry.apply("x", _ctx())
    assert '{"json": true}' in prompt
    assert f"{WORKSPACE}/tools/" in prompt


def test_workspace_defaults_to_empty_without_breaking() -> None:
    """A context built without a workspace must not crash the prompt path."""
    ctx = HookContext(
        chat_id=-100, message_count=0, is_new_session=True, provider="claude", model="sonnet"
    )
    registry = MessageHookRegistry()
    registry.register(DELEGATION_BRIEF)
    assert "{workspace}" not in registry.apply("x", ctx)
