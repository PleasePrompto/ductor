# Cron Tasks

Isolated workspaces for scheduled cron jobs. One folder per job.

## Timezone (CHECK FIRST)

**Before creating or editing any cron job with a time-based schedule:**

1. Run `python3 tools/cron_tools/cron_time.py` to check timezone config.
2. If `user_timezone` is **not set**: ask the user where they are and set it in `config.json`.
3. Cron hours are interpreted in the user's timezone. Without it, schedules fire in UTC.

## MANDATORY: Use the CLI Tools

**ALWAYS use the cron tools to manage jobs. NEVER do it manually.**

```bash
python3 tools/cron_tools/cron_add.py --name "..." --title "..." --description "..." --schedule "..."
python3 tools/cron_tools/cron_list.py
python3 tools/cron_tools/cron_edit.py "exact-job-id" --schedule "30 8 * * *"
python3 tools/cron_tools/cron_remove.py "exact-job-id"
```

- **To edit a job** (title/description/schedule/enabled/name): use `cron_edit.py`.
- **To remove a job**: Run `cron_list.py` first, then `cron_remove.py` with the EXACT job ID.
- **NEVER manually delete folders** in `cron_tasks/`. The tool handles both JSON + folder atomically.
- **NEVER manually edit `cron_jobs.json`** for add/edit/remove. Use cron tools only.

## How Cron Agents Work

Each cron job spawns a **fresh agent session** inside its folder. The sub-agent has
NO chat history, NO main memory, NO session context. It only sees:

1. The `CLAUDE.md` in its folder (fixed rules -- do NOT edit)
2. The `TASK_DESCRIPTION.md` (the actual task assignment)
3. Its own `<name>_MEMORY.md` (persistent across runs)

## Per-Task Folder Structure

```
<name>/
  CLAUDE.md            # Fixed agent rules (do NOT edit)
  AGENTS.md            # Mirror of CLAUDE.md (for Codex)
  TASK_DESCRIPTION.md  # The task assignment (edit THIS to change the task)
  <name>_MEMORY.md     # Persistent memory (read first, update last)
  scripts/             # Task-specific helper scripts
```

## Single Source of Truth

To modify what a cron task does, edit `cron_tasks/<name>/TASK_DESCRIPTION.md`.
That is the only file you need to change. CLAUDE.md and AGENTS.md are fixed framework
files and must not be edited.

## Memory Update on Setup

When you create/edit cron jobs or webhook-triggered cron tasks, update
`memory_system/MAINMEMORY.md` silently.
This applies to the main assistant while managing setup (not the isolated cron sub-agent run).

- Do not only log events like "cron/webhook created".
- Infer what the setup suggests about user interests/preferences and store that signal.
- Example (cron task): "read RSS feed for AI news every morning" suggests sustained AI enthusiasm; store that and later refine preferred AI subtopics.
- Example: webhook filters newsletter mail for next football game -> likely football interest; store this and clarify team preference later.
- Add a useful hypothesis when appropriate (e.g., likely domain interest).
- If information is uncertain but important, ask one natural follow-up question in normal conversation.
