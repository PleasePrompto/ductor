# Cron Tasks

This directory contains isolated task folders used by scheduled jobs.
For cron tool commands (add/edit/remove/list), see `tools/cron_tools/CLAUDE.md`.

## ⚠️ MANDATORY WORKFLOW: Creating Cron Jobs

**CRITICAL: When creating a new cron job, you MUST ALWAYS ask the user these questions:**

1. **Which model?** (`--model <name>`)
   - Options: `haiku` (fast), `sonnet` (balanced), `opus` (most capable)
   - Default if user doesn't specify: Use global config model

**YOU MUST present these options to the user and wait for their answers BEFORE calling cron_add.py!**

**Advanced: CLI Parameters**
If the user explicitly requests additional CLI flags (e.g., `--chrome`), use `--cli-parameters '<json-array>'`.
DO NOT suggest this proactively - only use if the user asks for it.

**Example conversation flow:**

User: "Create a cron job to check weather every 3 minutes"

You: "I'll create a cron job to check weather every 3 minutes. Let me configure the execution:

**Model**: Which Claude model should execute this task?
   - `haiku` (fast and cost-effective)
   - `sonnet` (balanced performance)
   - `opus` (most capable, highest quality)

Please specify your choice, or I'll use the global config default."

[Wait for user response, then call cron_add.py with appropriate flags]

## Important Context

Each cron run starts a fresh agent session in `cron_tasks/<task-folder>/`.
That sub-agent has no Telegram chat history and no main-session context.

## Task Folder Structure

```text
cron_tasks/<name>/
  CLAUDE.md            # fixed task rules (do not edit)
  TASK_DESCRIPTION.md  # task instructions (edit this)
  <name>_MEMORY.md     # task-local memory
  scripts/             # task-specific helpers
```

## Editing Rules

- Edit behavior in `TASK_DESCRIPTION.md`.
- Keep jobs edited in place (`cron_edit.py`), do not recreate unless required.
- Do not edit task-folder `CLAUDE.md` manually.
- Do not manually delete task folders; use `cron_remove.py`.

## Memory During Setup

While creating/editing cron or webhook-triggered tasks, update
`memory_system/MAINMEMORY.md` silently with user preference signals and inferred interests.

## Per-Task Execution Overrides

Each cron task can override global config settings in `cron_jobs.json`:

- `model`: Model name (optional, defaults to global config)
  - Available: `"haiku"`, `"sonnet"`, `"opus"`
- `cli_parameters`: List of additional CLI flags (optional, advanced users only)

**Fallback behavior:**
- If a field is `null` or missing, the global config value is used
- This allows per-task customization while maintaining global defaults

**Example:**
```json
{
  "id": "daily-summary",
  "schedule": "0 8 * * *",
  "task_folder": "summary",
  "agent_instruction": "Summarize daily reports",
  "model": "opus"
}
```

**Use cases:**
- High-capability tasks: `"model": "opus"`
- Cost-effective tasks: `"model": "haiku"`
- Advanced CLI flags: `"cli_parameters": [...]` (only if user explicitly requests)
