"""Cron task rule file sync (create/list/delete lives in the deployed cron_tools scripts)."""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)

# Provider rule files — created per task only for authenticated providers.
_RULE_FILENAMES = ("CLAUDE.md", "AGENTS.md", "GEMINI.md")


def _detect_rule_filenames(cron_tasks_dir: Path) -> list[str]:
    """Determine which rule files to create based on parent directory contents.

    Checks which provider rule files (CLAUDE.md, AGENTS.md, GEMINI.md) exist
    in the ``cron_tasks/`` root — these are deployed by ``RulesSelector``
    based on CLI authentication status.  New task folders mirror only the
    providers that are currently authenticated.

    Falls back to ``["CLAUDE.md"]`` when no rule files are found (e.g. in tests
    or before workspace init has run).
    """
    found = [name for name in _RULE_FILENAMES if (cron_tasks_dir / name).is_file()]
    return found or ["CLAUDE.md"]


def ensure_task_rule_files(cron_tasks_dir: Path) -> int:
    """Add missing rule files to existing cron task folders.

    Checks which provider rule files exist in the ``cron_tasks/`` root
    (deployed by ``RulesSelector``) and creates any that are missing in
    task subdirectories.  Content is copied from an existing rule file in
    the same task folder so the agent instructions stay consistent.

    Only adds files — never removes.  Safe to call repeatedly (idempotent).

    Returns the number of files created.
    """
    if not cron_tasks_dir.is_dir():
        return 0

    expected = _detect_rule_filenames(cron_tasks_dir)
    created = 0

    for task_dir in sorted(cron_tasks_dir.iterdir()):
        if not task_dir.is_dir():
            continue

        # Identify existing rule files — skip dirs that have none (not a task).
        existing = [name for name in _RULE_FILENAMES if (task_dir / name).is_file()]
        if not existing:
            continue

        missing = [name for name in expected if not (task_dir / name).is_file()]
        if not missing:
            continue

        # Copy content from the first existing rule file (they're identical).
        source_content = (task_dir / existing[0]).read_text(encoding="utf-8")
        for name in missing:
            (task_dir / name).write_text(source_content, encoding="utf-8")
            created += 1
            logger.info("Created missing rule file %s in task %s", name, task_dir.name)

    return created
