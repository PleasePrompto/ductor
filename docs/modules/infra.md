# infra/

Process/runtime infrastructure: PID locking, restart signaling, Docker sandbox helper, install-mode detection, service management, version checking, auto-update observer, and supervisor loop.

## Files

- `pidlock.py`: single-instance lock with optional kill-existing behavior.
- `restart.py`: restart sentinel/marker helpers + `EXIT_RESTART = 42`.
- `docker.py`: `DockerManager` for optional persistent sidecar container.
- `install.py`: installation mode detection (`pipx` / `pip` / `dev`).
- `service.py`: platform dispatcher for service management backends.
- `service_linux.py`: Linux systemd user-service install/status/start/stop/logs/uninstall.
- `service_macos.py`: macOS launchd Launch Agent install/status/start/stop/logs/uninstall.
- `service_windows.py`: Windows Task Scheduler install/status/start/stop/logs/uninstall.
- `version.py`: PyPI version check, GitHub changelog fetch, `VersionInfo` model, installed version detection.
- `updater.py`: `UpdateObserver` background task, `perform_upgrade()`, upgrade sentinel read/write.
- `ductor_bot/run.py`: supervisor (hot reload + crash recovery).
- `run.py` (repo root): thin wrapper calling `ductor_bot.run.main()`.

## Service Backends

`service.py` dispatches by platform:

- `win32` -> `service_windows`
- `darwin` -> `service_macos`
- otherwise -> `service_linux`

Backend highlights:

- Linux (`service_linux.py`): systemd user service with linger support (`loginctl enable-linger`).
- macOS (`service_macos.py`): launchd user Launch Agent (`~/Library/LaunchAgents/dev.ductor.plist`) with crash-only restart (`KeepAlive.SuccessfulExit=false`), restart throttle (`ThrottleInterval=10`), and launchd stdout/stderr at `~/.ductor/logs/service.log` + `service.err`.
- Windows (`service_windows.py`): Task Scheduler task with 10s logon delay (`PT10S`), prefers `pythonw.exe -m ductor_bot` to avoid console windows (fallback `ductor` binary), and shows explicit Admin guidance when `schtasks` returns access-denied errors.
- `print_service_logs()` on Linux: `journalctl --user -u ductor -f`
- `print_service_logs()` on macOS/Windows: prints recent lines from newest `~/.ductor/logs/ductor*.log`

## PID Lock

`acquire_lock(pid_file, kill_existing=False)`:

- reads existing PID if lockfile exists,
- if process is alive:
  - `kill_existing=True` -> SIGTERM, wait, optional SIGKILL,
  - otherwise -> exit with error,
- writes current PID.

`release_lock(pid_file)` removes lock only when PID belongs to current process.

### Windows compatibility

- `_is_process_alive(pid)` catches `OSError` in addition to `ProcessLookupError` and `PermissionError`, because Windows raises various `OSError` subclasses from `os.kill(pid, 0)` for invalid or stale PIDs.
- `_force_kill_process(pid)` falls back to `SIGTERM` on Windows since `signal.SIGKILL` does not exist there.

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
4. mount `~/.ductor` into container as `/ductor` and mount available auth directories (`~/.claude`, `~/.codex`) when present.

`teardown()` stops and removes container.

`Orchestrator.create()` calls `DockerManager.setup()` when `docker.enabled=true`. If setup fails, orchestration continues in host-execution mode with warning logs.

## Version Check (`version.py`)

- `get_current_version()`: returns installed version via `importlib.metadata.version("ductor")`, falls back to `"0.0.0"`.
- `check_pypi()`: async HTTP GET to `https://pypi.org/pypi/ductor/json`, returns `VersionInfo(current, latest, update_available, summary)` or `None` on failure.
- `fetch_changelog(version)`: async fetch from GitHub Releases (`v<version>` tag fallback to `<version>`).
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
- otherwise uses `pipx upgrade --force ductor` or falls back to `python -m pip install --upgrade ductor`,
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
