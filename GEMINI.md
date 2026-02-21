# GEMINI.md

This file provides context and guidance for Gemini (via Gemini CLI) when working with the **ductor** repository.

## Project Overview

**ductor** is a professional Telegram bot that serves as a bridge to official AI CLIs (Claude Code, OpenAI Codex, and Google Gemini). It allows users to interact with these powerful agents from their mobile devices or desktop Telegram clients while maintaining a persistent, local workspace on their host machine.

- **Main Technologies:** Python 3.11+, `aiogram` (Telegram bot framework), `Pydantic` (Data validation), `asyncio` (Asynchronous I/O), `hatchling` (Build system).
- **Core Philosophy:** No spoofing or proxying; it spawns the real CLI binaries as subprocesses and parses their streaming output.
- **Architecture:** 
    - **Frontend:** `bot/` module handles Telegram interactions, real-time streaming edits, and a rich UI (inline keyboards, file browser).
    - **Orchestration:** `orchestrator/` routes messages, manages command dispatch, and coordinates background tasks.
    - **CLI Layer:** `cli/` manages subprocess lifecycles for different AI providers (Claude, Codex, and Gemini).
    - **Automation:** Includes built-in `cron` jobs, `webhooks`, and `heartbeat` systems.
    - **Workspace:** Everything is localized in `~/.ductor/`, with a structured environment for memory, logs, and subagent tasks.

## Building and Running

### Setup
```bash
# Recommended: use a virtual environment
python -m venv .venv
# On Windows: .venv\Scripts\activate
# On POSIX: source .venv/bin/activate

# Install in editable mode with development dependencies
pip install -e ".[dev]"
```

### AI CLI Installation
To use the various providers, ensure the respective official CLIs are installed:
- **Claude:** `npm install -g @anthropic-ai/claude-code`
- **Codex:** `npm install -g @openai/codex`
- **Gemini:** `npm install -g @google/gemini-cli`

### Running the Bot
```bash
# Start the bot (runs onboarding wizard if not configured)
ductor

# Start with verbose logging
ductor -v

# Run directly via module
python -m ductor_bot
```

### Testing
```bash
# Run all tests
pytest

# Run tests with coverage report
pytest --cov=ductor_bot --cov-report=term-missing
```

### Quality Assurance
Before committing changes, ensure all quality checks pass:
```bash
# Formatting
ruff format .

# Linting
ruff check .

# Type Checking
mypy ductor_bot
```

## Development Conventions

### Coding Style
- **Formatting:** Follows Ruff's default formatting (line length 100, double quotes).
- **Docstrings:** Use Google-style docstrings.
- **Typing:** Strict typing is enforced via `mypy`. Avoid `Any` where possible; use `type: ignore` only as a last resort.
- **Imports:** Use `from __future__ import annotations` in all modules for modern type hinting support.

### Architecture Patterns
- **Paths:** Always use `DuctorPaths` (defined in `ductor_bot/workspace/paths.py`) for filesystem access. Never hardcode paths.
- **Logging:** Use the `log_context` module to ensure log entries are enriched with `chat_id` and `session_id`.
- **Persistence:** JSON files (`sessions.json`, `config.json`, etc.) are the primary data stores. Use atomic writes (write to temp file, then rename) to prevent corruption.
- **Providers:** AI providers implement the `BaseCLI` interface. The `GeminiCLI` implementation is highly optimized for Windows environments.
    - **Direct node execution:** Bypasses `.ps1` or `.cmd` wrappers to ensure environment variable inheritance (e.g., `GEMINI_SYSTEM_MD`).
    - **Prompt piping:** Always uses `stdin` for prompts to bypass Windows' 32k command-line character limit (`WinError 206`).
    - **Forced Tool Overrides:** Critical tools like `run_shell_command`, `google_web_search`, and `ask_user` are forced to manual execution to ensure robust behavior and 60s timeouts.
    - **Automatic Fallback:** If an internal Gemini tool fails, the provider automatically falls back to ductor's manual tool loop in the same turn.
    - **History Filtering:** Automatically detects and discards re-echoed conversation history in `stream-json` mode by tracking the `delta` flag.
        -   **Session Persistence:** Correctly maps `--resume` flags and captures `session_id` from `SystemInitEvent` to maintain conversation context.
    - **Cron Support:** Fully integrated with the in-process scheduler.
        - **One-shot execution:** Uses `json` output format for reliable result parsing.
        - **Windows Safety:** Employs the same `stdin` piping technique as the main provider to bypass character limits in scheduled tasks.
        - **Result Extraction:** Robust parsing of Gemini's JSON response to capture the final agent output for Telegram notifications.
    - **Error Handling:** Use the domain-specific exceptions defined in `ductor_bot/errors.py`.

### Workspace Management
- **_home_defaults:** The `ductor_bot/_home_defaults/` directory contains the template for the user's `~/.ductor/` home.
- **Programmatic Trust:** The `init_workspace` logic automatically adds the ductor workspace to Gemini CLI's `trustedFolders.json` to avoid "Safe Mode" restrictions.
- **Rule Sync:** `CLAUDE.md` and `AGENTS.md` are synchronized automatically to provide consistent instructions across different agents.

## Key Files
- `pyproject.toml`: Project metadata, dependencies, and tool configurations (Ruff, Mypy, Pytest).
- `CLAUDE.md`: Instructional context for Claude Code (and a reference for this project's conventions).
- `ductor_bot/__main__.py`: CLI entry point and lifecycle management.
- `ductor_bot/bot/app.py`: Main Telegram bot class and high-level handlers.
- `ductor_bot/orchestrator/core.py`: Central logic for routing messages and managing background observers.
- `ductor_bot/cli/gemini_provider.py`: Implementation of the Google Gemini CLI wrapper.
- `ductor_bot/config.py`: Pydantic models for bot configuration.
