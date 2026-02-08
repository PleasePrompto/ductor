# Output Directory

This is the designated output directory for all user-requested file creation.

## When to Use

Whenever the user asks you to **create, generate, or produce a file**, save it here.
This includes but is not limited to:

- Images (PNG, JPG, SVG, WebP)
- Documents (PDF, TXT, DOCX, CSV, XLSX)
- Code files, scripts, archives
- Any other generated artifact the user wants to receive

## How to Use

1. Save the file into this directory: `output_to_user/`
2. Use descriptive filenames: `summary_report.pdf`, `logo_draft.png`, `data_export.csv`
3. Send the file to the user with `<file:/absolute/path/to/output_to_user/filename>`

## Rules

- **Always** use this directory for user-facing output files. Do not scatter files across the workspace.
- Subdirectories are allowed for organizing larger outputs (e.g. `output_to_user/charts/`).
- Clean up files that are no longer needed.
- Temporary intermediate files (build artifacts, caches) do NOT belong here -- only final deliverables.
