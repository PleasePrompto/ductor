#!/usr/bin/env python3
"""Edit a cron job in place (no delete/recreate).

Supports metadata updates and safe rename of id + task folder.

Usage:
    python tools/cron_tools/cron_edit.py "daily-report" --schedule "30 10 * * *"
    python tools/cron_tools/cron_edit.py "daily-report" --title "Daily Report 2"
    python tools/cron_tools/cron_edit.py "daily-report" --name "daily-report-v2"
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from _shared import (
    CRON_TASKS_DIR,
    JOBS_PATH,
    available_job_ids,
    find_job_by_id_or_task_folder,
    load_jobs_strict,
    render_cron_task_claude_md,
    safe_task_dir,
    sanitize_name,
    save_jobs,
)

_TUTORIAL = """\
CRON EDIT -- Update an existing cron job safely in place.

This tool edits cron_jobs.json and (on rename) updates cron_tasks/<name>/ folder.
It does NOT remove jobs.

USAGE:
  python tools/cron_tools/cron_edit.py "<job-id>" [changes...]

CHANGES:
  --name "<new-id>"          Rename job id + task folder (sanitized)
  --title "<new-title>"      Update display title
  --description "<text>"     Update metadata description
  --schedule "<cron-expr>"   Update execution schedule
  --timezone "<iana>"        Set per-job timezone override (e.g. 'Europe/Berlin')
  --enable                   Set enabled=true
  --disable                  Set enabled=false

EXAMPLES:
  python tools/cron_tools/cron_edit.py "weather-check" --schedule "30 8 * * *"
  python tools/cron_tools/cron_edit.py "weather-check" --title "Weather 08:30"
  python tools/cron_tools/cron_edit.py "weather-check" --name "weather-morning"
  python tools/cron_tools/cron_edit.py "weather-check" --disable
"""


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Edit an existing cron job safely in place",
        epilog="Run without arguments for a full tutorial.",
    )
    parser.add_argument("job_id", nargs="?", help="Existing job ID")
    parser.add_argument("--name", help="New job ID / task folder")
    parser.add_argument("--title", help="New display title")
    parser.add_argument("--description", help="New description text")
    parser.add_argument("--schedule", help="New cron expression")
    parser.add_argument("--timezone", help="IANA timezone for this job (e.g. 'Europe/Berlin')")
    enabled_group = parser.add_mutually_exclusive_group()
    enabled_group.add_argument("--enable", action="store_true", help="Enable the job")
    enabled_group.add_argument("--disable", action="store_true", help="Disable the job")
    return parser.parse_args()


def _rename_task_folder(
    *,
    old_task_folder: str,
    old_id: str,
    new_name: str,
) -> tuple[bool, bool, list[str]]:
    notes: list[str] = []
    old_folder = safe_task_dir(old_task_folder)
    new_folder = safe_task_dir(new_name)

    if new_folder.exists():
        msg = f"Target folder already exists: cron_tasks/{new_name}"
        raise FileExistsError(msg)

    if not old_folder.is_dir():
        notes.append(
            f"Task folder cron_tasks/{old_task_folder} did not exist; JSON was updated only."
        )
        return False, False, notes

    old_folder.rename(new_folder)

    memory_renamed = False
    memory_candidates = [f"{old_task_folder}_MEMORY.md", f"{old_id}_MEMORY.md"]
    seen: set[str] = set()
    for candidate in memory_candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        source = new_folder / candidate
        if source.exists():
            source.rename(new_folder / f"{new_name}_MEMORY.md")
            memory_renamed = True
            break

    if not memory_renamed:
        notes.append("No legacy memory file found to rename.")

    rule_content = render_cron_task_claude_md(new_name)
    (new_folder / "CLAUDE.md").write_text(rule_content, encoding="utf-8")
    (new_folder / "AGENTS.md").write_text(rule_content, encoding="utf-8")
    notes.append("Updated CLAUDE.md/AGENTS.md for renamed memory file reference.")

    return True, memory_renamed, notes


def _apply_updates(args: argparse.Namespace, job: dict[str, Any]) -> tuple[list[str], list[str]]:
    updated_fields: list[str] = []
    notes: list[str] = []

    if args.title is not None:
        title = args.title.strip()
        if not title:
            msg = "Title must not be empty"
            raise ValueError(msg)
        if job.get("title") != title:
            job["title"] = title
            updated_fields.append("title")

    if args.description is not None and job.get("description") != args.description:
        job["description"] = args.description
        updated_fields.append("description")

    if args.schedule is not None:
        schedule = args.schedule.strip()
        if not schedule:
            msg = "Schedule must not be empty"
            raise ValueError(msg)
        if job.get("schedule") != schedule:
            job["schedule"] = schedule
            updated_fields.append("schedule")

    if args.timezone is not None:
        tz_val = args.timezone.strip()
        if job.get("timezone", "") != tz_val:
            job["timezone"] = tz_val
            updated_fields.append("timezone")

    if args.enable and job.get("enabled", True) is not True:
        job["enabled"] = True
        updated_fields.append("enabled")
    if args.disable and job.get("enabled", True) is not False:
        job["enabled"] = False
        updated_fields.append("enabled")

    notes.append(
        "Task behavior still comes from cron_tasks/<name>/TASK_DESCRIPTION.md; "
        "title/description are metadata."
    )
    return updated_fields, notes


def main() -> None:
    args = _parse_args()

    if not args.job_id:
        print(_TUTORIAL)
        sys.exit(1)

    has_changes = any(
        [
            args.name is not None,
            args.title is not None,
            args.description is not None,
            args.schedule is not None,
            args.timezone is not None,
            args.enable,
            args.disable,
        ]
    )
    if not has_changes:
        print(_TUTORIAL)
        print("Missing changes: pass at least one edit flag (e.g. --schedule, --title, --name).")
        sys.exit(1)

    if not JOBS_PATH.exists():
        print(
            json.dumps(
                {
                    "error": f"Job '{args.job_id}' not found (no cron_jobs.json file)",
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
    job = find_job_by_id_or_task_folder(jobs, args.job_id)
    if job is None:
        print(
            json.dumps(
                {
                    "error": f"Job '{args.job_id}' not found",
                    "hint": "Use the EXACT job ID from cron_list.py output.",
                    "available_jobs": available_job_ids(jobs),
                }
            )
        )
        sys.exit(1)

    old_id = str(job["id"])
    old_task_folder = str(job.get("task_folder", old_id))
    updated_fields: list[str] = []
    notes: list[str] = []
    folder_renamed = False
    memory_file_renamed = False

    if args.name is not None:
        raw_new_name = args.name.strip()
        new_name = sanitize_name(raw_new_name)
        if not new_name:
            print(json.dumps({"error": "Name resolves to empty after sanitization"}))
            sys.exit(1)
        if any(j.get("id") == new_name and j is not job for j in jobs):
            print(json.dumps({"error": f"Job '{new_name}' already exists"}))
            sys.exit(1)

        if new_name != old_id:
            try:
                folder_renamed, memory_file_renamed, rename_notes = _rename_task_folder(
                    old_task_folder=old_task_folder,
                    old_id=old_id,
                    new_name=new_name,
                )
            except (ValueError, FileExistsError) as exc:
                print(json.dumps({"error": str(exc)}))
                sys.exit(1)

            notes.extend(rename_notes)
            job["id"] = new_name
            job["task_folder"] = new_name
            updated_fields.extend(["id", "task_folder"])

    try:
        meta_updates, meta_notes = _apply_updates(args, job)
    except ValueError as exc:
        print(json.dumps({"error": str(exc)}))
        sys.exit(1)

    updated_fields.extend(meta_updates)
    notes.extend(meta_notes)

    # Keep order stable but unique
    seen_fields: set[str] = set()
    unique_fields: list[str] = []
    for field in updated_fields:
        if field in seen_fields:
            continue
        seen_fields.add(field)
        unique_fields.append(field)

    if not unique_fields:
        print(
            json.dumps(
                {
                    "job_id": old_id,
                    "updated": False,
                    "message": "No effective changes detected.",
                }
            )
        )
        return

    save_jobs(JOBS_PATH, data)

    current_id = str(job["id"])
    task_folder = str(job.get("task_folder", current_id))
    result: dict[str, Any] = {
        "job_id": current_id,
        "updated": True,
        "updated_fields": unique_fields,
        "schedule": job.get("schedule"),
        "enabled": job.get("enabled", True),
        "task_folder": f"cron_tasks/{task_folder}",
        "folder_renamed": folder_renamed,
        "memory_file_renamed": memory_file_renamed,
        "notes": notes,
    }

    if args.job_id != old_id:
        result["matched_via"] = "task_folder"

    if args.name is not None:
        result["original_name"] = old_id
        result["new_name"] = current_id
        if sanitize_name(args.name.strip()) != args.name.strip():
            result["name_sanitized"] = True

    print(json.dumps(result))


if __name__ == "__main__":
    main()
