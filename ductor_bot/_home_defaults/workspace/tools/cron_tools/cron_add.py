#!/usr/bin/env python3
"""Add a cron job: creates both the JSON entry and the cron_task folder.

The CronObserver detects the JSON change automatically and schedules the job.

Usage:
    python tools/cron_tools/cron_add.py \
        --name "daily-report" \
        --title "Daily Report" \
        --description "Generate daily status report" \
        --schedule "0 9 * * *"
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import UTC, datetime
from pathlib import Path

from _shared import (
    CRON_TASKS_DIR,
    JOBS_PATH,
    load_jobs_or_default,
    read_user_timezone,
    render_cron_task_claude_md,
    sanitize_name,
    save_jobs,
)

_DEFAULT_INSTRUCTION = (
    "Read through TASK_DESCRIPTION.md and carry it out. Stay focused and complete the task neatly."
)

_TUTORIAL = """\
CRON ADD -- Create a scheduled cron job with its own workspace.

This tool does TWO things in one step:
  1. Creates a cron_task folder (CLAUDE.md, AGENTS.md, TASK_DESCRIPTION.md, Memory, scripts/)
  2. Adds a job entry to cron_jobs.json

The CronObserver picks up the JSON change automatically and schedules the job.

REQUIRED PARAMETERS:
  --name          Unique job/folder ID (lowercase, hyphens ok)
  --title         Short human-readable title
  --description   What the job does (pre-fills TASK_DESCRIPTION.md)
  --schedule      Cron expression (see format below)

OPTIONAL:
  --timezone      IANA timezone override for this job (e.g. 'Europe/Berlin')
                  If omitted, uses user_timezone from config.json.
                  If config has no user_timezone either, falls back to UTC.

EXECUTION OVERRIDES (optional, override global config for this specific job):
  --provider          CLI provider: 'claude' or 'codex' (defaults to global config)
  --model             Model name (e.g. 'opus', 'sonnet', 'gpt-5.2-codex')
  --reasoning-effort  Thinking level for Codex: 'low', 'medium', 'high', 'xhigh'
  --cli-parameters    Additional CLI flags as JSON array (e.g. '["--chrome"]' for Claude only)

QUIET HOURS (optional, prevent jobs from running during specific hours):
  --quiet-start       Start of quiet hours (0-23, job WON'T run during this time)
  --quiet-end         End of quiet hours (0-23, exclusive)
                      If omitted, uses global heartbeat.quiet_start/quiet_end from config.
                      Supports wrap-around: --quiet-start 21 --quiet-end 8 means 21:00-07:59.
                      Set both to same value to disable quiet hours for this job.
                      Example: --quiet-start 22 --quiet-end 7 (no execution 22:00-06:59)

DEPENDENCIES (optional, prevent concurrent resource conflicts):
  --dependency        Resource identifier (e.g. 'chrome_browser', 'api_token', 'database')
                      Jobs with the SAME dependency run sequentially (one at a time, FIFO).
                      Jobs with DIFFERENT dependencies or no dependency run in parallel.
                      Use this when jobs share resources that can't be used concurrently.
                      Examples: --dependency chrome_browser (multiple browser automation jobs)
                                --dependency api_rate_limit (API calls with rate limiting)

TIMEZONE REMINDER:
  Hours in cron expressions are interpreted in the user's timezone.
  If user_timezone is NOT set in config.json, ask the user where they are
  and set it BEFORE creating the job. Otherwise schedules will fire in UTC.
  Use cron_time.py to check the current timezone configuration.

CRON EXPRESSION FORMAT:
  .---------------- minute (0-59)
  |  .------------- hour (0-23)
  |  |  .---------- day of month (1-31)
  |  |  |  .------- month (1-12)
  |  |  |  |  .---- day of week (0-7, 0=Sun, 7=Sun)
  |  |  |  |  |
  *  *  *  *  *

EXAMPLES:
  "0 9 * * *"      Every day at 09:00
  "*/15 * * * *"   Every 15 minutes
  "0 9 * * 1-5"    Weekdays at 09:00
  "0 0 1 * *"      First day of each month at midnight
  "30 8,12,18 * * *"  At 08:30, 12:30, and 18:30

FULL EXAMPLES:

Basic job (uses global config):
  python tools/cron_tools/cron_add.py \\
      --name "weather-check" \\
      --title "Weather Check Muenster" \\
      --description "Check current weather and summarize" \\
      --schedule "0 8 * * *"

Codex job with high reasoning:
  python tools/cron_tools/cron_add.py \\
      --name "data-analysis" \\
      --title "Daily Data Analysis" \\
      --description "Analyze data with extended thinking" \\
      --schedule "0 9 * * *" \\
      --provider codex \\
      --model gpt-5.2-codex \\
      --reasoning-effort high

Claude job with browser automation:
  python tools/cron_tools/cron_add.py \\
      --name "web-scraper" \\
      --title "News Scraper" \\
      --description "Scrape news sites with browser" \\
      --schedule "*/30 * * * *" \\
      --provider claude \\
      --model sonnet \\
      --cli-parameters '["--chrome"]'

Job with quiet hours (don't run at night):
  python tools/cron_tools/cron_add.py \\
      --name "daily-summary" \\
      --title "Daily Summary" \\
      --description "Summarize day's activities" \\
      --schedule "0 20 * * *" \\
      --quiet-start 22 \\
      --quiet-end 8
  # This job is scheduled for 20:00, but if it's delayed it won't run 22:00-07:59

Job with dependency (prevent Chrome conflicts):
  python tools/cron_tools/cron_add.py \\
      --name "linkedin-scraper" \\
      --title "LinkedIn Scraper" \\
      --description "Scrape LinkedIn with browser" \\
      --schedule "0 10 * * *" \\
      --dependency chrome_browser \\
      --cli-parameters '["--chrome"]'
  # If multiple jobs have --dependency chrome_browser, they run one at a time

WHAT HAPPENS AFTER CREATION:
  1. Open cron_tasks/<name>/TASK_DESCRIPTION.md
  2. Fill in the Assignment and Output sections with specific instructions
  3. If scripts are needed: create them in cron_tasks/<name>/scripts/
  4. If Python packages are needed: create a .venv in the task folder
  5. The CronObserver triggers at the scheduled time and spawns a fresh agent
"""


def _render_task_description_md(title: str, description: str) -> str:
    """Render the TASK_DESCRIPTION.md template for a cron task."""
    return f"""\
# {title}

## Goal

{description}

## Assignment

(Detailed instructions for completing this task. Be specific and actionable.)

## Output

(What should the final result look like? Format, content, destination.)
"""


def _create_task_folder(name: str, title: str, description: str) -> Path:
    """Create the cron_task workspace folder."""
    task_dir = CRON_TASKS_DIR / name
    task_dir.mkdir(parents=True, exist_ok=False)

    claude_content = render_cron_task_claude_md(name)
    (task_dir / "CLAUDE.md").write_text(claude_content, encoding="utf-8")
    (task_dir / "AGENTS.md").write_text(claude_content, encoding="utf-8")

    task_desc = _render_task_description_md(title, description)
    (task_dir / "TASK_DESCRIPTION.md").write_text(task_desc, encoding="utf-8")

    (task_dir / f"{name}_MEMORY.md").write_text(f"# {name} Memory\n", encoding="utf-8")
    (task_dir / "scripts").mkdir(exist_ok=True)

    return task_dir


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Add a cron job with its own workspace folder",
        epilog="Run without arguments or with --help for a full tutorial.",
    )
    parser.add_argument("--name", help="Unique job/folder ID")
    parser.add_argument("--title", help="Short human-readable title")
    parser.add_argument("--description", help="What the job does")
    parser.add_argument("--schedule", help="Cron expression (e.g. '0 9 * * *')")
    parser.add_argument(
        "--timezone",
        help="IANA timezone for this job (e.g. 'Europe/Berlin'). "
        "Overrides config user_timezone for this job only.",
    )
    parser.add_argument(
        "--provider",
        choices=["claude", "codex"],
        help="CLI provider for this job (claude or codex). If omitted, uses global config.",
    )
    parser.add_argument(
        "--model",
        help="Model name for this job (e.g. 'opus', 'sonnet', 'gpt-5.2-codex'). "
        "If omitted, uses global config.",
    )
    parser.add_argument(
        "--reasoning-effort",
        choices=["low", "medium", "high", "xhigh"],
        help="Thinking level for Codex jobs only (low, medium, high, xhigh). "
        "If omitted, uses global config or model default.",
    )
    parser.add_argument(
        "--cli-parameters",
        help="Additional CLI flags as JSON array (e.g. '[\"--chrome\"]'). "
        "If omitted, uses only global config parameters.",
    )
    parser.add_argument(
        "--quiet-start",
        type=int,
        choices=range(24),
        metavar="HOUR",
        help="Start of quiet hours (0-23). Job won't run during quiet hours. "
        "If omitted, uses global heartbeat.quiet_start from config.",
    )
    parser.add_argument(
        "--quiet-end",
        type=int,
        choices=range(24),
        metavar="HOUR",
        help="End of quiet hours (0-23, exclusive). "
        "If omitted, uses global heartbeat.quiet_end from config.",
    )
    parser.add_argument(
        "--dependency",
        help="Resource dependency (e.g. 'chrome_browser'). "
        "Jobs with same dependency run sequentially, different dependencies run in parallel.",
    )
    args = parser.parse_args()

    missing = [p for p in ("name", "title", "description", "schedule") if not getattr(args, p)]
    if missing:
        print(_TUTORIAL)
        print(f"Missing required parameters: {', '.join('--' + m for m in missing)}")
        sys.exit(1)

    name = sanitize_name(args.name)
    if not name:
        print(json.dumps({"error": "Name resolves to empty after sanitization"}))
        sys.exit(1)

    data = load_jobs_or_default(JOBS_PATH)

    if any(j["id"] == name for j in data["jobs"]):
        print(json.dumps({"error": f"Job '{name}' already exists"}))
        sys.exit(1)

    task_dir = CRON_TASKS_DIR / name
    if task_dir.exists():
        print(json.dumps({"error": f"Folder '{name}' already exists in cron_tasks/"}))
        sys.exit(1)

    # 1) Create the cron_task folder
    _create_task_folder(name, args.title, args.description)

    # 2) Add job to JSON
    job: dict = {
        "id": name,
        "title": args.title,
        "description": args.description,
        "schedule": args.schedule,
        "task_folder": name,
        "agent_instruction": _DEFAULT_INSTRUCTION,
        "enabled": True,
        "created_at": datetime.now(UTC).isoformat(),
        "last_run_at": None,
        "last_run_status": None,
    }
    if args.timezone:
        job["timezone"] = args.timezone.strip()
    if args.provider:
        job["provider"] = args.provider
    if args.model:
        job["model"] = args.model
    if args.reasoning_effort:
        job["reasoning_effort"] = args.reasoning_effort
    if args.cli_parameters:
        try:
            cli_params = json.loads(args.cli_parameters)
            if isinstance(cli_params, list):
                job["cli_parameters"] = cli_params
            else:
                print(json.dumps({"error": "--cli-parameters must be a JSON array"}))
                sys.exit(1)
        except json.JSONDecodeError as e:
            print(json.dumps({"error": f"Invalid --cli-parameters JSON: {e}"}))
            sys.exit(1)
    if args.quiet_start is not None:
        job["quiet_start"] = args.quiet_start
    if args.quiet_end is not None:
        job["quiet_end"] = args.quiet_end
    if args.dependency:
        job["dependency"] = args.dependency.strip()
    data["jobs"].append(job)
    save_jobs(JOBS_PATH, data)

    # The CronObserver detects the mtime change and auto-schedules

    task_path = f"cron_tasks/{name}"
    effective_tz = args.timezone.strip() if args.timezone else read_user_timezone()
    result: dict = {
        "job_id": name,
        "schedule": args.schedule,
        "timezone": effective_tz or "UTC (no user_timezone configured)",
        "task_folder": task_path,
        "folder_created": True,
        "json_entry_created": True,
        "action_required": [
            f"Open {task_path}/TASK_DESCRIPTION.md and fill in the Assignment and Output sections NOW.",
            "TASK_DESCRIPTION.md is the cron agent's task file. "
            "The agent spawns blind in this folder -- no chat history, no main memory.",
            "Make the Assignment section technical, specific, and actionable. "
            "Describe exactly what the agent must do, step by step.",
            f"If scripts are needed: create them in {task_path}/scripts/ "
            "and reference them in TASK_DESCRIPTION.md.",
            f"If Python packages are needed: create a .venv in {task_path}/ "
            "and install dependencies.",
            f"The agent's memory is {task_path}/{name}_MEMORY.md. "
            "CLAUDE.md already tells the agent to read and update it.",
            f"To change title/description/schedule/name/enabled later: "
            f'python3 tools/cron_tools/cron_edit.py "{name}" ...',
            "To modify this task later: edit TASK_DESCRIPTION.md only. "
            "CLAUDE.md and AGENTS.md are fixed framework files.",
            f'To REMOVE this job later: python3 tools/cron_tools/cron_remove.py "{name}"',
        ],
    }
    if not effective_tz:
        result["timezone_warning"] = (
            "IMPORTANT: No user_timezone is configured. "
            "Cron schedules will fire in UTC which is likely WRONG for the user. "
            "Ask the user for their timezone (country/city) and set user_timezone "
            'in config.json (e.g. "Europe/Berlin", "America/New_York").'
        )
    if name != args.name.strip():
        result["name_sanitized"] = True
        result["original_name"] = args.name.strip()
        result["warning"] = (
            f"Name was sanitized: '{args.name.strip()}' -> '{name}'. "
            f"Use '{name}' as the job ID for all future operations (list, remove)."
        )
    print(json.dumps(result))


if __name__ == "__main__":
    main()
