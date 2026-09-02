"""Workspace paths in prompt text must survive a working-directory override.

The failure this prevents is silent. An agent running in a project directory is
told its tools are at ``tools/agent_tools/`` — which does not exist there — and
goes looking for a workspace it was promised. Worse, an instruction to update
``memory_system/MAINMEMORY.md`` resolves *inside the user's repository*.

No exception, no log line, no failing test. It reaches the user as "the folder
binding is broken".
"""

from __future__ import annotations

import pytest

from ductor_bot.workspace.anchor import WORKSPACE_DIRS, anchor_workspace_paths

WS = "/home/ductor/.ductor/workspace"


@pytest.mark.parametrize("directory", WORKSPACE_DIRS)
def test_every_known_directory_is_anchored(directory: str) -> None:
    out = anchor_workspace_paths(f"see {directory}/thing", WS)
    assert out == f"see {WS}/{directory}/thing"


def test_the_memory_instruction_cannot_land_in_a_repo() -> None:
    """The worst case: bot memory written into the user's git repository."""
    out = anchor_workspace_paths("update memory_system/MAINMEMORY.md silently.", WS)
    assert out == f"update {WS}/memory_system/MAINMEMORY.md silently."


def test_already_absolute_paths_are_untouched() -> None:
    """Idempotent: anchoring twice must not double the prefix."""
    once = anchor_workspace_paths("run tools/agent_tools/x.py", WS)
    assert anchor_workspace_paths(once, WS) == once
    assert once.count(WS) == 1


def test_a_nested_directory_name_is_not_mangled() -> None:
    """``tools`` is a substring of ``user_tools``; only the whole segment counts."""
    out = anchor_workspace_paths("check user_tools/ now", WS)
    assert out == f"check {WS}/user_tools/ now"
    assert "user_/home" not in out
    assert out.count(WS) == 1


def test_ordinary_prose_is_left_alone() -> None:
    for text in ("the tools are good", "no skills required", "my toolset"):
        assert anchor_workspace_paths(text, WS) == text


def test_a_path_under_another_directory_is_not_rewritten() -> None:
    """Only leading segments are workspace-relative."""
    text = "/etc/tools/thing and ../tools/other"
    assert anchor_workspace_paths(text, WS) == text


def test_empty_inputs_are_safe() -> None:
    assert anchor_workspace_paths("", WS) == ""
    assert anchor_workspace_paths("tools/x", "") == "tools/x"


def test_trailing_slash_on_the_workspace_does_not_double_up() -> None:
    assert anchor_workspace_paths("tools/x", WS + "/") == f"{WS}/tools/x"


# ---------------------------------------------------------------------------
# The real prompts
# ---------------------------------------------------------------------------


def test_the_actual_shipped_prompts_are_anchored() -> None:
    """Drives the strings ductor really sends, not invented ones."""
    from ductor_bot.config import (
        _DEFAULT_COMPACT_PROMPT,
        _DEFAULT_FLUSH_PROMPT,
        _DEFAULT_HEARTBEAT_PROMPT,
    )

    shipped = (
        _DEFAULT_HEARTBEAT_PROMPT,
        _DEFAULT_FLUSH_PROMPT,
        _DEFAULT_COMPACT_PROMPT,
    )
    assert any("memory_system/" in p for p in shipped), "test is stale; the prompts moved"

    for prompt in shipped:
        out = anchor_workspace_paths(prompt, WS)
        for directory in ("memory_system/", "cron_tasks/", "tools/"):
            for index in range(len(out)):
                if out.startswith(directory, index):
                    assert out[:index].endswith(f"{WS}/"), (
                        f"unanchored {directory} in: {out[max(0, index - 60) : index + 20]!r}"
                    )


def test_media_prompts_are_anchored() -> None:
    """These name tools/media_tools/ and are injected on every attachment."""
    from ductor_bot.files import prompt as media_prompt

    source = media_prompt.__file__
    with open(source, encoding="utf-8") as handle:  # noqa: PTH123
        text = handle.read()
    assert "tools/media_tools/" in text, "test is stale; the prompts moved"

    out = anchor_workspace_paths("Use tools/media_tools/transcribe_audio.py --file x", WS)
    assert out.startswith(f"Use {WS}/tools/media_tools/")


def test_the_project_handoff_path_is_not_anchored_to_the_workspace() -> None:
    """handoffs/ belongs to the conversation's project folder, not the shared
    workspace: anchoring it would put project knowledge in ductor's home and
    quietly re-create the mixing this split exists to end."""
    from ductor_bot.config import _DEFAULT_FLUSH_PROMPT

    out = anchor_workspace_paths(_DEFAULT_FLUSH_PROMPT, WS)

    assert "handoffs/knowledge.md" in out
    assert f"{WS}/handoffs/" not in out
