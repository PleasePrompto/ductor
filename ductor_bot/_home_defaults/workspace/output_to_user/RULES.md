# Output Directory

Use this directory for user-facing final deliverables.

## Use It For

- Generated reports, exports, images, documents, archives
- Any file the user should receive in Telegram

## Rules

1. Save final files in `output_to_user/`.
2. Use descriptive filenames.
3. Send with `<file:/absolute/path/to/output_to_user/...>`.
4. Keep temporary/intermediate build files elsewhere.

Prefer top-level files in this directory.
Auto-cleanup removes files older than `cleanup.output_to_user_days` (see `config/CLAUDE.md`).
Cleanup is non-recursive and targets top-level files only.
If you create subdirectories, clean them manually when no longer needed.
