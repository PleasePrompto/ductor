# infra/

Process/runtime infrastructure: PID locking, restart signaling, Docker sandbox helper, version checking, auto-update observer, and supervisor loop.

## Files

- `pidlock.py`: single-instance lock with optional kill-existing behavior.
- `restart.py`: restart sentinel/marker helpers + `EXIT_RESTART = 42`.
- `docker.py`: `DockerManager` for optional persistent sidecar container.
- `version.py`: PyPI version check, `VersionInfo` model, installed version detection.
- `updater.py`: `UpdateObserver` background task, `perform_upgrade()`, upgrade sentinel read/write.
- `ductor_bot/run.py`: supervisor (hot reload + crash recovery).
- `run.py` (repo root): thin wrapper calling `ductor_bot.run.main()`.

## PID Lock

`acquire_lock(pid_file, kill_existing=False)`:

- reads existing PID if lockfile exists,
- if process is alive:
  - `kill_existing=True` -> SIGTERM, wait, optional SIGKILL,
  - otherwise -> exit with error,
- writes current PID.

`release_lock(pid_file)` removes lock only when PID belongs to current process.

## Restart Protocol

`restart.py` API:

- `write_restart_sentinel(chat_id, message, sentinel_path)`
- `consume_restart_sentinel(sentinel_path)`
- `write_restart_marker(marker_path)`
- `consume_restart_marker(marker_path)`

Usage:

- `/restart` writes sentinel and stops polling with exit code `42`.
- on next startup, sentinel is consumed and user gets restart confirmation.
- marker file (`restart-requested`) allows external restart request for running bot.

## Docker Manager

`DockerManager.setup()`:

1. verify docker binary and daemon,
2. verify image or build (if `auto_build=true`),
3. reuse or start configured container,
4. mount workspace and available auth directories.

`teardown()` stops and removes container.

Note: default startup path does not automatically wire `DockerManager.setup()` into CLI execution.

## Version Check (`version.py`)

- `get_current_version()`: returns installed version via `importlib.metadata.version("ductor")`, falls back to `"0.0.0"`.
- `check_pypi()`: async HTTP GET to `https://pypi.org/pypi/ductor/json`, returns `VersionInfo(current, latest, update_available, summary)` or `None` on failure.
- Version comparison: dotted string parsed to int tuple via `_parse_version()`.

## Update Observer (`updater.py`)

`UpdateObserver`:

- background asyncio task matching `CronObserver` / `HeartbeatObserver` pattern,
- initial delay: 60 seconds, check interval: 60 minutes,
- calls `check_pypi()` and invokes `notify` callback when new version found,
- deduplicates by version string (notifies once per new version).
- started by `TelegramBot` only for upgradeable installs (`pipx`/`pip`).

`perform_upgrade()`:

- refuses dev/source installs (`detect_install_mode() == "dev"`),
- otherwise detects `pipx` or falls back to `python -m pip install --upgrade ductor`,
- returns `(success: bool, output: str)`.

Upgrade sentinel (`upgrade-sentinel.json`):

- `write_upgrade_sentinel(sentinel_dir, chat_id, old_version, new_version)`: persists chat context for post-restart notification.
- `consume_upgrade_sentinel(sentinel_dir)`: reads + deletes sentinel, returns data dict or `None`.

## Supervisor (`ductor_bot/run.py`)

- starts child process `python -m ductor_bot`.
- optionally watches `.py` file changes (if `watchfiles` installed).
- restart rules:
  - exit `0`: stop supervisor,
  - exit `42`: immediate restart,
  - file-change trigger: immediate restart,
  - crash: exponential backoff up to configured max.
- SIGINT/SIGTERM cancel supervisor task.
