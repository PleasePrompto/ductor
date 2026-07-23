"""Bot lifecycle CLI commands (stop, start, restart, uninstall, upgrade)."""

from __future__ import annotations

import asyncio
import json
import os
import shutil
import subprocess
import sys
import time
from typing import NoReturn

from rich.console import Console
from rich.panel import Panel

from ductor_bot.cli_commands.docker import _stop_docker_container
from ductor_bot.i18n import t_rich
from ductor_bot.infra.fs import robust_rmtree
from ductor_bot.infra.platform import is_windows
from ductor_bot.infra.restart import EXIT_RESTART
from ductor_bot.workspace.paths import resolve_paths

_console = Console()


def _re_exec_bot() -> NoReturn:
    """Re-exec the bot process (cross-platform).

    Spawns a new Python process running ``ductor_bot`` and exits the current one.
    Under a service manager the caller should ``sys.exit(EXIT_RESTART)`` instead.
    """
    subprocess.Popen([sys.executable, "-m", "ductor_bot"])
    sys.exit(0)


def _stop_service_if_running() -> None:
    """Stop the system service if installed and running."""
    import contextlib

    with contextlib.suppress(Exception):
        from ductor_bot.infra.service import is_service_installed, is_service_running, stop_service

        if is_service_installed() and is_service_running():
            stop_service(_console)


def stop_bot() -> None:
    """Stop all running ductor instances and Docker container.

    1. Stop the system service (prevents Task Scheduler/systemd/launchd respawn)
    2. Kill the PID-file instance
    3. Kill any remaining ductor processes system-wide
    4. Wait for file locks to release (Windows only)
    5. Stop Docker container if enabled
    """
    from ductor_bot.infra.pidlock import _is_process_alive, _kill_and_wait

    # 1. Stop service to prevent respawn
    _stop_service_if_running()

    # 2. Kill PID-file instance
    paths = resolve_paths()
    pid_file = paths.ductor_home / "bot.pid"
    stopped = False

    if pid_file.exists():
        try:
            pid = int(pid_file.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            pid = None
        if pid is not None and _is_process_alive(pid):
            _console.print(t_rich("lifecycle.stopping_bot", pid=pid))
            _kill_and_wait(pid)
            pid_file.unlink(missing_ok=True)
            _console.print(t_rich("lifecycle.bot_stopped"))
            stopped = True
        else:
            pid_file.unlink(missing_ok=True)

    # 3. Kill all remaining ductor processes system-wide
    from ductor_bot.infra.process_tree import kill_all_ductor_processes

    extra = kill_all_ductor_processes()
    if extra:
        _console.print(t_rich("lifecycle.killed_extra", count=extra))
        stopped = True

    if not stopped:
        _console.print(t_rich("lifecycle.no_instance"))

    # 4. Brief wait for file locks to release on Windows
    if is_windows() and stopped:
        time.sleep(1.0)

    # 5. Stop Docker container if enabled in config
    config_path = paths.config_path
    if config_path.exists():
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
            docker = data.get("docker", {})
            if isinstance(docker, dict) and docker.get("enabled"):
                container = str(docker.get("container_name", "ductor-sandbox"))
                _stop_docker_container(container)
        except (json.JSONDecodeError, OSError):
            pass


def start_bot(verbose: bool = False) -> None:
    """Load config and start the Telegram bot."""
    import logging

    from ductor_bot.__main__ import load_config, run_telegram
    from ductor_bot.logging_config import setup_logging

    paths = resolve_paths()
    setup_logging(verbose=verbose, log_dir=paths.logs_dir)
    config = load_config()
    from ductor_bot.i18n import init as init_i18n

    init_i18n(config.language)
    if not verbose:
        config_level = getattr(logging, config.log_level.upper(), logging.INFO)
        if config_level != logging.INFO:
            setup_logging(level=config_level, log_dir=paths.logs_dir)
    try:
        exit_code = asyncio.run(run_telegram(config))
    except KeyboardInterrupt:
        exit_code = 0
    if exit_code == EXIT_RESTART:
        if os.environ.get("DUCTOR_SUPERVISOR") or os.environ.get("INVOCATION_ID"):
            sys.exit(EXIT_RESTART)
        _re_exec_bot()
    elif exit_code:
        sys.exit(exit_code)


def cmd_restart() -> None:
    """Stop and re-exec the bot."""
    stop_bot()
    _re_exec_bot()


def uninstall() -> None:
    """Full uninstall: stop bot, remove Docker, delete workspace, uninstall package."""
    import questionary

    _console.print()
    _console.print(
        Panel(
            t_rich("lifecycle.uninstall.body"),
            title=t_rich("lifecycle.uninstall.title"),
            border_style="red",
            padding=(1, 2),
        ),
    )

    confirmed: bool | None = questionary.confirm(
        t_rich("lifecycle.uninstall.confirm"),
        default=False,
    ).ask()
    if not confirmed:
        _console.print(f"\n{t_rich('lifecycle.uninstall.cancelled')}\n")
        return

    # 1. Stop bot + Docker container + all ductor processes
    stop_bot()

    # 2. Remove Docker image
    paths = resolve_paths()
    if paths.config_path.exists():
        try:
            data = json.loads(paths.config_path.read_text(encoding="utf-8"))
            docker = data.get("docker", {})
            if isinstance(docker, dict) and docker.get("enabled") and shutil.which("docker"):
                image = str(docker.get("image_name", "ductor-sandbox"))
                _console.print(t_rich("lifecycle.uninstall.removing_image", image=image))
                subprocess.run(
                    ["docker", "rmi", image],
                    capture_output=True,
                    check=False,
                )
                _console.print(t_rich("lifecycle.uninstall.image_removed"))
        except (json.JSONDecodeError, OSError):
            pass

    # 3. Delete workspace
    ductor_home = paths.ductor_home
    if ductor_home.exists():
        robust_rmtree(ductor_home)
        if ductor_home.exists():
            _console.print(t_rich("lifecycle.uninstall.delete_warning", home=ductor_home))
        else:
            _console.print(t_rich("lifecycle.uninstall.deleted", home=ductor_home))

    # 4. Uninstall package
    _console.print(t_rich("lifecycle.uninstall.uninstalling"))
    if shutil.which("pipx"):
        subprocess.run(
            ["pipx", "uninstall", "ductor"],
            capture_output=True,
            check=False,
        )
    else:
        subprocess.run(
            [sys.executable, "-m", "pip", "uninstall", "-y", "ductor"],
            capture_output=True,
            check=False,
        )

    _console.print(
        Panel(
            t_rich("lifecycle.uninstall.done_body"),
            title=t_rich("lifecycle.uninstall.done_title"),
            border_style="green",
            padding=(1, 2),
        ),
    )
    _console.print()


def upgrade() -> None:
    """Stop bot, upgrade package, restart."""
    from ductor_bot.infra.install import detect_install_mode
    from ductor_bot.infra.updater import perform_upgrade_pipeline
    from ductor_bot.infra.version import get_current_version

    mode = detect_install_mode()
    if mode == "dev":
        _console.print(
            Panel(
                t_rich("lifecycle.upgrade.dev_body"),
                title=t_rich("lifecycle.upgrade.dev_title"),
                border_style="yellow",
                padding=(1, 2),
            ),
        )
        return

    _console.print()
    _console.print(
        Panel(
            t_rich("lifecycle.upgrade.body"),
            title=t_rich("lifecycle.upgrade.title"),
            border_style="cyan",
            padding=(1, 2),
        ),
    )

    current = get_current_version()

    # 1. Graceful stop
    stop_bot()

    # 2. Upgrade + verification pipeline
    _console.print(t_rich("lifecycle.upgrade.upgrading"))
    changed, actual, output = asyncio.run(
        perform_upgrade_pipeline(current_version=current),
    )
    if output:
        _console.print(f"[dim]{output}[/dim]")

    if not changed:
        _console.print(t_rich("lifecycle.upgrade.unchanged", version=actual))
        return

    _console.print(t_rich("lifecycle.upgrade.complete", old=current, new=actual))

    # 3. Re-exec with new version
    _console.print(t_rich("lifecycle.upgrade.restarting"))
    _re_exec_bot()
