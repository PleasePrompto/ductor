"""Rewrite workspace-relative paths in prompt text to absolute ones.

ductor's own files — ``tools/``, ``memory_system/``, ``cron_tasks/`` and the
rest — live in the shared workspace. Prompt text all over the codebase names
them relatively, which is correct only while the CLI's working directory *is*
the workspace.

A conversation bound to a project directory breaks that assumption: the agent
is told it has tools under a path that does not exist in the user's repo, goes
looking for a workspace it was promised, and behaves as though it is in the
wrong place. Worse, an instruction to write ``memory_system/MAINMEMORY.md``
resolves inside the user's repository.

Nothing fails when this happens — no exception, no log line — so it is fixed
centrally rather than by trusting every prompt author to remember.
"""

from __future__ import annotations

import re

#: Directories that exist in the shared workspace and nowhere else. Anchoring
#: is limited to this list so ordinary prose about "tools" is left alone.
WORKSPACE_DIRS = (
    "memory_system",
    "cron_tasks",
    "user_tools",
    "output_to_user",
    "task_tools",
    "media_tools",
    "cron_tools",
    "webhook_tools",
    "telegram_files",
    "matrix_files",
    "api_files",
    "tools",
    "skills",
)

#: A mention is relative when it is not already preceded by a path separator,
#: a word character or a brace. The word-character guard is what keeps
#: ``user_tools/`` from matching the ``tools/`` entry.
_RELATIVE = re.compile(r"(?<![\w/{.])(" + "|".join(WORKSPACE_DIRS) + r")/")


def anchor_workspace_paths(text: str, workspace: str) -> str:
    """Return *text* with workspace-relative paths rewritten under *workspace*.

    Idempotent: a path that is already absolute is preceded by ``/`` and so is
    not matched again.
    """
    if not text or not workspace:
        return text
    return _RELATIVE.sub(lambda m: f"{workspace.rstrip('/')}/{m.group(1)}/", text)
