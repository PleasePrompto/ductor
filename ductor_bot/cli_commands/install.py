"""CLI command: ductor install <extra>."""

from __future__ import annotations

import subprocess
import sys

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from ductor_bot.i18n import t_rich
from ductor_bot.infra.install import detect_install_mode

# Available extras and their key package + description
_EXTRAS: dict[str, tuple[str, str]] = {
    "matrix": ("nio", "Matrix messenger support (matrix-nio)"),
    "slack": ("slack_bolt", "Slack messenger support (slack-bolt)"),
    "api": ("nacl", "WebSocket API with E2E encryption (PyNaCl)"),
}


def _is_installed(module_name: str) -> bool:
    """Check if a Python module is importable."""
    try:
        __import__(module_name)
    except ImportError:
        return False
    return True


def _install_extra(name: str) -> None:
    """Install an optional extra dependency."""
    console = Console()

    if name not in _EXTRAS:
        console.print(f"[red]Unknown extra: {name}[/red]")
        print_install_help()
        return

    module, description = _EXTRAS[name]

    if _is_installed(module):
        console.print(t_rich("install.already", description=description))
        return

    mode = detect_install_mode()

    if mode == "pipx":
        cmd = [sys.executable, "-m", "pip", "install", f"ductor[{name}]"]
    elif mode == "dev":
        cmd = [sys.executable, "-m", "pip", "install", "-e", f".[{name}]"]
    else:
        cmd = [sys.executable, "-m", "pip", "install", f"ductor[{name}]"]

    console.print(t_rich("install.installing", description=description))
    console.print(t_rich("install.command", cmd=" ".join(cmd)))

    result = subprocess.run(cmd, capture_output=True, text=True, check=False)

    if result.returncode == 0:
        console.print(t_rich("install.done", description=description))
    else:
        console.print(t_rich("install.failed"))
        if result.stderr:
            console.print(f"[dim]{result.stderr.strip()}[/dim]")


def print_install_help() -> None:
    """Show available extras."""
    table = Table(show_header=True, header_style="bold")
    table.add_column(t_rich("install.col_extra"))
    table.add_column(t_rich("install.col_description"))
    table.add_column(t_rich("install.col_status"))

    for name, (module, desc) in _EXTRAS.items():
        status = (
            t_rich("install.status_installed")
            if _is_installed(module)
            else t_rich("install.status_not_installed")
        )
        table.add_row(name, desc, status)

    Console().print(Panel(table, title="ductor install <extra>"))


def cmd_install(args: list[str]) -> None:
    """Handle `ductor install <extra>`."""
    if len(args) < 2 or args[1] not in _EXTRAS:
        print_install_help()
        return
    _install_extra(args[1])
