# Cron Tools

CLI tools for managing scheduled cron jobs. All output JSON to stdout.

## Mandatory Rules

1. **Always use these tools** to create, list, edit, and remove jobs.
2. **Never manually edit `cron_jobs.json` for normal operations.** Use `cron_edit.py` for title/description/schedule/enabled/name changes.
3. **Never manually delete `cron_tasks/` folders.** Use `cron_remove.py`.
4. **Always run `cron_list.py` first** before removing -- use the EXACT `id` from output.
5. Job IDs are sanitized (lowercase + hyphens). Always check `job_id` in tool output.

## Timezone (CRITICAL)

**Before creating any cron job, check if `user_timezone` is set in config:**

1. Run `python3 tools/cron_tools/cron_time.py` to check the current timezone configuration.
2. If `user_timezone` is **not set** (empty):
   - Ask the user where they are (country/city).
   - Set `user_timezone` in `config.json` to the correct IANA timezone (e.g. `"Europe/Berlin"`, `"America/New_York"`).
   - Tell the user to `/restart` so the bot picks up the new timezone.
3. If `user_timezone` **is set**: all good, cron hours match the user's wall clock.

Without a timezone, all cron schedules fire in **UTC**, which is almost certainly wrong for the user.

Per-job override: `--timezone "Europe/Berlin"` on `cron_add.py` or `cron_edit.py` (rarely needed).

### Check Time

```bash
python3 tools/cron_tools/cron_time.py                              # Show config TZ + common zones
python3 tools/cron_tools/cron_time.py --zone "America/New_York"    # Check specific zone
```

## Tools

### Create

```bash
python3 tools/cron_tools/cron_add.py \
    --name "daily-report" --title "Daily Report" \
    --description "Generate daily status report" --schedule "0 9 * * *"
```

After creation: fill in `cron_tasks/<name>/TASK_DESCRIPTION.md` (Assignment + Output sections). Add scripts to `cron_tasks/<name>/scripts/` if needed.
After creating/editing cron jobs: update `memory_system/MAINMEMORY.md` silently with preference signals (not only "job created"), including reasonable user-interest hypotheses.
Example: daily RSS aggregation for AI news indicates likely ongoing AI interest; keep that in memory and refine subtopic preference later.

### List

```bash
python3 tools/cron_tools/cron_list.py
```

### Edit

```bash
python3 tools/cron_tools/cron_edit.py "exact-job-id" --schedule "30 8 * * *"
python3 tools/cron_tools/cron_edit.py "exact-job-id" --title "New Title"
python3 tools/cron_tools/cron_edit.py "exact-job-id" --description "New description"
python3 tools/cron_tools/cron_edit.py "exact-job-id" --name "new-job-id"
python3 tools/cron_tools/cron_edit.py "exact-job-id" --enable
python3 tools/cron_tools/cron_edit.py "exact-job-id" --disable
```

`cron_edit.py` never removes jobs. It updates existing jobs in place.

### Remove

```bash
python3 tools/cron_tools/cron_list.py                    # Step 1: get exact ID
python3 tools/cron_tools/cron_remove.py "exact-job-id"   # Step 2: remove
```

Removes both JSON entry and `cron_tasks/` folder atomically.

## Task Folder Structure

```
cron_tasks/<name>/
  CLAUDE.md            # Fixed rules (do NOT edit)
  AGENTS.md            # Mirror of CLAUDE.md
  TASK_DESCRIPTION.md  # The task (edit THIS)
  <name>_MEMORY.md     # Persistent task memory
  scripts/             # Task-specific scripts
```

## Modifying Jobs

**Edit in place. Never delete and recreate.**

| Change | Where |
|--------|-------|
| Title / description / schedule / enable-disable / rename | `python3 tools/cron_tools/cron_edit.py ...` |
| Task content | `cron_tasks/<name>/TASK_DESCRIPTION.md` |

Do NOT edit CLAUDE.md or AGENTS.md in task folders.

## Common Pitfalls

- **Name sanitization**: `"Wetter München"` becomes `"wetter-m-nchen"`. Check `job_id` in tool output.
- Use `python3` explicitly. `python` may not exist on some systems.
- Wrong ID? Error output lists all available IDs.

## Cron Expression Format

```
.------- minute (0-59)
|  .---- hour (0-23)
|  |  .- day of month (1-31)
|  |  |  .-- month (1-12)
|  |  |  |  .-- day of week (0-7, Sun=0/7)
*  *  *  *  *
```

Run any tool without arguments for a full tutorial.
