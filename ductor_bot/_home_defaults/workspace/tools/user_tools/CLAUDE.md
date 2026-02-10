# User Tools

Create custom scripts here when the user needs one-off or reusable utilities.

## Rules

- Keep scripts in this directory (do not scatter across workspace).
- Reuse existing scripts before creating new ones.
- Use clear filenames and add `--help`.
- Prefer structured stdout (JSON) where practical.
- Remove obsolete scripts when they are no longer useful.

## Python Environment

For dependencies, use a local virtual environment in this folder:

```bash
python3 -m venv .venv
source .venv/bin/activate
```

## Long Tasks

Avoid blocking chat for long operations.
If needed, run background jobs (for example with `nohup`) and give the user
clear progress/check commands.

## Memory

When creating scripts that indicate recurring user workflows or preferences,
update `memory_system/MAINMEMORY.md` silently.
