"""Systemd user service management for ductor (Linux)."""

from __future__ import annotations

import getpass
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING

from ductor_bot.i18n import t_rich
from ductor_bot.infra.service_base import (
    collect_nvm_bin_dirs,
    ensure_console,
    find_ductor_binary,
    print_binary_not_found,
    print_install_success,
    print_not_installed,
    print_not_running,
    print_removed,
    print_start_failed,
    print_started,
    print_stopped,
)
from ductor_bot.infra.service_logs import print_journal_service_logs

if TYPE_CHECKING:
    from rich.console import Console

logger = logging.getLogger(__name__)

_SERVICE_NAME = "ductor"
_SERVICE_FILE = f"{_SERVICE_NAME}.service"


def _systemd_user_dir() -> Path:
    return Path.home() / ".config" / "systemd" / "user"


def _service_path() -> Path:
    return _systemd_user_dir() / _SERVICE_FILE


def _has_systemd() -> bool:
    """Check if systemd is available."""
    return shutil.which("systemctl") is not None


def _has_linger() -> bool:
    """Check if loginctl linger is enabled for the current user."""
    user = getpass.getuser()
    linger_dir = Path(f"/var/lib/systemd/linger/{user}")
    return linger_dir.exists()


def _enable_linger(user: str) -> bool:
    """Enable loginctl linger for *user*, using sudo only when needed and available."""
    cmd = ["loginctl", "enable-linger", user]
    if os.geteuid() != 0:
        if shutil.which("sudo") is None:
            return False
        cmd = ["sudo", *cmd]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=False)
    except OSError:
        return False
    return result.returncode == 0


def _run_systemctl(*args: str, user: bool = True) -> subprocess.CompletedProcess[str]:
    """Run a systemctl command."""
    cmd = ["systemctl"]
    if user:
        cmd.append("--user")
    cmd.extend(args)
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def _generate_service_unit(binary_path: str) -> str:
    """Generate the systemd service unit file content."""
    home = Path.home()
    path_dirs = [
        str(home / ".local" / "bin"),
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
    ]
    nvm_bins = collect_nvm_bin_dirs(home)
    if nvm_bins:
        path_dirs = [*nvm_bins, *path_dirs]

    path_dirs = list(dict.fromkeys(path_dirs))

    path_value = ":".join(path_dirs)

    return f"""\
[Unit]
Description=ductor - Telegram bot powered by AI CLIs
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
ExecStart={binary_path}
Restart=on-failure
RestartSec=5
Environment=PATH={path_value}
Environment=HOME={home}
Environment=DUCTOR_SUPERVISOR=1

[Install]
WantedBy=default.target
"""


def is_service_installed() -> bool:
    """Check if the ductor service is installed."""
    return _service_path().exists()


def is_service_running() -> bool:
    """Check if the ductor service is currently running."""
    if not _has_systemd() or not is_service_installed():
        return False
    result = _run_systemctl("is-active", _SERVICE_NAME)
    return result.stdout.strip() == "active"


def is_service_available() -> bool:
    """Check if systemd service management is available on this system."""
    return _has_systemd()


def install_service(console: Console | None = None) -> bool:
    """Install and start the ductor systemd user service.

    Returns True on success.
    """
    console = ensure_console(console)

    if not _has_systemd():
        console.print(t_rich("service.linux.no_systemd"))
        return False

    binary = find_ductor_binary()
    if not binary:
        print_binary_not_found(console)
        return False

    service_dir = _systemd_user_dir()
    service_dir.mkdir(parents=True, exist_ok=True)
    service_file = _service_path()
    service_file.write_text(_generate_service_unit(binary), encoding="utf-8")
    logger.info("Service file written: %s", service_file)

    _run_systemctl("daemon-reload")
    _run_systemctl("enable", _SERVICE_NAME)
    logger.info("Service enabled")

    if not _has_linger():
        console.print(f"\n{t_rich('service.linux.linger_warning')}")
        user = getpass.getuser()
        if _enable_linger(user):
            console.print(t_rich("service.linux.linger_enabled"))
        else:
            console.print(t_rich("service.linux.linger_manual", user=user))

    result = _run_systemctl("start", _SERVICE_NAME)
    if result.returncode != 0:
        console.print(t_rich("service.linux.start_failed", error=result.stderr.strip()))
        return False

    print_install_success(
        console,
        detail=t_rich("service.linux.detail"),
        logs_hint="View live logs",
    )
    return True


def uninstall_service(console: Console | None = None) -> bool:
    """Stop, disable, and remove the ductor service."""
    console = ensure_console(console)

    if not _has_systemd():
        console.print("[dim]systemd not available.[/dim]")
        return False

    if not is_service_installed():
        console.print("[dim]No service installed.[/dim]")
        return False

    _run_systemctl("stop", _SERVICE_NAME)
    _run_systemctl("disable", _SERVICE_NAME)
    _service_path().unlink(missing_ok=True)
    _run_systemctl("daemon-reload")

    print_removed(console)
    return True


def start_service(console: Console | None = None) -> None:
    """Start the service."""
    console = ensure_console(console)

    if not _has_systemd():
        console.print("[dim]systemd not available.[/dim]")
        return

    if not is_service_installed():
        print_not_installed(console)
        return

    result = _run_systemctl("start", _SERVICE_NAME)
    if result.returncode == 0:
        print_started(console)
    else:
        print_start_failed(console, result.stderr.strip())


def stop_service(console: Console | None = None) -> None:
    """Stop the service."""
    console = ensure_console(console)
    if is_service_running():
        _run_systemctl("stop", _SERVICE_NAME)
        print_stopped(console)
    else:
        print_not_running(console)


def print_service_status(console: Console | None = None) -> None:
    """Print the service status."""
    console = ensure_console(console)

    if not _has_systemd():
        console.print("[dim]systemd not available.[/dim]")
        return

    if not is_service_installed():
        print_not_installed(console)
        return

    result = _run_systemctl("status", _SERVICE_NAME, "--no-pager")
    console.print(result.stdout or result.stderr)


def print_service_logs(console: Console | None = None) -> None:
    """Show live journal logs for the service."""
    console = ensure_console(console)
    print_journal_service_logs(
        console,
        installed=is_service_installed(),
        service_name=_SERVICE_NAME,
    )
