"""One way to delegate, stated in the channel the model trusts.

The bug this replaces: an agent spent eleven minutes planning, handed the work
to the built-in sub-agent tool, and ended its turn saying it would report back.
That helper runs inside the turn's own process, which exits when the model
replies, so the work died unfinished and unrecorded. Nothing had told the agent
the task hub existed — the brief fired only on a new session and on every
fifteenth message, and that turn was neither.
"""

from __future__ import annotations

from ductor_bot.handoff.prompts import DELEGATION_BRIEF, delegation_brief

WORKSPACE = "/home/ductor/.ductor/workspace"


def test_the_brief_names_every_task_tool() -> None:
    body = delegation_brief(WORKSPACE)

    for tool in ("create_task.py", "list_tasks.py", "resume_task.py", "cancel_task.py"):
        assert tool in body


def test_the_workspace_path_is_filled_in() -> None:
    """An unresolved {workspace} sends the agent looking for a relative path
    that does not exist under a bound project folder."""
    body = delegation_brief(WORKSPACE)

    assert "{workspace}" not in body
    assert f"{WORKSPACE}/tools/task_tools/create_task.py" in body


def test_it_says_why_the_builtin_helper_is_wrong() -> None:
    """Naming the alternative is not enough — the model reached for the wrong
    tool because it was available, so the brief has to say what happens."""
    body = delegation_brief(WORKSPACE).lower()

    assert "built-in sub-agent" in body
    assert "exits" in body


def test_it_promises_the_result_comes_back_to_the_topic() -> None:
    assert "reports its result" in delegation_brief(WORKSPACE)


def test_it_tells_the_agent_not_to_wait() -> None:
    """Waiting synchronously would burn the turn and hit the CLI's own limits."""
    body = delegation_brief(WORKSPACE).lower()

    assert "you do not wait" in body


def test_the_brief_is_stable_across_calls() -> None:
    """A varying system prompt breaks the cached prefix, which costs more than
    the text itself. Same workspace must produce identical bytes."""
    assert delegation_brief(WORKSPACE) == delegation_brief(WORKSPACE)


def test_the_template_keeps_its_placeholder_for_reuse() -> None:
    assert "{workspace}" in DELEGATION_BRIEF
