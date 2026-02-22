"""Platform-dispatching service management for ductor.

On Linux: delegates to systemd user service (service_linux).
On Windows: delegates to Windows Task Scheduler (service_windows).
"""

from __future__ import annotations

import sys
from typing import TYPE_CHECKING

if sys.platform == "win32":
    from ductor_bot.infra import service_windows as _backend
elif sys.platform == "darwin":
    from ductor_bot.infra import service_macos as _backend
else:
    from ductor_bot.infra import service_linux as _backend

if TYPE_CHECKING:
    from rich.console import Console


def is_service_available() -> bool:
    """Check if background service management is available on this platform."""
    return _backend.is_service_available()


def is_service_installed() -> bool:
    """Check if the ductor service is installed."""
    return _backend.is_service_installed()


def is_service_running() -> bool:
    """Check if the ductor service is currently running."""
    return _backend.is_service_running()


def install_service(console: Console | None = None) -> bool:
    """Install and start the ductor background service. Returns True on success."""
    return _backend.install_service(console)


def uninstall_service(console: Console | None = None) -> bool:
    """Stop and remove the ductor background service."""
    return _backend.uninstall_service(console)


def start_service(console: Console | None = None) -> None:
    """Start the service."""
    _backend.start_service(console)


def stop_service(console: Console | None = None) -> None:
    """Stop the service."""
    _backend.stop_service(console)


def print_service_status(console: Console | None = None) -> None:
    """Print the service status."""
    _backend.print_service_status(console)


def print_service_logs(console: Console | None = None) -> None:
    """Show service logs."""
    _backend.print_service_logs(console)
