# User Tools

Custom scripts built on demand for the user during conversation.

## When to Use

When the user asks to build a script, tool, or utility -- create it here.
Examples: data processing, API wrappers, automation helpers, file converters.

## How to Use

- Create scripts in this directory.
- Use a `.venv` for Python dependencies: `python3 -m venv .venv && source .venv/bin/activate`
- Name scripts descriptively: `fetch_weather.py`, `resize_images.py`
- Make scripts self-documenting (`argparse`, `--help`).
- Output JSON to stdout when possible.

## Rules

- Scripts live here. Do not scatter tools across the workspace.
- Reuse existing scripts before creating new ones.
- Delete scripts that are no longer needed.

## IMPORTANT:
Long Tasks:
Never block the chat. Write a background script, run with nohup, tell the user how to check progress. Nobody likes staring at a loading screen.
If you have to install large packages, this could cause LONG delays, during which time the user will not be able to read anything from you. That would be negative. Come up with dynamic, smart solutions to install everything but continue chatting.