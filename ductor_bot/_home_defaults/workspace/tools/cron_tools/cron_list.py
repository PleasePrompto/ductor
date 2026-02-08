#!/usr/bin/env python3
"""List all cron jobs with folder existence check.

Usage:
    python tools/cron_tools/cron_list.py
"""

from __future__ import annotations

import json

from _shared import CRON_TASKS_DIR, JOBS_PATH, load_jobs_or_default, read_user_timezone


def main() -> None:
    data = load_jobs_or_default(JOBS_PATH)

    jobs = []
    for j in data.get("jobs", []):
        task_folder = j.get("task_folder", "")
        entry: dict = {
            "id": j["id"],
            "title": j.get("title", ""),
            "schedule": j["schedule"],
            "task_folder": task_folder,
            "enabled": j.get("enabled", True),
            "last_run_at": j.get("last_run_at"),
            "last_run_status": j.get("last_run_status"),
            "task_folder_exists": (CRON_TASKS_DIR / task_folder).is_dir()
            if task_folder
            else False,
        }
        if j.get("timezone"):
            entry["timezone"] = j["timezone"]
        jobs.append(entry)

    global_tz = read_user_timezone()
    print(
        json.dumps(
            {
                "jobs": jobs,
                "count": len(jobs),
                "user_timezone": global_tz or "NOT SET (schedules fire in UTC!)",
                "how_to_modify": {
                    "change_schedule": (
                        "Use cron_edit.py: python tools/cron_tools/cron_edit.py "
                        '"<job-id>" --schedule "<cron-expr>". '
                        "Do NOT delete and recreate the job."
                    ),
                    "change_task_content": (
                        "Edit cron_tasks/<name>/TASK_DESCRIPTION.md -- "
                        "it is the single source of truth for what the agent does."
                    ),
                    "enable_disable": (
                        "Use cron_edit.py: python tools/cron_tools/cron_edit.py "
                        '"<job-id>" --enable|--disable.'
                    ),
                    "rename_job": (
                        "Use cron_edit.py: python tools/cron_tools/cron_edit.py "
                        '"<job-id>" --name "<new-id>".'
                    ),
                    "never_do": (
                        "Do NOT edit CLAUDE.md or AGENTS.md (fixed framework files). "
                        "Do NOT delete/recreate a job just to change title, description, schedule, "
                        "enabled state, or name."
                    ),
                },
                "how_to_create": (
                    "python tools/cron_tools/cron_add.py "
                    '--name "..." --title "..." --description "..." --schedule "..." '
                    "then fill in cron_tasks/<name>/TASK_DESCRIPTION.md."
                ),
                "how_to_edit": (
                    "python tools/cron_tools/cron_edit.py "
                    '"<job-id>" --title "..." --description "..." --schedule "..."'
                ),
            }
        )
    )


if __name__ == "__main__":
    main()
