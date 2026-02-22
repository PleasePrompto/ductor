"""Interactive onboarding wizard for first-time setup."""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import NoReturn
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import questionary
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from ductor_bot.cli.auth import AuthStatus, check_claude_auth, check_codex_auth
from ductor_bot.config import AgentConfig, deep_merge_config
from ductor_bot.workspace.init import init_workspace
from ductor_bot.workspace.paths import resolve_paths

_BANNER_PATH = Path(__file__).resolve().parent.parent / "_banner.txt"
logger = logging.getLogger(__name__)


def _load_banner() -> str:
    """Read ASCII art from bundled file."""
    try:
        return _BANNER_PATH.read_text(encoding="utf-8").rstrip()
    except OSError:
        return "ductor.dev"


_TOKEN_PATTERN = re.compile(r"^\d{8,}:[A-Za-z0-9_-]{30,}$")

_TIMEZONES: list[str] = [
    # Europe
    "Europe/Berlin",
    "Europe/London",
    "Europe/Paris",
    "Europe/Zurich",
    "Europe/Moscow",
    "Europe/Amsterdam",
    "Europe/Rome",
    "Europe/Madrid",
    # Americas
    "America/New_York",
    "America/Chicago",
    "America/Denver",
    "America/Los_Angeles",
    "America/Sao_Paulo",
    "America/Toronto",
    # Asia & Middle East
    "Asia/Tokyo",
    "Asia/Shanghai",
    "Asia/Kolkata",
    "Asia/Dubai",
    "Asia/Singapore",
    # Oceania & Other
    "Australia/Sydney",
    "Pacific/Auckland",
    "UTC",
]

_MANUAL_TZ_OPTION = "-> Enter manually"


def _abort() -> NoReturn:
    """Print abort message and exit."""
    Console().print("\n[dim]Setup cancelled.[/dim]\n")
    sys.exit(0)


def _show_banner(console: Console) -> None:
    """Display the ASCII art banner."""
    banner = Text(_load_banner(), style="bold cyan")
    console.print(
        Panel(
            banner,
            subtitle="[dim]ductor.dev[/dim]",
            border_style="cyan",
            padding=(0, 2),
        ),
    )


_STATUS_ICON = {
    AuthStatus.AUTHENTICATED: "[bold green]authenticated[/bold green]",
    AuthStatus.INSTALLED: "[bold yellow]installed but not logged in[/bold yellow]",
    AuthStatus.NOT_FOUND: "[dim]not found[/dim]",
}


def _check_clis(console: Console) -> None:
    """Detect CLI availability and require at least one authenticated provider."""
    claude = check_claude_auth()
    codex = check_codex_auth()

    lines = [
        "[bold]Detected AI Backends:[/bold]\n",
        f"  Claude Code CLI   {_STATUS_ICON[claude.status]}",
        f"  OpenAI Codex CLI  {_STATUS_ICON[codex.status]}",
    ]

    has_auth = claude.is_authenticated or codex.is_authenticated

    if has_auth:
        border = "green"
    else:
        border = "red"
        lines.append(
            "\n[bold red]At least one CLI must be installed and authenticated.[/bold red]\n\n"
            "  Claude: [dim]https://docs.anthropic.com/en/docs/claude-code[/dim]\n"
            "  Codex:  [dim]https://github.com/openai/codex[/dim]"
        )

    console.print(
        Panel(
            "\n".join(lines),
            title="[bold]CLI Backends[/bold]",
            border_style=border,
            padding=(1, 2),
        ),
    )

    if not has_auth:
        console.print()
        _abort()


def _show_disclaimer(console: Console) -> None:
    """Display the risk disclaimer and require confirmation."""
    disclaimer = (
        "[bold]Important -- please read before continuing.[/bold]\n\n"
        "ductor connects to [bold]Anthropic Claude CLI[/bold] and "
        "[bold]OpenAI Codex CLI[/bold] as AI agent backends.\n\n"
        "The bot operates in [bold yellow]full permission bypass mode[/bold yellow]. "
        "The agent can read, write, and delete files, execute commands, "
        "and interact with your system without asking for confirmation.\n\n"
        "While safeguards are in place, [bold red]unintended actions can occur[/bold red] "
        "-- including data loss, unexpected file changes, or unintended command execution.\n\n"
        "[bold green]We strongly recommend running ductor inside a Docker container[/bold green] "
        "to isolate it from your host system."
    )
    console.print(
        Panel(
            disclaimer,
            title="[bold yellow]Disclaimer[/bold yellow]",
            border_style="yellow",
            padding=(1, 2),
        )
    )

    accepted = questionary.confirm(
        "I understand the risks and want to continue.",
        default=False,
    ).ask()
    if not accepted:
        _abort()


def _ask_telegram_token(console: Console) -> str:
    """Prompt for the Telegram bot token with instructions."""
    instructions = (
        "[bold]How to get your Telegram Bot Token:[/bold]\n\n"
        "  1. Open Telegram and search for [bold cyan]@BotFather[/bold cyan]\n"
        "  2. Send [bold]/newbot[/bold] and follow the prompts\n"
        "  3. Choose a name and username for your bot\n"
        "  4. BotFather will reply with your bot token\n\n"
        "[dim]Token format: 1234567890:ABCdefGHIjklMNOpqrsTUVwxyz[/dim]"
    )
    console.print(
        Panel(
            instructions,
            title="[bold]Telegram Bot Token[/bold]",
            border_style="blue",
            padding=(1, 2),
        )
    )

    while True:
        token: str | None = questionary.text("Paste your bot token:").ask()
        if token is None:
            _abort()
        token = token.strip()
        if _TOKEN_PATTERN.match(token):
            return str(token)
        console.print(
            "[red]Invalid token format. Expected: digits:alphanumeric (e.g. 123456:ABC-xyz)[/red]"
        )


def _ask_user_id(console: Console) -> list[int]:
    """Prompt for the Telegram user ID with instructions."""
    instructions = (
        "[bold]How to find your Telegram User ID:[/bold]\n\n"
        "  1. Open Telegram and search for [bold cyan]@userinfobot[/bold cyan]\n"
        "  2. Send [bold]/start[/bold] to the bot\n"
        "  3. It will reply with your numeric user ID\n\n"
        "[dim]Only messages from this user ID will be accepted by the bot.[/dim]"
    )
    console.print(
        Panel(
            instructions, title="[bold]Telegram User ID[/bold]", border_style="blue", padding=(1, 2)
        )
    )

    while True:
        raw = questionary.text("Enter your numeric user ID:").ask()
        if raw is None:
            _abort()
        raw = raw.strip()
        try:
            uid = int(raw)
        except ValueError:
            console.print("[red]Please enter a valid number.[/red]")
            continue
        if uid <= 0:
            console.print("[red]User ID must be a positive number.[/red]")
            continue
        return [uid]


def _ask_docker(console: Console) -> bool:
    """Detect Docker and ask whether to enable sandboxing."""
    docker_found = shutil.which("docker") is not None

    if docker_found:
        console.print(
            Panel(
                "[bold green]Docker detected on your system.[/bold green]\n\n"
                "Running ductor inside Docker isolates it from your host.\n"
                "This is the recommended setup for safety.",
                title="[bold]Docker Sandboxing[/bold]",
                border_style="green",
                padding=(1, 2),
            ),
        )
        enabled: bool | None = questionary.confirm(
            "Enable Docker sandboxing? (Recommended)",
            default=True,
        ).ask()
        if enabled is None:
            _abort()
        return bool(enabled)

    console.print(
        Panel(
            "[bold yellow]Docker was not found on your system.[/bold yellow]\n\n"
            "We recommend installing Docker to run the bot in an isolated container.\n"
            "You can enable Docker sandboxing later in the config.\n\n"
            "[dim]https://docs.docker.com/get-docker/[/dim]",
            title="[bold]Docker Sandboxing[/bold]",
            border_style="yellow",
            padding=(1, 2),
        ),
    )
    return False


def _ask_timezone(console: Console) -> str:
    """Prompt for timezone selection."""
    console.print(
        Panel(
            "Your timezone is used for cron scheduling, heartbeat quiet hours,\n"
            "and daily session resets.",
            title="[bold]Timezone[/bold]",
            border_style="blue",
            padding=(1, 2),
        ),
    )

    choices = [*_TIMEZONES, _MANUAL_TZ_OPTION]
    selected: str | None = questionary.select("Select your timezone:", choices=choices).ask()
    if selected is None:
        _abort()

    if selected != _MANUAL_TZ_OPTION:
        return str(selected)

    while True:
        manual: str | None = questionary.text("Enter IANA timezone (e.g. Europe/Berlin):").ask()
        if manual is None:
            _abort()
        manual = manual.strip()
        try:
            ZoneInfo(manual)
        except (ZoneInfoNotFoundError, KeyError):
            console.print(f"[red]Unknown timezone: {manual}[/red]")
            continue
        return str(manual)


def _offer_service_install(console: Console) -> bool:
    """Ask whether to install ductor as a background service."""
    from ductor_bot.infra.service import is_service_available

    if not is_service_available():
        return False

    is_windows = sys.platform == "win32"
    is_macos = sys.platform == "darwin"
    if is_windows:
        mechanism = "scheduled task"
        trigger = "login"
    elif is_macos:
        mechanism = "launch agent"
        trigger = "login"
    else:
        mechanism = "systemd service"
        trigger = "boot"

    console.print(
        Panel(
            f"[bold]Run ductor as a background service?[/bold]\n\n"
            f"This creates a {mechanism} that:\n\n"
            f"  - Starts ductor on {trigger}\n"
            "  - Restarts automatically on crash\n"
            "  - Keeps running in the background\n\n"
            "[dim]Recommended for VPS or always-on setups.[/dim]",
            title="[bold]Background Service[/bold]",
            border_style="blue",
            padding=(1, 2),
        ),
    )

    enabled: bool | None = questionary.confirm(
        "Install as background service? (Recommended for VPS)",
        default=True,
    ).ask()
    if enabled is None:
        _abort()
    console.print()
    return bool(enabled)


def _write_config(
    *,
    telegram_token: str,
    allowed_user_ids: list[int],
    user_timezone: str,
    docker_enabled: bool,
) -> Path:
    """Write the config file with wizard values merged into defaults."""
    paths = resolve_paths()
    config_path = paths.config_path
    config_path.parent.mkdir(parents=True, exist_ok=True)

    if config_path.exists():
        try:
            existing: dict[str, object] = json.loads(config_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            logger.warning("Ignoring invalid config file during onboarding: %s", config_path)
            existing = {}
    else:
        existing = {}

    defaults = AgentConfig().model_dump(mode="json")
    merged, _ = deep_merge_config(existing, defaults)

    merged["telegram_token"] = telegram_token
    merged["allowed_user_ids"] = allowed_user_ids
    merged["user_timezone"] = user_timezone
    docker_section = merged.get("docker")
    if isinstance(docker_section, dict):
        docker_section["enabled"] = docker_enabled
    else:
        merged["docker"] = {"enabled": docker_enabled}

    config_path.write_text(
        json.dumps(merged, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )

    init_workspace(paths)
    return config_path


def run_onboarding() -> bool:
    """Run onboarding and return True only when service install succeeded."""
    console = Console()
    console.print()
    _show_banner(console)

    _check_clis(console)
    console.print()

    _show_disclaimer(console)
    console.print()

    token = _ask_telegram_token(console)
    console.print()

    user_ids = _ask_user_id(console)
    console.print()

    docker_enabled = _ask_docker(console)
    console.print()

    timezone = _ask_timezone(console)
    console.print()

    config_path = _write_config(
        telegram_token=token,
        allowed_user_ids=user_ids,
        user_timezone=timezone,
        docker_enabled=docker_enabled,
    )

    paths = resolve_paths()

    # Offer background service setup on Linux with systemd
    run_as_service = _offer_service_install(console)

    console.print(
        Panel(
            "[bold green]Setup complete![/bold green]\n\n"
            "[bold]Your ductor files:[/bold]\n\n"
            f"  Home:       [cyan]{paths.ductor_home}[/cyan]\n"
            f"  Config:     [cyan]{config_path}[/cyan]\n"
            f"  Workspace:  [cyan]{paths.workspace}[/cyan]\n"
            f"  Logs:       [cyan]{paths.logs_dir}[/cyan]\n\n"
            + ("Installing service..." if run_as_service else "Starting bot..."),
            title="[bold green]Ready[/bold green]",
            border_style="green",
            padding=(1, 2),
        ),
    )
    console.print()

    service_installed = False
    if run_as_service:
        from ductor_bot.infra.service import install_service

        service_installed = install_service(console)

    return service_installed


def run_smart_reset(ductor_home: Path) -> None:
    """Read existing config, handle Docker cleanup, and delete workspace."""
    console = Console()
    console.print()

    config_path = ductor_home / "config" / "config.json"

    # Read Docker config from existing setup
    docker_container: str | None = None
    docker_image: str | None = None
    if config_path.exists():
        try:
            data = json.loads(config_path.read_text(encoding="utf-8"))
            docker = data.get("docker", {})
            if isinstance(docker, dict) and docker.get("enabled"):
                docker_container = str(docker.get("container_name", "ductor-sandbox"))
                docker_image = str(docker.get("image_name", "ductor-sandbox"))
        except (json.JSONDecodeError, OSError):
            pass

    # Warning panel
    console.print(
        Panel(
            "[bold yellow]You already have a configured setup.[/bold yellow]\n\n"
            "Re-running onboarding will perform a [bold red]full reset[/bold red]:\n\n"
            f"  [dim]{ductor_home}[/dim] will be deleted entirely.\n"
            "  All sessions, configs, memory, and cron tasks will be lost.",
            title="[bold yellow]Existing Setup Detected[/bold yellow]",
            border_style="yellow",
            padding=(1, 2),
        ),
    )

    # Docker cleanup offer
    if docker_container and shutil.which("docker"):
        console.print()
        console.print(
            Panel(
                "[bold]Docker sandboxing is enabled in your current config.[/bold]\n\n"
                f"  Container: [cyan]{docker_container}[/cyan]\n"
                f"  Image:     [cyan]{docker_image}[/cyan]",
                title="[bold]Docker Cleanup[/bold]",
                border_style="blue",
                padding=(1, 2),
            ),
        )
        remove_docker: bool | None = questionary.confirm(
            "Remove the Docker container and image?",
            default=True,
        ).ask()
        if remove_docker is None:
            _abort()
        if remove_docker:
            console.print("[dim]Removing Docker resources...[/dim]")
            subprocess.run(
                ["docker", "stop", "-t", "5", docker_container],
                capture_output=True,
                check=False,
            )
            subprocess.run(
                ["docker", "rm", "-f", docker_container],
                capture_output=True,
                check=False,
            )
            if docker_image:
                subprocess.run(
                    ["docker", "rmi", docker_image],
                    capture_output=True,
                    check=False,
                )
            console.print("[green]Docker cleanup done.[/green]")

    # Final confirmation
    console.print()
    confirmed: bool | None = questionary.confirm(
        "Delete everything and start fresh?",
        default=False,
    ).ask()
    if not confirmed:
        _abort()

    shutil.rmtree(ductor_home)
    console.print("[dim]Workspace deleted.[/dim]\n")
