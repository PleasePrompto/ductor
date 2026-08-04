# infra/

Runtime infrastructure: process lifecycle, restart/update flow, Docker sandbox, service backends, shared low-level helpers.

## Files

- process/runtime: `pidlock.py`, `restart.py`, `inflight.py`, `recovery.py`, `startup_state.py`, `boot_id.py`
- watchers: `file_watcher.py`
- secrets: `env_secrets.py`
- Docker: `docker.py`, `docker_extras.py`
- service: `service.py`, `service_base.py`, `service_logs.py`, `service_linux.py`, `service_macos.py`, `service_windows.py`
- update/version: `install.py`, `version.py`, `updater.py`
- filesystem/atomic I/O: `fs.py`, `atomic_io.py`, `json_store.py`
- shared observer/task helpers: `base_observer.py`, `base_task_observer.py`, `task_runner.py`
- platform/process helpers: `platform.py`, `process_tree.py`

## Startup and recovery state

State files under `~/.ductor/`:

- `startup_state.json`
- `inflight_turns.json`

Behavior:

- startup kind detection: `first_start`, `service_restart`, `system_reboot`
- in-flight foreground turns are tracked for restart recovery planning
- named-session recovery candidates are merged by `RecoveryPlanner`

## PID lock and single-instance control

`acquire_lock(pid_file, kill_existing=True)` ensures one active runtime instance.

`run_bot()` acquires lock at startup and releases it on shutdown.

## Restart protocol

Restart code: `42` (`EXIT_RESTART`).

- `/restart` writes sentinel and stops polling
- service/supervisor context: process exits with restart code
- foreground direct context: process re-exec path

## Lifecycle stop behavior

`stop_bot()` (implemented in `cli_commands/lifecycle.py`):

1. stop installed service
2. kill PID-file instance
3. kill remaining ductor processes
4. short Windows lock-release wait
5. stop Docker container when enabled

## Docker manager

`DockerManager.setup()` handles:

- daemon/image/container checks
- container (re)start
- mounts:
  - `~/.ductor -> /ductor`
  - provider homes (`~/.claude`, `~/.codex`, `~/.gemini`, `~/.grok`, `~/.claude.json` when present)
  - optional host cache mount
  - user-configured `docker.mounts` to `/mnt/<name>`

The sandbox image owns the Grok CLI version. `grok update` inside the container is unsupported;
use `ductor docker rebuild` to update it.

Docker extras (`docker_extras.py`):

- `DockerExtra` frozen dataclass registry of optional AI/ML packages (Whisper, PyTorch, OpenCV, Tesseract, etc.)
- `resolve_extras()` resolves transitive dependencies in topological order
- `generate_dockerfile_extras()` appends `RUN` blocks (apt + pip) to the base Dockerfile
- packages with custom `--index-url` (e.g. PyTorch CPU) are installed before standard PyPI packages to prevent CUDA variant downloads
- `calculate_build_timeout()` adds per-extra timeout to the base build timeout
- build output is streamed live via `_exec_stream()` to the Rich console

Fallback behavior:

- if Docker setup/recovery fails, runtime falls back to host execution.

## Service backends

Platform dispatch via `infra/service.py`:

- Linux: systemd user service
- macOS: launchd
- Windows: Task Scheduler

`ductor service logs`:

- Linux: journal stream
- macOS/Windows: file tail from `~/.ductor/logs/agent.log` (fallback newest `*.log`)

Deep dive: [service_management](service_management.md)

## Atomic persistence helpers

- `atomic_io.py`: `atomic_text_save`, `atomic_bytes_save`
- `json_store.py`: `atomic_json_save`, `load_json`

These are used across session/task/cron/webhook/config persistence paths.

## Environment secrets (`env_secrets.py`)

Centralised loading of user-defined API secrets from `~/.ductor/.env`.

- standard dotenv syntax (comments, `export` prefix, single/double quotes)
- loaded once per process and cached
- injected at three points:
  - `_build_subprocess_env()` in `executor.py` (host CLI execution)
  - `docker_wrap()` in `base.py` (`docker exec -e` flags)
  - `_start_container()` in `docker.py` (`docker run -e` flags)
- existing environment variables are never overridden
- provider-specific `extra_env` (e.g. `GEMINI_API_KEY` from config) takes precedence
- mtime-based cache invalidation: edits take effect on the next CLI invocation without restart

## Shared task observer helpers

- `base_task_observer.py`: shared execution-config and execution-log behavior for cron/webhook observers
- `task_runner.py`: shared one-shot task execution helpers used by cron/webhook/background

## Update/version flow

- `version.py`: current version + PyPI metadata helpers
- `updater.py`: `UpdateObserver` and upgrade pipeline helpers
- upgrade sentinel is consumed on next startup for post-upgrade user message
