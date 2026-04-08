# Cron Tools (Claude, Codex & Gemini)

Scripts for creating, editing, listing, and removing scheduled jobs.

## MANDATORY: Ask Before Creating Jobs

**When the user requests a new cron job, you MUST ask:**

1. **Which CLI provider?**
   - `claude` - Standard Claude models
   - `codex` - OpenAI Codex models with extended thinking
   - `gemini` - Google Gemini models

2. **Which model?**
   - **If Claude:**
     - `haiku` - Fast and cost-effective
     - `sonnet` - Balanced performance (recommended)
     - `opus` - Most capable, highest quality
   - **If Codex:**
     - `gpt-5.4` - Latest frontier agentic coding model (recommended)
     - `gpt-5.4-mini` - Smaller, faster frontier coding model
     - `gpt-5.3-codex` - Frontier Codex-optimized coding model
     - `gpt-5.2` - Optimized for long-running agent work
   - **If Gemini:**
     - `gemini-2.5-pro` - Balanced, most capable (recommended)
     - `gemini-2.5-flash` - Fast and cost-effective
     - `gemini-2.5-flash-lite` - Cheapest, fastest
     - `gemini-3-pro-preview` - Next-gen preview
     - `gemini-3-flash-preview` - Next-gen fast preview
     - `gemini-3.1-pro-preview` - Latest preview

3. **If Codex: Which thinking level?**
   - `low` - Fast, surface-level reasoning
   - `medium` - Balanced (default)
   - `high` - Extended thinking
   - `xhigh` - Maximum reasoning depth
   - Most current Codex models support `low`, `medium`, `high`, `xhigh`

4. **Should this job respect quiet hours?**
   - Ask: "Should this job skip execution during specific hours (e.g., at night)?"
   - If YES: Ask for start/end hours (e.g., "Don't run between 22:00-08:00")
   - Explain: "Quiet hours prevent jobs from running during specified times (default: 21:00-08:00)"
   - Use `--quiet-start <hour>` and `--quiet-end <hour>` (0-23, supports wrap-around)

5. **Does this job share resources with other jobs?**
   - Ask: "Does this job use Chrome/browser, or compete for API rate limits/tokens?"
   - If YES: "Use a dependency name (e.g., `chrome_browser`) so jobs run one at a time"
   - Explain: "Jobs with the SAME dependency run sequentially. Different dependencies run in parallel."
   - Use `--dependency <name>` (e.g., `chrome_browser`, `api_rate_limit`, `database`)

**Present these options and wait for the user's choice!**

Do NOT suggest `--cli-parameters` proactively. Only mention it exists if the user asks.

## Mandatory Rules

1. Use these scripts for cron lifecycle actions.
2. Do not manually edit `cron_jobs.json` for normal operations.
3. Do not manually delete `cron_tasks/` folders.
4. Run `cron_list.py` before `cron_remove.py` and use exact job IDs.

## Timezone (Critical)

Before creating time-based jobs:

1. Run `python3 tools/cron_tools/cron_time.py`.
2. If `user_timezone` is empty, ask the user and set it in `~/.ductor/config/config.json`.
3. Tell the user to run `/restart` after timezone edits.

Runtime timezone resolution is:
job override (`--timezone`) -> `user_timezone` -> host timezone -> UTC.
Set `user_timezone` explicitly for predictable user-facing schedules.

## Core Commands

### Create Job (WITH FULL CONFIGURATION)

```bash
# Claude example:
python3 tools/cron_tools/cron_add.py \
  --name "job-name" \
  --title "Job Title" \
  --description "What this job does" \
  --schedule "0 9 * * *" \
  --provider claude \
  --model sonnet

# Codex example:
python3 tools/cron_tools/cron_add.py \
  --name "job-name" \
  --title "Job Title" \
  --description "What this job does" \
  --schedule "0 9 * * *" \
  --provider codex \
  --model gpt-5.4 \
  --reasoning-effort high

# Gemini example:
python3 tools/cron_tools/cron_add.py \
  --name "job-name" \
  --title "Job Title" \
  --description "What this job does" \
  --schedule "0 9 * * *" \
  --provider gemini \
  --model gemini-2.5-pro
```

**Available parameters:**
- `--provider` - CLI provider: `claude`, `codex`, or `gemini` (optional, uses global config if omitted)
- `--model` - Model choice (optional, uses global config if omitted)
- `--reasoning-effort` - Codex only: thinking level (optional, defaults to `medium`)
- `--cli-parameters` - Advanced: JSON array of CLI flags (only if user explicitly requests)

### List Jobs

```bash
python3 tools/cron_tools/cron_list.py
```

### Edit Job

```bash
python3 tools/cron_tools/cron_edit.py "exact-job-id" --schedule "30 8 * * *"
python3 tools/cron_tools/cron_edit.py "exact-job-id" --timezone "Europe/Berlin"
python3 tools/cron_tools/cron_edit.py "exact-job-id" --provider gemini
python3 tools/cron_tools/cron_edit.py "exact-job-id" --model gemini-2.5-flash
python3 tools/cron_tools/cron_edit.py "exact-job-id" --reasoning-effort xhigh
python3 tools/cron_tools/cron_edit.py "exact-job-id" --enable
python3 tools/cron_tools/cron_edit.py "exact-job-id" --disable
```

### Remove Job

```bash
python3 tools/cron_tools/cron_remove.py "exact-job-id"
```

Use `cron_edit.py` for in-place updates (title/description/schedule/timezone/provider/model/reasoning_effort/enabled).

## Task Content

Each job owns `cron_tasks/<name>/TASK_DESCRIPTION.md`.
Edit that file to change task behavior.
Do not edit task-folder `CLAUDE.md`, `AGENTS.md`, or `GEMINI.md` manually.

## After Cron Setup

Update `memory_system/MAINMEMORY.md` silently with inferred preference signals
from the user's requested automation (not just "created job").

## Pitfalls

- IDs are sanitized (lowercase + hyphens).
- Prefer exact IDs from `cron_list.py` output.
- Run any tool without args for its built-in tutorial.
