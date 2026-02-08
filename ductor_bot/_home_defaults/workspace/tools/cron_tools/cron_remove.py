#!/usr/bin/env python3
"""Remove a cron job: deletes both the JSON entry and the cron_task folder.

The CronObserver detects the JSON change and cancels the scheduled job.

Usage:
    python tools/cron_tools/cron_remove.py "daily-report"
"""

from __future__ import annotations

import json
import shutil
import sys

from _shared import (
    CRON_TASKS_DIR,
    JOBS_PATH,
    available_job_ids,
    find_job_by_id_or_task_folder,
    load_jobs_strict,
    safe_task_dir,
    save_jobs,
)

_TUTORIAL = """\
CRON REMOVE -- Delete a scheduled cron job and its workspace folder.

This tool does TWO things:
  1. Removes the job entry from cron_jobs.json
  2. Deletes the cron_tasks/<name>/ folder (including all files!)

The CronObserver picks up the JSON change automatically and cancels the job.

USAGE:
  python tools/cron_tools/cron_remove.py "<job-id>"

EXAMPLE:
  python tools/cron_tools/cron_remove.py "weather-check"

IMPORTANT:
  The <job-id> is the EXACT ID stored in cron_jobs.json (not the display
  title, not the folder name). Use cron_list.py first to see all job IDs.

  python tools/cron_tools/cron_list.py   # Shows all jobs with their IDs
"""


def main() -> None:
    if len(sys.argv) < 2 or not sys.argv[1].strip():
        print(_TUTORIAL)
        sys.exit(1)

    job_id = sys.argv[1].strip()

    if not JOBS_PATH.exists():
        print(
            json.dumps(
                {
                    "error": f"Job '{job_id}' not found (no cron_jobs.json file)",
                    "available_jobs": [],
                }
            )
        )
        sys.exit(1)

    try:
        data = load_jobs_strict(JOBS_PATH)
    except (json.JSONDecodeError, TypeError):
        print(json.dumps({"error": "Corrupt cron_jobs.json -- cannot parse"}))
        sys.exit(1)

    jobs = data.get("jobs", [])

    # Find job: exact ID match first, then task_folder fallback
    job = find_job_by_id_or_task_folder(jobs, job_id)
    if job is None:
        available = available_job_ids(jobs)
        print(
            json.dumps(
                {
                    "error": f"Job '{job_id}' not found",
                    "hint": "Use the EXACT job ID from cron_list.py output.",
                    "available_jobs": available,
                }
            )
        )
        sys.exit(1)

    actual_id = job["id"]
    task_folder = job.get("task_folder", actual_id)

    # 1) Remove from JSON
    json_removed = False
    try:
        data["jobs"] = [j for j in jobs if j.get("id") != actual_id]
        save_jobs(JOBS_PATH, data)
        json_removed = True
    except OSError as exc:
        json_error = str(exc)

    # 2) Delete the cron_task folder
    folder_deleted = False
    folder_error = None
    try:
        folder_path = safe_task_dir(task_folder)
    except ValueError as exc:
        folder_path = (CRON_TASKS_DIR / task_folder).resolve()
        folder_error = str(exc)
    if folder_error is None and folder_path.is_dir():
        try:
            shutil.rmtree(folder_path)
            folder_deleted = True
        except OSError as exc:
            folder_error = str(exc)

    # Build result
    result: dict = {
        "job_id": actual_id,
        "json_entry_removed": json_removed,
        "folder_deleted": folder_deleted,
        "folder_path": str(folder_path),
    }

    if not json_removed:
        result["error"] = f"CRITICAL: JSON entry for '{actual_id}' could NOT be removed"
        result["json_error"] = json_error  # type: ignore[possibly-undefined]
        result["action_required"] = (
            f"The job '{actual_id}' is still in cron_jobs.json and will keep running! "
            "Manually remove the entry from ~/.ductor/cron_jobs.json."
        )

    if folder_error:
        result["folder_error"] = folder_error

    if not folder_path.is_dir() and not folder_deleted:
        result["folder_note"] = "Folder did not exist (already deleted or never created)"

    if actual_id != job_id:
        result["matched_via"] = "task_folder"
        result["note"] = (
            f"You passed '{job_id}' but the actual job ID is '{actual_id}'. "
            "Always use the exact job ID from cron_list.py."
        )

    # The CronObserver detects the mtime change and cancels the job

    print(json.dumps(result))
    sys.exit(0 if json_removed else 1)


if __name__ == "__main__":
    main()
