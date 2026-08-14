"""macOS launchd Launch Agent service management for ductor."""

from __future__ import annotations

import logging
import plistlib
import shutil
import subprocess
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ductor_bot.i18n import t_rich
from ductor_bot.infra.service_base import (
    collect_nvm_bin_dirs,
    ensure_console,
    find_ductor_binary,
    print_binary_not_found,
    print_install_success,
    print_no_service,
    print_not_installed,
    print_not_running,
    print_removed,
    print_start_failed,
    print_started,
    print_stop_failed,
    print_stopped,
)
from ductor_bot.infra.service_logs import print_file_service_logs
from ductor_bot.workspace.paths import resolve_paths

if TYPE_CHECKING:
    from rich.console import Console

logger = logging.getLogger(__name__)

_LABEL = "dev.ductor"
_PLIST_NAME = f"{_LABEL}.plist"


def _launch_agents_dir() -> Path:
    return Path.home() / "Library" / "LaunchAgents"


def _plist_path() -> Path:
    return _launch_agents_dir() / _PLIST_NAME


def _run_launchctl(*args: str) -> subprocess.CompletedProcess[str]:
    """Run a launchctl command."""
    return subprocess.run(
        ["launchctl", *args],
        capture_output=True,
        text=True,
        check=False,
    )


def _generate_plist_data(binary_path: str) -> dict[str, Any]:
    """Generate the plist dictionary for a macOS Launch Agent.

    Creates an agent that:
    - Starts on user login (RunAtLoad)
    - Restarts only on crash, not on clean exit (KeepAlive/SuccessfulExit=false)
    - Throttles restarts to 10s intervals
    - Runs as a background process
    - Sets PATH to include common binary locations
    """
    home = Path.home()
    paths = resolve_paths()

    path_dirs = [
        str(home / ".local" / "bin"),
        str(home / ".npm-global" / "bin"),
        "/opt/homebrew/bin",
        "/usr/local/bin",
        "/usr/bin",
        "/bin",
    ]
    nvm_bins = collect_nvm_bin_dirs(home)
    if nvm_bins:
        path_dirs = [*nvm_bins, *path_dirs]

    path_dirs = list(dict.fromkeys(path_dirs))

    return {
        "Label": _LABEL,
        "ProgramArguments": [binary_path],
        "RunAtLoad": True,
        "KeepAlive": {"SuccessfulExit": False},
        "ThrottleInterval": 10,
        "ProcessType": "Background",
        "EnvironmentVariables": {
            "PATH": ":".join(path_dirs),
            "HOME": str(home),
            "DUCTOR_SUPERVISOR": "1",
        },
        "StandardOutPath": str(paths.logs_dir / "service.log"),
        "StandardErrorPath": str(paths.logs_dir / "service.err"),
    }


def is_service_available() -> bool:
    """Check if launchd service management is available on this system."""
    return shutil.which("launchctl") is not None


def is_service_installed() -> bool:
    """Check if the ductor Launch Agent plist exists."""
    return _plist_path().exists()


def is_service_running() -> bool:
    """Check if the ductor Launch Agent is currently running."""
    if not is_service_installed():
        return False
    result = _run_launchctl("list", _LABEL)
    if result.returncode != 0:
        return False
    return '"PID"' in result.stdout


def install_service(console: Console | None = None) -> bool:
    """Install and start the ductor Launch Agent.

    Returns True on success.
    """
    console = ensure_console(console)

    if not is_service_available():
        console.print(t_rich("service.macos.no_launchctl"))
        return False

    binary = find_ductor_binary()
    if not binary:
        print_binary_not_found(console)
        return False

    # Unload existing agent if present (clean re-install)
    if is_service_installed():
        _run_launchctl("unload", "-w", str(_plist_path()))

    # Ensure log directory exists
    paths = resolve_paths()
    paths.logs_dir.mkdir(parents=True, exist_ok=True)

    # Write plist
    plist_path = _plist_path()
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_data = _generate_plist_data(binary)
    plist_path.write_bytes(plistlib.dumps(plist_data, fmt=plistlib.FMT_XML))
    plist_path.chmod(0o644)
    logger.info("Launch Agent plist written: %s", plist_path)

    # Load and enable
    result = _run_launchctl("load", "-w", str(plist_path))
    if result.returncode != 0:
        console.print(t_rich("service.macos.start_failed", error=result.stderr.strip()))
        return False

    logger.info("Launch Agent loaded: %s", _LABEL)

    print_install_success(
        console,
        detail=t_rich("service.macos.detail"),
    )
    return True


def uninstall_service(console: Console | None = None) -> bool:
    """Stop and remove the ductor Launch Agent."""
    console = ensure_console(console)

    if not is_service_installed():
        print_no_service(console)
        return False

    result = _run_launchctl("unload", "-w", str(_plist_path()))
    if result.returncode != 0:
        console.print(t_rich("service.macos.unload_failed", error=result.stderr.strip()))
        return False

    _plist_path().unlink(missing_ok=True)
    print_removed(console)
    return True


def start_service(console: Console | None = None) -> None:
    """Start the Launch Agent."""
    console = ensure_console(console)

    if not is_service_installed():
        print_not_installed(console)
        return

    result = _run_launchctl("start", _LABEL)
    if result.returncode == 0:
        print_started(console)
    else:
        print_start_failed(console, result.stderr.strip())


def stop_service(console: Console | None = None) -> None:
    """Stop the Launch Agent."""
    console = ensure_console(console)

    if not is_service_running():
        print_not_running(console)
        return

    result = _run_launchctl("stop", _LABEL)
    if result.returncode == 0:
        print_stopped(console)
    else:
        print_stop_failed(console, result.stderr.strip())


def print_service_status(console: Console | None = None) -> None:
    """Print the Launch Agent status."""
    console = ensure_console(console)

    if not is_service_installed():
        print_not_installed(console)
        return

    result = _run_launchctl("list", _LABEL)
    if result.returncode == 0:
        console.print(result.stdout)
    else:
        console.print(t_rich("service.macos.not_loaded"))


def print_service_logs(console: Console | None = None) -> None:
    """Show recent log output."""
    console = ensure_console(console)
    print_file_service_logs(
        console,
        installed=is_service_installed(),
        logs_dir=resolve_paths().logs_dir,
    )
