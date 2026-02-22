"""Systemd user service management for ductor (Linux)."""

from __future__ import annotations

import getpass
import logging
import os
import shutil
import subprocess
from pathlib import Path

from rich.console import Console
from rich.panel import Panel

logger = logging.getLogger(__name__)

_SERVICE_NAME = "ductor"
_SERVICE_FILE = f"{_SERVICE_NAME}.service"


def _systemd_user_dir() -> Path:
    return Path.home() / ".config" / "systemd" / "user"


def _service_path() -> Path:
    return _systemd_user_dir() / _SERVICE_FILE


def _find_ductor_binary() -> str | None:
    """Find the ductor binary path."""
    return shutil.which("ductor")


def _has_systemd() -> bool:
    """Check if systemd is available."""
    return shutil.which("systemctl") is not None


def _has_linger() -> bool:
    """Check if loginctl linger is enabled for the current user."""
    user = getpass.getuser()
    linger_dir = Path(f"/var/lib/systemd/linger/{user}")
    return linger_dir.exists()


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
    nvm_dir = home / ".nvm"
    if nvm_dir.is_dir():
        for node_dir in sorted(nvm_dir.glob("versions/node/*/bin"), reverse=True):
            path_dirs.insert(0, str(node_dir))
            break

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
    if console is None:
        console = Console()

    if not _has_systemd():
        console.print(
            "[bold red]systemd not found. Service install requires Linux with systemd.[/bold red]"
        )
        return False

    binary = _find_ductor_binary()
    if not binary:
        console.print("[bold red]Could not find the ductor binary in PATH.[/bold red]")
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
        console.print(
            "\n[bold yellow]Linger must be enabled so ductor keeps running "
            "after you log out.[/bold yellow]"
        )
        user = getpass.getuser()
        result = subprocess.run(
            ["sudo", "loginctl", "enable-linger", user],
            capture_output=True,
            text=True,
            check=False,
        )
        if result.returncode == 0:
            console.print("[green]Linger enabled.[/green]")
        else:
            console.print(
                f"[yellow]Could not enable linger automatically.[/yellow]\n"
                f"Run manually: [bold]sudo loginctl enable-linger {user}[/bold]"
            )

    result = _run_systemctl("start", _SERVICE_NAME)
    if result.returncode != 0:
        console.print(f"[bold red]Failed to start service:[/bold red] {result.stderr.strip()}")
        return False

    console.print(
        Panel(
            "[bold green]ductor is now running as a background service.[/bold green]\n\n"
            "It starts on boot and restarts on crash.\n\n"
            "[bold]Useful commands:[/bold]\n\n"
            "  [cyan]ductor service status[/cyan]     Check if it's running\n"
            "  [cyan]ductor service stop[/cyan]       Stop the service\n"
            "  [cyan]ductor service logs[/cyan]       View live logs\n"
            "  [cyan]ductor service uninstall[/cyan]  Remove the service",
            title="[bold green]Service Installed[/bold green]",
            border_style="green",
            padding=(1, 2),
        ),
    )
    return True


def uninstall_service(console: Console | None = None) -> bool:
    """Stop, disable, and remove the ductor service."""
    if console is None:
        console = Console()

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

    console.print("[green]Service removed.[/green]")
    return True


def start_service(console: Console | None = None) -> None:
    """Start the service."""
    if console is None:
        console = Console()

    if not _has_systemd():
        console.print("[dim]systemd not available.[/dim]")
        return

    if not is_service_installed():
        console.print("[dim]Service not installed. Run [bold]ductor service install[/bold].[/dim]")
        return

    result = _run_systemctl("start", _SERVICE_NAME)
    if result.returncode == 0:
        console.print("[green]Service started.[/green]")
    else:
        console.print(f"[red]Failed to start: {result.stderr.strip()}[/red]")


def stop_service(console: Console | None = None) -> None:
    """Stop the service."""
    if console is None:
        console = Console()
    if is_service_running():
        _run_systemctl("stop", _SERVICE_NAME)
        console.print("[green]Service stopped.[/green]")
    else:
        console.print("[dim]Service is not running.[/dim]")


def print_service_status(console: Console | None = None) -> None:
    """Print the service status."""
    if console is None:
        console = Console()

    if not _has_systemd():
        console.print("[dim]systemd not available.[/dim]")
        return

    if not is_service_installed():
        console.print("[dim]Service not installed. Run [bold]ductor service install[/bold].[/dim]")
        return

    result = _run_systemctl("status", _SERVICE_NAME, "--no-pager")
    console.print(result.stdout or result.stderr)


def print_service_logs(console: Console | None = None) -> None:
    """Show live journal logs for the service."""
    if console is None:
        console = Console()

    if not is_service_installed():
        console.print("[dim]Service not installed.[/dim]")
        return

    console.print("[dim]Showing logs (Ctrl+C to stop)...[/dim]\n")
    try:
        os.execvp(  # noqa: S606
            "journalctl",
            ["journalctl", "--user", "-u", _SERVICE_NAME, "-f", "--no-hostname"],
        )
    except FileNotFoundError:
        console.print("[bold red]journalctl not found.[/bold red]")
