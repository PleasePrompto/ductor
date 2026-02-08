# Telegram File Tools

CLI tools for processing files received via Telegram. All output JSON to stdout, all support `--help`.

## File Storage

Files in `telegram_files/` organized by date (`YYYY-MM-DD/`). Index: `_index.yaml`.

## Tools

**`list_files.py`** -- Browse received files:
```bash
python3 tools/telegram_tools/list_files.py                    # Last 20
python3 tools/telegram_tools/list_files.py --type image       # Filter by type (image/audio/video)
python3 tools/telegram_tools/list_files.py --date 2025-06-15  # Filter by date
```

**`file_info.py`** -- File metadata (name, MIME type, size, caption):
```bash
python3 tools/telegram_tools/file_info.py --file /path/to/file.jpg
```

**`read_document.py`** -- Extract text from PDF, CSV, JSON, Markdown, YAML, HTML, code:
```bash
python3 tools/telegram_tools/read_document.py --file /path/to/document.pdf
```

**`transcribe_audio.py`** -- Voice/audio to text (tries: OpenAI Whisper API, local whisper, whisper.cpp):
```bash
python3 tools/telegram_tools/transcribe_audio.py --file /path/to/voice.ogg
```

**`process_video.py`** -- Keyframes + audio transcription (requires `ffmpeg`):
```bash
python3 tools/telegram_tools/process_video.py --file /path/to/video.mp4
```

## Quick Reference

| File type | Action |
|-----------|--------|
| Photo/Image | View directly (you have vision) |
| Voice/Audio | `transcribe_audio.py` first, then respond |
| Document/PDF | `read_document.py` to extract text |
| Video | `process_video.py` for frames + transcript |
| Sticker | Acknowledge naturally |

## Quick Reply Buttons

After processing a file, offer follow-up actions via buttons. Syntax: `[button:Label]` at the end of your message. Same line = one row, separate lines = separate rows.

```
Transkript fertig (1200 Woerter).

[button:Zusammenfassen] [button:Uebersetzen]
[button:Als PDF speichern]
```

## Dependencies

- **Always available:** `file_info.py`, `list_files.py`
- **PDF:** `pip install pypdf`
- **File listing:** `pip install pyyaml`
- **Audio:** OpenAI API key OR `pip install openai-whisper` OR whisper.cpp
- **Video:** `sudo apt install ffmpeg`
