"""Shared helpers for cron tool scripts."""

from __future__ import annotations

import json
import os
import re
from pathlib import Path
from typing import Any

DUCTOR_HOME = Path(os.environ.get("DUCTOR_HOME", "~/.ductor")).expanduser()
CONFIG_PATH = DUCTOR_HOME / "config" / "config.json"
JOBS_PATH = DUCTOR_HOME / "cron_jobs.json"
CRON_TASKS_DIR = DUCTOR_HOME / "workspace" / "cron_tasks"


def read_user_timezone() -> str:
    """Read user_timezone from config.json. Returns empty string if not set."""
    if not CONFIG_PATH.exists():
        return ""
    try:
        data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return str(data.get("user_timezone", "")).strip()
    except (json.JSONDecodeError, OSError):
        return ""


def sanitize_name(raw: str) -> str:
    """Lowercase and normalize a task name to [a-z0-9-]."""
    slug = raw.lower()
    slug = re.sub(r"[^a-z0-9-]", "-", slug)
    slug = re.sub(r"-{2,}", "-", slug)
    return slug.strip("-")


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
    if not jobs_path.exists():
        return {"jobs": []}
    try:
        data = json.loads(jobs_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"jobs": []}
    if not isinstance(data, dict):
        return {"jobs": []}
    if not isinstance(data.get("jobs"), list):
        return {"jobs": []}
    return data


def load_jobs_strict(jobs_path: Path) -> dict[str, Any]:
    """Load cron jobs JSON and raise on malformed structure."""
    data = json.loads(jobs_path.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("jobs"), list):
        msg = "Corrupt cron_jobs.json -- cannot parse"
        raise TypeError(msg)
    return data


def find_job_by_id_or_task_folder(jobs: list[dict[str, Any]], job_id: str) -> dict[str, Any] | None:
    """Find a job by exact id, then by task_folder."""
    exact = next((j for j in jobs if j.get("id") == job_id), None)
    if exact:
        return exact
    return next((j for j in jobs if j.get("task_folder") == job_id), None)


def available_job_ids(jobs: list[dict[str, Any]]) -> list[str]:
    """Return all job IDs for diagnostics."""
    return [str(j.get("id", "???")) for j in jobs]


def safe_task_dir(task_folder: str) -> Path:
    """Resolve and validate a cron task folder path under CRON_TASKS_DIR."""
    folder_path = (CRON_TASKS_DIR / task_folder).resolve()
    if not folder_path.is_relative_to(CRON_TASKS_DIR.resolve()):
        msg = f"Path traversal blocked: {task_folder!r} resolves outside cron_tasks"
        raise ValueError(msg)
    return folder_path


def save_jobs(jobs_path: Path, data: dict[str, Any]) -> None:
    """Persist cron jobs JSON with stable formatting."""
    jobs_path.parent.mkdir(parents=True, exist_ok=True)
    jobs_path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
