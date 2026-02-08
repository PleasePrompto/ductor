# Setup Wizard & CLI

Interactive onboarding, CLI lifecycle commands, and the in-bot auto-update system.

## Files

- `ductor_bot/__main__.py`: CLI entry point, command dispatch, help/status/stop/restart/upgrade/uninstall.
- `ductor_bot/cli/init_wizard.py`: onboarding wizard (banner, disclaimer, prompts, config write), smart reset.
- `ductor_bot/infra/version.py`: PyPI version check, `VersionInfo` model.
- `ductor_bot/infra/updater.py`: `UpdateObserver` background task, `perform_upgrade()`, upgrade sentinel.
- `ductor_bot/orchestrator/commands.py`: `/upgrade` Telegram command.

## CLI Commands

| Command | Behavior |
|---|---|
| `ductor-bot` | Start bot. If unconfigured, runs onboarding first. |
| `ductor-bot onboarding` | Run setup wizard. If already configured, runs smart reset first. |
| `ductor-bot reset` | Same as `onboarding` (alias). |
| `ductor-bot stop` | Stop running bot (PID kill) and Docker container if active. |
| `ductor-bot restart` | Stop bot, re-exec process. |
| `ductor-bot upgrade` | Stop bot, upgrade package (`pipx` or `pip`), re-exec. On dev/source installs: show `git pull` guidance instead of self-upgrade. |
| `ductor-bot uninstall` | Full removal: stop bot, remove Docker, delete `~/.ductor/`, uninstall package. |
| `ductor-bot status` | Show running state, provider/model, Docker, error count, all paths. |
| `ductor-bot help` | Command reference + status panel (if configured). |
| `-v`, `--verbose` | Verbose logging (combinable with any command). |

Dispatch uses `_COMMANDS` dict mapping strings to action names. `--help` / `-h` map to `help`. Unknown commands fall through to default (auto-onboard + start).

## Onboarding Wizard (`run_onboarding`)

Interactive flow using Rich panels and Questionary prompts:

1. **ASCII Banner** -- "DUCTOR" in cyan.
2. **CLI Detection** -- checks `claude` and `codex` CLI auth status via `check_claude_auth()` / `check_codex_auth()`. Requires at least one authenticated provider. Blocks setup if none found.
3. **Disclaimer** -- risk warning (bypass permissions mode, Docker recommendation). Must confirm to proceed.
4. **Telegram Bot Token** -- step-by-step BotFather instructions. Validated with regex (`digits:alphanumeric`).
5. **Telegram User ID** -- step-by-step @userinfobot instructions. Validated as positive integer.
6. **Docker Detection** -- if `docker` binary found, offer to enable sandboxing (default: yes). If not found, show info panel and skip.
7. **Timezone Selection** -- grouped list of common IANA zones + manual entry option. Validated via `zoneinfo.ZoneInfo`.
8. **Write Config** -- merges wizard values into `AgentConfig` defaults, writes `config.json`, runs `init_workspace()`.
9. **Success Panel** -- shows all file paths (Home, Config, Workspace, Logs), then starts bot.

Returns `True` on completion. Any `None` response from Questionary (Ctrl+C) calls `_abort()` -> `sys.exit(0)`.

## Smart Reset (`run_smart_reset`)

Triggered when `onboarding` or `reset` is run on an already-configured system:

1. Read existing `config.json` for Docker settings (container name, image name).
2. Warning panel: explains full reset consequences (data loss).
3. If Docker was enabled + `docker` binary exists: offer to remove container and image.
4. Final confirmation (default: No).
5. `shutil.rmtree(ductor_home)` removes `~/.ductor/` entirely.
6. Onboarding wizard runs fresh.

## Configuration Detection

`_is_configured()` in `__main__.py`:

- reads `config.json` directly (no `AgentConfig` load),
- checks `telegram_token` is non-empty and not `YOUR_*` placeholder,
- checks `allowed_user_ids` is non-empty.

This drives the auto-onboarding behavior: `ductor-bot` with no arguments runs onboarding if unconfigured, otherwise starts the bot directly.

## Status Display

`ductor-bot status` and the status panel in `help`:

- **Running state**: reads PID from `bot.pid`, checks process alive, computes uptime from PID file mtime.
- **Provider/Model**: reads from `config.json`.
- **Docker**: enabled/disabled + container name.
- **Error count**: counts ` ERROR ` occurrences in latest `ductor*.log` file.
- **Paths**: Home, Config, Workspace, Logs, Sessions.

## Auto-Update System

### Background Check (`UpdateObserver`)

Pattern follows `CronObserver` / `HeartbeatObserver`:

1. Started in `TelegramBot._on_startup()` only when install mode is upgradeable (`pipx`/`pip`, not dev/source).
2. Initial delay: 60 seconds after startup.
3. Check interval: 60 minutes.
4. Calls `check_pypi()` -> `VersionInfo` (current, latest, update_available, summary).
5. On new version: sends Telegram notification to all `allowed_user_ids` with inline buttons.
6. Deduplicates by version string (notifies once per version).

### PyPI Version Check (`check_pypi`)

- HTTP GET `https://pypi.org/pypi/ductor-bot/json` via aiohttp (10s timeout).
- Compares installed version (`importlib.metadata.version`) against latest PyPI version.
- Version comparison uses parsed int tuples (handles dotted version strings).

### Telegram `/upgrade` Command

In `orchestrator/commands.py`:

1. Calls `check_pypi()`.
2. If no update: shows "Already up to date" with version info.
3. If update available: shows version diff + summary + inline keyboard:
   - `[Yes, upgrade now]` -> callback `upg:yes:<version>`
   - `[Not now]` -> callback `upg:no`

### Upgrade Callback Flow

In `TelegramBot._handle_upgrade_callback()`:

1. `upg:yes:<version>`: remove keyboard, send "Upgrading..." message, run `perform_upgrade()`, write upgrade sentinel, exit with code 42 (supervisor restart).
2. `upg:no`: remove keyboard and edit message to "Upgrade skipped.".

### Upgrade Execution (`perform_upgrade`)

- Refuses dev/source installs (`detect_install_mode() == "dev"`).
- Otherwise uses `pipx upgrade ductor-bot` or falls back to `python -m pip install --upgrade ductor-bot`.
- Returns `(success, output)`.

### Post-Restart Notification

Upgrade sentinel (`upgrade-sentinel.json`) in `ductor_home`:

- Written before restart: contains `chat_id`, `old_version`, `new_version`.
- Consumed on next startup in `_on_startup()`.
- Sends "Upgrade complete" message to recorded chat with old -> new version.

## CLI Upgrade (`ductor-bot upgrade`)

Separate from the Telegram flow. For users who prefer terminal:

1. Stop running bot gracefully.
2. If install mode is dev/source: print guidance to update with `git pull` and stop.
3. Otherwise run `pipx upgrade ductor-bot` (or `python -m pip install --upgrade ductor-bot`).
4. `os.execv()` to re-exec with new version.

## Uninstall (`ductor-bot uninstall`)

1. Warning panel listing all actions.
2. Questionary confirm (default: No).
3. Stop bot + Docker container.
4. Remove Docker image if Docker was enabled.
5. `shutil.rmtree(ductor_home)`.
6. `pipx uninstall` or `pip uninstall -y`.
7. Success panel.
