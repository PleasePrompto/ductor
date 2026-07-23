"""Shared helpers for cron tool scripts."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any

from ductor_bot._home_defaults.workspace.tools._tool_shared import (
    available_ids,
    find_by_id,
    load_collection_or_default,
    load_collection_strict,
    sanitize_name,
    save_collection,
)

DUCTOR_HOME = Path(os.environ.get("DUCTOR_HOME", "~/.ductor")).expanduser()
CONFIG_PATH = DUCTOR_HOME / "config" / "config.json"
JOBS_PATH = DUCTOR_HOME / "cron_jobs.json"
CRON_TASKS_DIR = DUCTOR_HOME / "workspace" / "cron_tasks"

# Provider rule files — only create for authenticated providers.
_RULE_FILENAMES = ("CLAUDE.md", "AGENTS.md", "GEMINI.md")


def read_user_timezone() -> str:
    """Read user_timezone from config.json. Returns empty string if not set."""
    if not CONFIG_PATH.exists():
        return ""
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return str(data.get("user_timezone", "")).strip()
    except (json.JSONDecodeError, OSError):
        return ""


def detect_rule_filenames() -> list[str]:
    """Determine which rule files to create based on parent cron_tasks/ contents.

    Checks which provider rule files (CLAUDE.md, AGENTS.md, GEMINI.md) exist
    in the ``cron_tasks/`` root.  These are deployed by the RulesSelector during
    workspace init based on CLI authentication status.

    Falls back to ``["CLAUDE.md"]`` when no rule files are found.
    """
    found = [name for name in _RULE_FILENAMES if (CRON_TASKS_DIR / name).is_file()]
    return found or ["CLAUDE.md"]


def render_cron_task_claude_md(name: str) -> str:
    """Render fixed CLAUDE.md/AGENTS.md content for a cron task folder."""
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


def load_jobs_or_default(jobs_path: Path) -> dict[str, Any]:
    """Load cron jobs JSON or return an empty payload if missing/corrupt."""
    return load_collection_or_default(jobs_path, "jobs")


def load_jobs_strict(jobs_path: Path) -> dict[str, Any]:
    """Load cron jobs JSON and raise on malformed structure."""
    return load_collection_strict(jobs_path, "jobs")


def find_job_by_id_or_task_folder(jobs: list[dict[str, Any]], job_id: str) -> dict[str, Any] | None:
    """Find a job by exact id, then by task_folder."""
    exact = find_by_id(jobs, job_id)
    if exact:
        return exact
    return next((j for j in jobs if j.get("task_folder") == job_id), None)


def available_job_ids(jobs: list[dict[str, Any]]) -> list[str]:
    """Return all job IDs for diagnostics."""
    return available_ids(jobs)


def safe_task_dir(task_folder: str) -> Path:
    """Resolve and validate a cron task folder path under CRON_TASKS_DIR."""
    folder_path = (CRON_TASKS_DIR / task_folder).resolve()
    if not folder_path.is_relative_to(CRON_TASKS_DIR.resolve()):
        msg = f"Path traversal blocked: {task_folder!r} resolves outside cron_tasks"
        raise ValueError(msg)
    return folder_path


def save_jobs(jobs_path: Path, data: dict[str, Any]) -> None:
    """Persist cron jobs JSON with stable formatting."""
    save_collection(jobs_path, data)
