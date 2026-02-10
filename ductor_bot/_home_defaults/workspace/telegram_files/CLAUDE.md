# Telegram Files

Incoming Telegram files are stored here, grouped by date.

## Structure

```text
telegram_files/
  _index.yaml
  YYYY-MM-DD/
    ...files...
```

## Index

`_index.yaml` is rebuilt automatically and is the fastest overview.
It includes total count plus per-file metadata (`name`, `type`, `size`, `received`).

## Processing

Use tools from `tools/telegram_tools/`:

- images: inspect directly
- audio/voice: `transcribe_audio.py --file <path>`
- documents: `read_document.py --file <path>`
- video: `process_video.py --file <path>`
- metadata: `file_info.py --file <path>`
- listing: `list_files.py --type image --limit 10`

Tool scripts require absolute paths (`--file /absolute/path/...`).

## Rules

- Do not manually edit `_index.yaml`.
- Do not move/delete files unless user requested it.
- Auto-cleanup removes files older than `cleanup.telegram_files_days` (see `config/CLAUDE.md`).
  Cleanup is non-recursive (top-level date folders are not auto-pruned).

## Memory

When file processing reveals durable user patterns or preferences
(e.g., "always transcribe voice notes", preferred formats), update
`memory_system/MAINMEMORY.md` silently.
