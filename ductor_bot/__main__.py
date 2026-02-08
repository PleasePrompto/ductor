"""Entry point: python -m ductor_bot."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import NoReturn

from rich.console import Console
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from ductor_bot.config import AgentConfig, deep_merge_config
from ductor_bot.infra.restart import EXIT_RESTART
from ductor_bot.logging_config import setup_logging
from ductor_bot.workspace.init import init_workspace
from ductor_bot.workspace.paths import DuctorPaths, resolve_paths

logger = logging.getLogger(__name__)

_console = Console()

_IS_WINDOWS = sys.platform == "win32"


def _re_exec_bot() -> NoReturn:
    """Re-exec the bot process (cross-platform).

    On POSIX: replaces current process via ``os.execv``.
    On Windows: spawns new process and exits (``os.execv`` doesn't truly replace).
    """
    args = [sys.executable, "-m", "ductor_bot"]
    if _IS_WINDOWS:
        subprocess.Popen(args)
        sys.exit(0)
    else:
        os.execv(sys.executable, args)  # noqa: S606


# ---------------------------------------------------------------------------
# Config helpers
# ---------------------------------------------------------------------------


def _is_configured() -> bool:
    """Check if bot has a valid configuration."""
    paths = resolve_paths()
    if not paths.config_path.exists():
        return False
    try:
        data = json.loads(paths.config_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return False
    token = data.get("telegram_token", "")
    users = data.get("allowed_user_ids", [])
    return bool(token) and not str(token).startswith("YOUR_") and bool(users)


def load_config() -> AgentConfig:
    """Load, auto-create, and smart-merge the bot config.

    Resolution order:
    1. ``~/.ductor/config/config.json`` (canonical location)
    2. Copy from ``config.example.json`` in the framework root on first start
    3. Fall back to Pydantic defaults if example file is missing

    On every load the config is deep-merged with current Pydantic defaults
    so that new fields from framework updates are added without destroying
    user settings.
    """
    paths = resolve_paths()
    config_path = paths.config_path

    first_start = not config_path.exists()

    if first_start:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        example = paths.config_example_path
        if example.is_file():
            shutil.copy2(example, config_path)
            logger.info("Created config from config.example.json at %s", config_path)
        else:
            defaults = AgentConfig().model_dump(mode="json")
            config_path.write_text(
                json.dumps(defaults, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            logger.info("Created default config at %s", config_path)

    try:
        user_data: dict[str, object] = json.loads(config_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.exception("Failed to parse config at %s", config_path)
        sys.exit(1)
    defaults = AgentConfig().model_dump(mode="json")
    merged, changed = deep_merge_config(user_data, defaults)

    if changed:
        config_path.write_text(
            json.dumps(merged, indent=2, ensure_ascii=False) + "\n",
            encoding="utf-8",
        )
        logger.info("Extended config with new default fields")

    init_workspace(paths)
    return AgentConfig.model_validate(merged)


# ---------------------------------------------------------------------------
# Bot lifecycle
# ---------------------------------------------------------------------------


async def run_telegram(config: AgentConfig) -> int:
    """Validate config and run the Telegram bot.

    Returns the exit code from the bot (``0`` = clean, ``42`` = restart requested).
    """
    paths = resolve_paths(ductor_home=config.ductor_home)

    missing_token = not config.telegram_token or config.telegram_token.startswith("YOUR_")
    if missing_token or not config.allowed_user_ids:
        _console.print(
            "[bold yellow]Config is incomplete. Run [bold]ductor onboarding[/bold].[/bold yellow]"
        )
        sys.exit(1)

    from ductor_bot.bot.app import TelegramBot
    from ductor_bot.infra.pidlock import acquire_lock, release_lock

    acquire_lock(pid_file=paths.ductor_home / "bot.pid", kill_existing=True)

    bot = TelegramBot(config)
    exit_code = 0
    try:
        exit_code = await bot.run()
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        await bot.shutdown()
        release_lock(pid_file=paths.ductor_home / "bot.pid")
    return exit_code


def _start_bot(verbose: bool = False) -> None:
    """Load config and start the Telegram bot."""
    paths = resolve_paths()
    setup_logging(verbose=verbose, log_dir=paths.logs_dir)
    config = load_config()
    if not verbose:
        config_level = getattr(logging, config.log_level.upper(), logging.INFO)
        if config_level != logging.INFO:
            setup_logging(level=config_level, log_dir=paths.logs_dir)
    exit_code = asyncio.run(run_telegram(config))
    if exit_code == EXIT_RESTART:
        if os.environ.get("DUCTOR_SUPERVISOR"):
            sys.exit(EXIT_RESTART)
        _re_exec_bot()
    elif exit_code:
        sys.exit(exit_code)


def _stop_bot() -> None:
    """Stop running bot instance and Docker container if active."""
    from ductor_bot.infra.pidlock import _is_process_alive, _kill_and_wait

    paths = resolve_paths()
    pid_file = paths.ductor_home / "bot.pid"
    stopped = False

    if pid_file.exists():
        try:
            pid = int(pid_file.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            pid = None
        if pid is not None and _is_process_alive(pid):
            _console.print(f"[dim]Stopping bot (pid={pid})...[/dim]")
            _kill_and_wait(pid)
            pid_file.unlink(missing_ok=True)
            _console.print("[green]Bot stopped.[/green]")
            stopped = True
        else:
            pid_file.unlink(missing_ok=True)

    if not stopped:
        _console.print("[dim]No running bot instance found.[/dim]")

    # Stop Docker container if enabled in config
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


def _stop_docker_container(container_name: str) -> None:
    """Stop and remove a Docker container."""
    if not shutil.which("docker"):
        return
    _console.print(f"[dim]Stopping Docker container '{container_name}'...[/dim]")
    subprocess.run(
        ["docker", "stop", "-t", "5", container_name],
        capture_output=True,
        check=False,
    )
    subprocess.run(
        ["docker", "rm", "-f", container_name],
        capture_output=True,
        check=False,
    )
    _console.print("[green]Docker container stopped.[/green]")


# ---------------------------------------------------------------------------
# Help & Status
# ---------------------------------------------------------------------------


def _print_usage() -> None:
    """Print commands and smart status information."""
    _console.print()
    banner_path = Path(__file__).resolve().parent / "_banner.txt"
    try:
        banner_text = banner_path.read_text(encoding="utf-8").rstrip()
    except OSError:
        banner_text = "ductor.dev"
    _console.print(
        Panel(
            Text(banner_text, style="bold cyan"),
            subtitle="[dim]ductor.dev[/dim]",
            border_style="cyan",
            padding=(0, 2),
        ),
    )

    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(style="bold green", min_width=24)
    table.add_column()
    table.add_row("ductor", "Start the bot (runs onboarding if needed)")
    table.add_row("ductor onboarding", "Setup wizard (resets if already configured)")
    table.add_row("ductor stop", "Stop running bot and Docker container")
    table.add_row("ductor restart", "Restart the bot")
    table.add_row("ductor reset", "Full reset and re-setup")
    table.add_row("ductor upgrade", "Stop, upgrade to latest, restart")
    table.add_row("ductor uninstall", "Remove everything and uninstall")
    table.add_row("ductor service install", "Run as background service (systemd)")
    table.add_row("ductor service", "Service management (status/stop/logs/...)")
    table.add_row("ductor status", "Show bot status, paths, and stats")
    table.add_row("ductor help", "Show this message")
    table.add_row("-v, --verbose", "Verbose logging output")

    _console.print(
        Panel(table, title="[bold]Commands[/bold]", border_style="blue", padding=(1, 0)),
    )

    if _is_configured():
        _print_status()
    else:
        _console.print(
            Panel(
                "[bold yellow]Not configured.[/bold yellow]\n\n"
                "Run [bold]ductor[/bold] to start the setup wizard.",
                title="[bold]Status[/bold]",
                border_style="yellow",
                padding=(1, 2),
            ),
        )
    _console.print()


def _build_status_lines(  # noqa: PLR0913
    *,
    bot_running: bool,
    bot_pid: int | None,
    bot_uptime: str,
    provider: str,
    model: str,
    docker_enabled: bool,
    docker_name: str | None,
    error_count: int,
    paths: DuctorPaths,
) -> list[str]:
    """Assemble the status panel content lines."""
    lines: list[str] = []
    if bot_running:
        lines.append(f"[bold green]Running[/bold green]  pid={bot_pid}  uptime: {bot_uptime}")
    else:
        lines.append("[dim]Not running[/dim]")
    lines.append(f"Provider:  [cyan]{provider}[/cyan] ({model})")
    if docker_enabled:
        lines.append(f"Docker:    [green]enabled[/green] ({docker_name})")
    else:
        lines.append("Docker:    [dim]disabled[/dim]")
    if error_count > 0:
        lines.append(f"Errors:    [bold red]{error_count}[/bold red] in latest log")
    else:
        lines.append("Errors:    [green]0[/green]")
    lines.append("")
    lines.append("[bold]Paths:[/bold]")
    lines.append(f"  Home:       [cyan]{paths.ductor_home}[/cyan]")
    lines.append(f"  Config:     [cyan]{paths.config_path}[/cyan]")
    lines.append(f"  Workspace:  [cyan]{paths.workspace}[/cyan]")
    lines.append(f"  Logs:       [cyan]{paths.logs_dir}[/cyan]")
    lines.append(f"  Sessions:   [cyan]{paths.sessions_path}[/cyan]")
    return lines


def _print_status() -> None:
    """Print bot status, paths, and runtime info."""
    paths = resolve_paths()
    try:
        data: dict[str, object] = json.loads(
            paths.config_path.read_text(encoding="utf-8"),
        )
    except (json.JSONDecodeError, OSError):
        return

    provider = data.get("provider", "claude")
    model = data.get("model", "opus")
    docker_cfg = data.get("docker", {})
    docker_enabled = isinstance(docker_cfg, dict) and bool(docker_cfg.get("enabled"))
    docker_name: str | None = None
    if docker_enabled and isinstance(docker_cfg, dict):
        docker_name = str(docker_cfg.get("container_name", "ductor-sandbox"))

    # Running state
    pid_file = paths.ductor_home / "bot.pid"
    bot_running = False
    bot_pid: int | None = None
    bot_uptime = ""
    if pid_file.exists():
        try:
            bot_pid = int(pid_file.read_text(encoding="utf-8").strip())
        except (ValueError, OSError):
            bot_pid = None
        if bot_pid is not None:
            from ductor_bot.infra.pidlock import _is_process_alive

            bot_running = _is_process_alive(bot_pid)
            if bot_running:
                mtime = datetime.fromtimestamp(pid_file.stat().st_mtime, tz=UTC)
                delta = datetime.now(UTC) - mtime
                hours, remainder = divmod(int(delta.total_seconds()), 3600)
                minutes, _ = divmod(remainder, 60)
                bot_uptime = f"{hours}h {minutes}m"

    # Error count from latest log
    error_count = _count_log_errors(paths.logs_dir)

    # Build status lines
    lines = _build_status_lines(
        bot_running=bot_running,
        bot_pid=bot_pid,
        bot_uptime=bot_uptime,
        provider=str(provider),
        model=str(model),
        docker_enabled=docker_enabled,
        docker_name=str(docker_name) if docker_name else None,
        error_count=error_count,
        paths=paths,
    )

    _console.print(
        Panel(
            "\n".join(lines),
            title="[bold]Status[/bold]",
            border_style="green",
            padding=(1, 2),
        ),
    )


def _count_log_errors(log_dir: Path) -> int:
    """Count ERROR entries in the most recent log file."""
    if not log_dir.is_dir():
        return 0
    log_files = sorted(
        log_dir.glob("ductor*.log"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not log_files:
        return 0
    try:
        return log_files[0].read_text(encoding="utf-8", errors="replace").count(" ERROR ")
    except OSError:
        return 0


# ---------------------------------------------------------------------------
# Uninstall
# ---------------------------------------------------------------------------


def _uninstall() -> None:
    """Full uninstall: stop bot, remove Docker, delete workspace, uninstall package."""
    import questionary

    _console.print()
    _console.print(
        Panel(
            "[bold red]This will permanently remove ductor from your system.[/bold red]\n\n"
            "  1. Stop the running bot (if active)\n"
            "  2. Remove Docker container and image (if used)\n"
            "  3. Delete all data in ~/.ductor/\n"
            "  4. Uninstall the ductor package",
            title="[bold red]Uninstall ductor[/bold red]",
            border_style="red",
            padding=(1, 2),
        ),
    )

    confirmed: bool | None = questionary.confirm(
        "Are you sure you want to uninstall everything?",
        default=False,
    ).ask()
    if not confirmed:
        _console.print("\n[dim]Uninstall cancelled.[/dim]\n")
        return

    # 1. Stop bot + Docker container
    _stop_bot()

    # 2. Remove Docker image
    paths = resolve_paths()
    if paths.config_path.exists():
        try:
            data = json.loads(paths.config_path.read_text(encoding="utf-8"))
            docker = data.get("docker", {})
            if isinstance(docker, dict) and docker.get("enabled") and shutil.which("docker"):
                image = str(docker.get("image_name", "ductor-sandbox"))
                _console.print(f"[dim]Removing Docker image '{image}'...[/dim]")
                subprocess.run(
                    ["docker", "rmi", image],
                    capture_output=True,
                    check=False,
                )
                _console.print("[green]Docker image removed.[/green]")
        except (json.JSONDecodeError, OSError):
            pass

    # 3. Delete workspace
    ductor_home = paths.ductor_home
    if ductor_home.exists():
        shutil.rmtree(ductor_home)
        _console.print(f"[green]Deleted {ductor_home}[/green]")

    # 4. Uninstall package
    _console.print("[dim]Uninstalling ductor package...[/dim]")
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
            "[bold green]ductor has been completely removed.[/bold green]\n\n"
            "Thank you for using ductor!",
            title="[bold green]Uninstalled[/bold green]",
            border_style="green",
            padding=(1, 2),
        ),
    )
    _console.print()


# ---------------------------------------------------------------------------
# Upgrade
# ---------------------------------------------------------------------------


def _upgrade() -> None:
    """Stop bot, upgrade package, restart."""
    from ductor_bot.infra.install import detect_install_mode

    mode = detect_install_mode()
    if mode == "dev":
        _console.print(
            Panel(
                "[bold yellow]Running from source (editable install).[/bold yellow]\n\n"
                "Self-upgrade is not available.\n"
                "Update with [bold]git pull[/bold] in your project directory.",
                title="[bold]Upgrade[/bold]",
                border_style="yellow",
                padding=(1, 2),
            ),
        )
        return

    _console.print()
    _console.print(
        Panel(
            "[bold cyan]Upgrading ductor...[/bold cyan]\n\n"
            "  1. Stop running bot gracefully\n"
            "  2. Upgrade to latest version\n"
            "  3. Restart",
            title="[bold]Upgrade[/bold]",
            border_style="cyan",
            padding=(1, 2),
        ),
    )

    # 1. Graceful stop
    _stop_bot()

    # 2. Upgrade
    _console.print("[dim]Upgrading package...[/dim]")
    if mode == "pipx":
        result = subprocess.run(
            ["pipx", "upgrade", "ductor"],
            capture_output=True,
            text=True,
            check=False,
        )
    else:
        result = subprocess.run(
            [sys.executable, "-m", "pip", "install", "--upgrade", "ductor"],
            capture_output=True,
            text=True,
            check=False,
        )

    if result.returncode != 0:
        _console.print(f"[bold red]Upgrade failed:[/bold red]\n{result.stderr or result.stdout}")
        return

    output = result.stdout.strip()
    if output:
        _console.print(f"[dim]{output}[/dim]")
    _console.print("[green]Upgrade complete.[/green]")

    # 3. Re-exec with new version
    _console.print("[dim]Restarting...[/dim]")
    _re_exec_bot()


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------


def _cmd_status() -> None:
    """Show bot status or hint to configure."""
    _console.print()
    if _is_configured():
        _print_status()
    else:
        _console.print(
            Panel(
                "[bold yellow]Not configured.[/bold yellow]\n\n"
                "Run [bold]ductor[/bold] to start the setup wizard.",
                title="[bold]Status[/bold]",
                border_style="yellow",
                padding=(1, 2),
            ),
        )
    _console.print()


def _cmd_restart() -> None:
    """Stop and re-exec the bot."""
    _stop_bot()
    _re_exec_bot()


def _cmd_setup(verbose: bool) -> None:
    """Run onboarding (with smart reset if already configured), then start."""
    from ductor_bot.cli.init_wizard import run_onboarding, run_smart_reset

    _stop_bot()
    paths = resolve_paths()
    if _is_configured():
        run_smart_reset(paths.ductor_home)
    run_onboarding()
    _start_bot(verbose)


_COMMANDS: dict[str, str] = {
    "help": "help",
    "status": "status",
    "stop": "stop",
    "restart": "restart",
    "upgrade": "upgrade",
    "uninstall": "uninstall",
    "onboarding": "setup",
    "reset": "setup",
    "service": "service",
}

_SERVICE_SUBCOMMANDS = frozenset({"install", "status", "stop", "start", "logs", "uninstall"})


def _parse_service_subcommand(args: list[str]) -> str | None:
    """Extract the subcommand after 'service' from CLI args."""
    found_service = False
    for a in args:
        if a.startswith("-"):
            continue
        if not found_service and a == "service":
            found_service = True
            continue
        if found_service:
            return a if a in _SERVICE_SUBCOMMANDS else None
    return None


def _print_service_help() -> None:
    """Print the service subcommand help table."""
    _console.print()
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(style="bold green", min_width=30)
    table.add_column()
    table.add_row("ductor service install", "Install and start background service")
    table.add_row("ductor service status", "Show service status")
    table.add_row("ductor service start", "Start the service")
    table.add_row("ductor service stop", "Stop the service")
    table.add_row("ductor service logs", "View live logs")
    table.add_row("ductor service uninstall", "Remove the service")
    _console.print(
        Panel(table, title="[bold]Service Commands[/bold]", border_style="blue", padding=(1, 0)),
    )
    _console.print()


def _cmd_service(args: list[str]) -> None:
    """Handle 'ductor service <subcommand>'."""
    from ductor_bot.infra.service import (
        install_service,
        print_service_logs,
        print_service_status,
        start_service,
        stop_service,
        uninstall_service,
    )

    sub = _parse_service_subcommand(args)
    if sub is None:
        _print_service_help()
        return

    dispatch: dict[str, object] = {
        "install": lambda: install_service(_console),
        "status": lambda: print_service_status(_console),
        "start": lambda: start_service(_console),
        "stop": lambda: stop_service(_console),
        "logs": lambda: print_service_logs(_console),
        "uninstall": lambda: uninstall_service(_console),
    }
    _console.print()
    dispatch[sub]()  # type: ignore[operator]
    _console.print()


def _default_action(verbose: bool) -> None:
    """Auto-onboarding if unconfigured, then start bot."""
    if not _is_configured():
        from ductor_bot.cli.init_wizard import run_onboarding

        service_installed = run_onboarding()
        if service_installed:
            return
    _start_bot(verbose)


def main() -> None:
    """CLI entry point."""
    args = sys.argv[1:]
    commands = {a for a in args if not a.startswith("-")}
    verbose = "--verbose" in args or "-v" in args

    if "--help" in args or "-h" in args:
        commands.add("help")

    # Resolve first matching command
    action = next((_COMMANDS[c] for c in commands if c in _COMMANDS), None)

    dispatch: dict[str, object] = {
        "help": _print_usage,
        "status": _cmd_status,
        "stop": _stop_bot,
        "restart": _cmd_restart,
        "upgrade": _upgrade,
        "uninstall": _uninstall,
        "setup": lambda: _cmd_setup(verbose),
        "service": lambda: _cmd_service(args),
    }

    handler = dispatch.get(action) if action else None
    if handler is not None:
        handler()  # type: ignore[operator]
    else:
        _default_action(verbose)


if __name__ == "__main__":
    main()
