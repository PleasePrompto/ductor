# Tools Directory

CLI tools and user scripts. All tools output JSON to stdout and support `--help`.
This file is the navigation index. For exact rules, open the `CLAUDE.md` inside the selected subfolder.

## Quick Routing

- Recurring tasks, schedules, cron folders -> `cron_tools/CLAUDE.md`
- Incoming HTTP triggers/endpoints -> `webhook_tools/CLAUDE.md`
- Processing Telegram files/media -> `telegram_tools/CLAUDE.md`
- Custom one-off scripts -> `user_tools/CLAUDE.md`

## `cron_tools/` -- Scheduling

**Always use these tools to add/edit/remove jobs. Never manually edit `cron_jobs.json` or delete `cron_tasks/` folders.**

- `cron_add.py` -- Create job (JSON entry + task folder)
- `cron_list.py` -- List jobs with exact IDs (run BEFORE remove)
- `cron_edit.py` -- Edit existing job safely in place (title/description/schedule/enabled/name)
- `cron_remove.py` -- Remove job atomically (JSON + folder)

See `cron_tools/CLAUDE.md` for mandatory rules, modification guide, and cron expression format.

## `telegram_tools/` -- File Processing

| File type | Tool |
|-----------|------|
| Photo/Image | View directly (vision) |
| Voice/Audio | `transcribe_audio.py --file <path>` |
| Document/PDF | `read_document.py --file <path>` |
| Video | `process_video.py --file <path>` |

Also: `list_files.py` (browse by type/date), `file_info.py` (metadata).
Files stored in `telegram_files/` by date. See `telegram_tools/CLAUDE.md` for details.

## `user_tools/` -- User Scripts

Custom scripts built on demand. Name descriptively, add `--help`, output JSON when possible. Reuse before recreating. Delete when obsolete.

## `webhook_tools/` -- Webhook Endpoints

Manage incoming HTTP webhook hooks and test payloads.

- `webhook_add.py` -- Create hook with auto-generated per-hook token (`bearer` or `hmac` auth)
- `webhook_list.py` -- List hooks, auth mode, token status, server config
- `webhook_edit.py` -- Edit hook in place (incl. `--regenerate-token`, auth mode changes)
- `webhook_remove.py` -- Remove hook entry only (does not delete `cron_tasks/`)
- `webhook_test.py` -- Send local test request (auto-resolves per-hook auth)
- `webhook_rotate_token.py` -- Rotate bearer tokens (all or single hook)

Use `webhook_tools/CLAUDE.md` for full auth modes, token rotation, template syntax, and debugging flow.

## Output

Generated files go to `output_to_user/`. Send with `<file:/absolute/path/to/output_to_user/filename>`.

## Memory

Update `memory_system/MAINMEMORY.md` when you learn something worth keeping. **Do it silently -- never tell the user you are reading or writing memory.**
