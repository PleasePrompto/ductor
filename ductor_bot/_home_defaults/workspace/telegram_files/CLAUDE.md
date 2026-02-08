# Telegram Files

All files the user sends via Telegram are stored here, sorted by date.

## Directory Structure

```
telegram_files/
  _index.yaml            # Auto-maintained file index (YAML)
  2026-01-15/            # One folder per day (YYYY-MM-DD)
    photo_abc123.jpg
    voice_def456.ogg
    report.pdf
    funny-video.mp4
  2026-01-16/
    ...
```

## Index (`_index.yaml`)

Updated automatically on every file download. Contains:
- `total_files` -- total count across all days
- `tree` -- date -> list of files, each with `name`, `type` (MIME), `size`, `received` (ISO timestamp)

Read this file first to get an overview of what files exist.

## File Naming

| Type | Pattern | Example |
|------|---------|---------|
| Photo | `photo_<id>.jpg` | `photo_AQADARVr.jpg` |
| Voice | `voice_<id>.ogg` | `voice_AgADYYwA.ogg` |
| Audio | `audio_<id>.<ext>` or original name | `podcast.mp3` |
| Video | `video_<id>.mp4` or original name | `demo.mp4` |
| Video note | `videonote_<id>.mp4` | `videonote_BbCCdd.mp4` |
| Document | Original filename (sanitized) | `report.pdf` |
| Sticker | `sticker_<id>.<webp|webm|tgs>` | `sticker_xyz.webp` |

Collisions are resolved with `_1`, `_2` suffixes automatically.

## Processing Tools

Tools in `tools/telegram_tools/`. All output JSON, all support `--help`. See `tools/telegram_tools/CLAUDE.md` for full docs.

| File type | Tool |
|-----------|------|
| Photo/Image | View directly (you have vision) |
| Voice/Audio | `transcribe_audio.py --file <path>` |
| Document/PDF | `read_document.py --file <path>` |
| Video | `process_video.py --file <path>` |
| Any file | `file_info.py --file <path>` |
| Browse all | `list_files.py --type image --limit 10` |

If a tool fails or the format is unsupported: try `file_info.py` first, then suggest creating a tool in `tools/user_tools/`.

## Rules

- Never move or delete files unless the user asks.
- Do not manually edit `_index.yaml` -- rebuilt automatically.
- Reference files with relative paths from workspace root (e.g. `telegram_files/2026-01-15/photo.jpg`).
