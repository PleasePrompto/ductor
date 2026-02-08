"""Cron task folder CRUD: create, list, get, delete mini-workspaces."""

from __future__ import annotations

import logging
import re
import shutil
import venv
from pathlib import Path

from ductor_bot.workspace.paths import DuctorPaths

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Dynamic templates (only these need Python rendering, all others are files)
# ---------------------------------------------------------------------------


def render_cron_task_claude_md(name: str) -> str:
    """Render a fixed CLAUDE.md for a cron task folder.

    The content is identical for every task -- task-specific details
    live in ``TASK_DESCRIPTION.md`` instead.
    """
    return f"""\
# Your Mission

You are an **automated agent**. You run on a schedule with NO human interaction.
Complete your task autonomously and update memory when done.

## Workflow

1. **Read** `{name}_MEMORY.md` first -- it contains context from previous runs.
2. **Read** the whole `TASK_DESCRIPTION.md`!
3. **Follow the assignment** in `TASK_DESCRIPTION.md`!
4. Perform the task conscientiously!
5. **Update** `{name}_MEMORY.md` with the current date/time and what you did.

## Rules

- Stay focused on this task only. Do not deviate.
- Do not modify files outside this task folder.
- Check whether all scripts are already present in the script folder! (IF they are needed!)
- Use `.venv` for Python dependencies: `source .venv/bin/activate`

## Important

You provide the final answer after the task is completed in a pleasant, concise,
and well-formatted manner.
"""


def render_task_description_md(title: str, description: str) -> str:
    """Render the TASK_DESCRIPTION.md template for a cron task.

    The main agent fills in the *Assignment* and *Output* sections
    after ``cron_add.py`` creates the folder.
    """
    return f"""\
# {title}

## Goal

{description}

## Assignment

(Detailed instructions for completing this task. Be specific and actionable.)

## Output

(What should the final result look like? Format, content, destination.)
"""


# ---------------------------------------------------------------------------
# CRUD operations
# ---------------------------------------------------------------------------


def _sanitize_name(raw: str) -> str:
    """Lowercase, strip non-alphanumeric (except hyphens), collapse runs."""
    slug = raw.lower()
    slug = re.sub(r"[^a-z0-9-]", "-", slug)
    slug = re.sub(r"-{2,}", "-", slug)
    return slug.strip("-")


def _validate_name(name: str) -> str:
    """Sanitize and validate a cron task name."""
    if not name or not name.strip():
        msg = "Cron task name must not be empty"
        raise ValueError(msg)
    if ".." in name or "/" in name or "\\" in name:
        msg = "Cron task name must not contain path traversal sequences"
        raise ValueError(msg)
    sanitized = _sanitize_name(name)
    if not sanitized:
        msg = "Cron task name resolves to empty after sanitization"
        raise ValueError(msg)
    return sanitized


def create_cron_task(
    paths: DuctorPaths,
    name: str,
    title: str,
    description: str,
    *,
    with_venv: bool = False,
) -> Path:
    """Create a new cron task folder with full workspace structure.

    Creates: CLAUDE.md (fixed rules), AGENTS.md (mirror), TASK_DESCRIPTION.md,
    <name>_MEMORY.md, scripts/. Optional .venv/ (default off).
    """
    safe_name = _validate_name(name)
    task_dir = paths.cron_tasks_dir / safe_name

    task_dir.mkdir(parents=False, exist_ok=False)

    rule_content = render_cron_task_claude_md(safe_name)
    (task_dir / "CLAUDE.md").write_text(rule_content, encoding="utf-8")
    (task_dir / "AGENTS.md").write_text(rule_content, encoding="utf-8")

    task_desc = render_task_description_md(title, description)
    (task_dir / "TASK_DESCRIPTION.md").write_text(task_desc, encoding="utf-8")

    (task_dir / f"{safe_name}_MEMORY.md").write_text(
        f"# {safe_name} Memory\n",
        encoding="utf-8",
    )
    (task_dir / "scripts").mkdir(exist_ok=True)

    if with_venv:
        _create_venv(task_dir / ".venv")

    logger.info("Cron task folder created task=%s", safe_name)
    return task_dir


def _create_venv(venv_dir: Path) -> None:
    """Create a Python virtual environment, logging failures silently."""
    try:
        venv.create(venv_dir, with_pip=True)
    except OSError:
        logger.warning("Failed to create .venv at %s", venv_dir, exc_info=True)


def list_cron_tasks(paths: DuctorPaths) -> list[str]:
    """Return sorted names of all cron task directories."""
    if not paths.cron_tasks_dir.is_dir():
        return []
    return sorted(d.name for d in paths.cron_tasks_dir.iterdir() if d.is_dir())


def delete_cron_task(paths: DuctorPaths, name: str) -> bool:
    """Delete a cron task folder and all its contents. Returns False if not found."""
    safe_name = _validate_name(name)
    task_dir = (paths.cron_tasks_dir / safe_name).resolve()
    if not task_dir.is_relative_to(paths.cron_tasks_dir.resolve()):
        logger.warning("Path traversal blocked in delete_cron_task: %s", name)
        return False
    if not task_dir.is_dir():
        return False
    shutil.rmtree(task_dir)
    logger.info("Cron task folder deleted task=%s", safe_name)
    return True
