"""Interactive onboarding wizard for first-time setup."""

from __future__ import annotations

import json
import logging
import re
import shutil
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import NoReturn, TypedDict
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import questionary
from rich.console import Console
from rich.panel import Panel
from rich.text import Text

from ductor_bot.cli.auth import (
    AuthResult,
    AuthStatus,
    check_antigravity_auth,
    check_claude_auth,
    check_codex_auth,
    check_gemini_auth,
    check_grok_auth,
)
from ductor_bot.config import DEFAULT_EMPTY_GEMINI_API_KEY, AgentConfig, deep_merge_config
from ductor_bot.i18n import t_rich
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
_MATRIX_USER_RE = re.compile(r"^@[a-z0-9._=/+-]+:[a-z0-9.-]+$", re.IGNORECASE)
_SLACK_BOT_TOKEN_RE = re.compile(r"^xoxb-[A-Za-z0-9-]+$")
_SLACK_APP_TOKEN_RE = re.compile(r"^xapp-[A-Za-z0-9-]+$")
_SLACK_CHANNEL_RE = re.compile(r"^[CG][A-Z0-9]{8,}$")
_SLACK_USER_RE = re.compile(r"^U[A-Z0-9]{8,}$")

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
    Console().print(f"\n{t_rich('wizard.common.cancelled')}\n")
    sys.exit(0)


def _show_banner(console: Console) -> None:
    """Display the ASCII art banner."""
    banner = Text(_load_banner(), style="bold cyan")
    console.print(
        Panel(
            banner,
            subtitle=f"[dim]{t_rich('wizard.common.subtitle')}[/dim]",
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
    """Detect CLI availability; warn but continue when any individual check raises."""
    results: dict[str, AuthResult] = {}
    probes: tuple[tuple[str, Callable[[], AuthResult]], ...] = (
        ("claude", check_claude_auth),
        ("codex", check_codex_auth),
        ("gemini", check_gemini_auth),
        ("antigravity", check_antigravity_auth),
        ("grok", check_grok_auth),
    )
    for name, fn in probes:
        try:
            results[name] = fn()
        except Exception as exc:
            # Wizard must stay alive on probe errors — see P1-BUG-01 (#109).
            logger.warning("Auth check for %s failed: %s", name, exc)
            results[name] = AuthResult(name, AuthStatus.NOT_FOUND)
            console.print(
                t_rich(f"wizard.cli_backends.{name}_check_failed", error=str(exc)),
            )

    lines = [
        t_rich("wizard.cli_backends.header"),
        *(
            t_rich(f"wizard.cli_backends.{name}", status=_STATUS_ICON[results[name].status])
            for name, _fn in probes
        ),
    ]

    has_auth = any(results[name].is_authenticated for name, _fn in probes)

    if has_auth:
        border = "green"
    else:
        border = "red"
        lines.append(t_rich("wizard.cli_backends.no_auth"))

    console.print(
        Panel(
            "\n".join(lines),
            title=t_rich("wizard.cli_backends.title"),
            border_style=border,
            padding=(1, 2),
        ),
    )

    if not has_auth:
        console.print()
        _abort()


def _show_disclaimer(console: Console) -> None:
    """Display the risk disclaimer and require confirmation."""
    console.print(
        Panel(
            t_rich("wizard.disclaimer.body"),
            title=t_rich("wizard.disclaimer.title"),
            border_style="yellow",
            padding=(1, 2),
        )
    )

    accepted = questionary.confirm(
        t_rich("wizard.disclaimer.confirm"),
        default=False,
    ).ask()
    if not accepted:
        _abort()


# ---------------------------------------------------------------------------
# Transport selection
# ---------------------------------------------------------------------------


def _ask_transport(console: Console) -> str:
    """Prompt for the messaging transport."""
    console.print(
        Panel(
            t_rich("wizard.transport.body"),
            title=t_rich("wizard.transport.title"),
            border_style="blue",
            padding=(1, 2),
        ),
    )

    selected: str | None = questionary.select(
        t_rich("wizard.transport.prompt"),
        choices=["Telegram", "Matrix", "Slack"],
    ).ask()
    if selected is None:
        _abort()
    transport_by_label = {
        "Telegram": "telegram",
        "Matrix": "matrix",
        "Slack": "slack",
    }
    return transport_by_label[selected]


# ---------------------------------------------------------------------------
# Telegram setup
# ---------------------------------------------------------------------------


def _ask_telegram_token(console: Console) -> str:
    """Prompt for the Telegram bot token with instructions."""
    console.print(
        Panel(
            t_rich("wizard.telegram.token.body"),
            title=t_rich("wizard.telegram.token.title"),
            border_style="blue",
            padding=(1, 2),
        )
    )

    while True:
        token: str | None = questionary.text(t_rich("wizard.telegram.token.prompt")).ask()
        if token is None:
            _abort()
        token = token.strip()
        if _TOKEN_PATTERN.match(token):
            return str(token)
        console.print(t_rich("wizard.telegram.token.error"))


def _ask_user_id(console: Console) -> list[int]:
    """Prompt for the Telegram user ID with instructions."""
    console.print(
        Panel(
            t_rich("wizard.telegram.user_id.body"),
            title=t_rich("wizard.telegram.user_id.title"),
            border_style="blue",
            padding=(1, 2),
        )
    )

    while True:
        raw = questionary.text(t_rich("wizard.telegram.user_id.prompt")).ask()
        if raw is None:
            _abort()
        raw = raw.strip()
        try:
            uid = int(raw)
        except ValueError:
            console.print(t_rich("wizard.telegram.user_id.error_nan"))
            continue
        if uid <= 0:
            console.print(t_rich("wizard.telegram.user_id.error_negative"))
            continue
        return [uid]


# ---------------------------------------------------------------------------
# Matrix setup
# ---------------------------------------------------------------------------


def _ask_matrix_homeserver(console: Console) -> str:
    """Prompt for the Matrix homeserver URL."""
    console.print(
        Panel(
            t_rich("wizard.matrix.homeserver.body"),
            title=t_rich("wizard.matrix.homeserver.title"),
            border_style="blue",
            padding=(1, 2),
        ),
    )

    while True:
        url: str | None = questionary.text(t_rich("wizard.matrix.homeserver.prompt")).ask()
        if url is None:
            _abort()
        url = url.strip().rstrip("/")
        if url.startswith("https://") and len(url) > len("https://"):
            return url
        console.print(t_rich("wizard.matrix.homeserver.error"))


def _ask_matrix_user_id(console: Console) -> str:
    """Prompt for the Matrix bot user ID."""
    console.print(
        Panel(
            t_rich("wizard.matrix.user_id.body"),
            title=t_rich("wizard.matrix.user_id.title"),
            border_style="blue",
            padding=(1, 2),
        ),
    )

    while True:
        uid: str | None = questionary.text(t_rich("wizard.matrix.user_id.prompt")).ask()
        if uid is None:
            _abort()
        uid = uid.strip()
        if _MATRIX_USER_RE.match(uid):
            return uid
        console.print(t_rich("wizard.matrix.user_id.error"))


def _ask_matrix_password(console: Console) -> str:
    """Prompt for the Matrix account password."""
    console.print(
        Panel(
            t_rich("wizard.matrix.password.body"),
            title=t_rich("wizard.matrix.password.title"),
            border_style="blue",
            padding=(1, 2),
        ),
    )

    while True:
        pw: str | None = questionary.password(t_rich("wizard.matrix.password.prompt")).ask()
        if pw is None:
            _abort()
        pw = pw.strip()
        if pw:
            return pw
        console.print(t_rich("wizard.matrix.password.error"))


def _ask_matrix_allowed_users(console: Console) -> list[str]:
    """Prompt for allowed Matrix user IDs."""
    console.print(
        Panel(
            t_rich("wizard.matrix.allowed_users.body"),
            title=t_rich("wizard.matrix.allowed_users.title"),
            border_style="blue",
            padding=(1, 2),
        ),
    )

    while True:
        raw: str | None = questionary.text(t_rich("wizard.matrix.allowed_users.prompt")).ask()
        if raw is None:
            _abort()
        raw = raw.strip()
        if _MATRIX_USER_RE.match(raw):
            return [raw]
        console.print(t_rich("wizard.matrix.allowed_users.error"))


# ---------------------------------------------------------------------------
# Slack setup
# ---------------------------------------------------------------------------


def _ask_slack_bot_token(console: Console) -> str:
    """Prompt for the Slack bot token."""
    console.print(
        Panel(
            t_rich("wizard.slack.bot_token.body"),
            title=t_rich("wizard.slack.bot_token.title"),
            border_style="blue",
            padding=(1, 2),
        )
    )

    while True:
        token: str | None = questionary.password(t_rich("wizard.slack.bot_token.prompt")).ask()
        if token is None:
            _abort()
        token = token.strip()
        if _SLACK_BOT_TOKEN_RE.match(token):
            return token
        console.print(t_rich("wizard.slack.bot_token.error"))


def _ask_slack_app_token(console: Console) -> str:
    """Prompt for the Slack app token."""
    console.print(
        Panel(
            t_rich("wizard.slack.app_token.body"),
            title=t_rich("wizard.slack.app_token.title"),
            border_style="blue",
            padding=(1, 2),
        )
    )

    while True:
        token: str | None = questionary.password(t_rich("wizard.slack.app_token.prompt")).ask()
        if token is None:
            _abort()
        token = token.strip()
        if _SLACK_APP_TOKEN_RE.match(token):
            return token
        console.print(t_rich("wizard.slack.app_token.error"))


def _parse_slack_ids(raw: str, *, pattern: re.Pattern[str]) -> list[str]:
    """Parse comma/space-separated Slack IDs and validate them."""
    values = [part.strip() for part in re.split(r"[\s,]+", raw) if part.strip()]
    if not values:
        return []
    if any(pattern.match(value) is None for value in values):
        raise ValueError(raw)
    return values


def _ask_slack_allowed_channels(console: Console) -> list[str]:
    """Prompt for allowed Slack channel IDs."""
    console.print(
        Panel(
            t_rich("wizard.slack.allowed_channels.body"),
            title=t_rich("wizard.slack.allowed_channels.title"),
            border_style="blue",
            padding=(1, 2),
        )
    )

    while True:
        raw: str | None = questionary.text(t_rich("wizard.slack.allowed_channels.prompt")).ask()
        if raw is None:
            _abort()
        raw = raw.strip()
        if not raw:
            return []
        try:
            return _parse_slack_ids(raw, pattern=_SLACK_CHANNEL_RE)
        except ValueError:
            console.print(t_rich("wizard.slack.allowed_channels.error"))


def _ask_slack_allowed_users(console: Console) -> list[str]:
    """Prompt for allowed Slack user IDs."""
    console.print(
        Panel(
            t_rich("wizard.slack.allowed_users.body"),
            title=t_rich("wizard.slack.allowed_users.title"),
            border_style="blue",
            padding=(1, 2),
        )
    )

    while True:
        raw: str | None = questionary.text(t_rich("wizard.slack.allowed_users.prompt")).ask()
        if raw is None:
            _abort()
        raw = raw.strip()
        try:
            values = _parse_slack_ids(raw, pattern=_SLACK_USER_RE)
        except ValueError:
            console.print(t_rich("wizard.slack.allowed_users.error"))
            continue
        if values:
            return values
        console.print(t_rich("wizard.slack.allowed_users.error"))


# ---------------------------------------------------------------------------
# Common steps
# ---------------------------------------------------------------------------


def _ask_docker(console: Console) -> bool:
    """Detect Docker and ask whether to enable sandboxing."""
    docker_found = shutil.which("docker") is not None

    if docker_found:
        console.print(
            Panel(
                t_rich("wizard.docker.found_body"),
                title=t_rich("wizard.docker.title"),
                border_style="green",
                padding=(1, 2),
            ),
        )
        enabled: bool | None = questionary.confirm(
            t_rich("wizard.docker.found_prompt"),
            default=True,
        ).ask()
        if enabled is None:
            _abort()
        return bool(enabled)

    console.print(
        Panel(
            t_rich("wizard.docker.not_found_body"),
            title=t_rich("wizard.docker.title"),
            border_style="yellow",
            padding=(1, 2),
        ),
    )
    return False


def _build_extras_table(console: Console) -> None:
    """Print a Rich overview table of all available Docker extras."""
    from rich.table import Table

    from ductor_bot.infra.docker_extras import DOCKER_EXTRAS_BY_ID, extras_for_display

    table = Table(
        show_header=True,
        header_style="bold",
        box=None,
        padding=(0, 2),
        title=t_rich("wizard.docker.extras.title"),
        title_style="bold blue",
    )
    table.add_column(t_rich("wizard.docker.extras.col_package"), style="bold green", min_width=18)
    table.add_column(t_rich("wizard.docker.extras.col_description"), min_width=40)
    table.add_column(t_rich("wizard.docker.extras.col_size"), style="cyan", justify="right")

    for category, extras in extras_for_display():
        table.add_row(f"[bold yellow]{category}[/bold yellow]", "", "")
        for extra in extras:
            dep_hint = ""
            if extra.depends_on:
                dep_names = ", ".join(
                    DOCKER_EXTRAS_BY_ID[d].name
                    for d in extra.depends_on
                    if d in DOCKER_EXTRAS_BY_ID
                )
                if dep_names:
                    dep_hint = f" [dim](+ {dep_names})[/dim]"
            table.add_row(
                f"  {extra.name}",
                f"{extra.description}{dep_hint}",
                extra.size_estimate,
            )

    console.print()
    console.print(table)
    console.print()
    console.print(t_rich("wizard.docker.extras.hint"))
    console.print()


def _ask_docker_extras(console: Console) -> list[str]:
    """Prompt for optional Docker sandbox packages."""
    from ductor_bot.infra.docker_extras import (
        DOCKER_EXTRAS_BY_ID,
        extras_for_display,
        resolve_extras,
    )

    _build_extras_table(console)

    # -- checkbox selection ---------------------------------------------------
    choices: list[questionary.Choice | questionary.Separator] = []
    for category, extras in extras_for_display():
        choices.append(questionary.Separator(f"── {category} ──"))
        choices.extend(
            questionary.Choice(
                title=f"{extra.name}  ({extra.size_estimate})",
                value=extra.id,
            )
            for extra in extras
        )

    selected: list[str] | None = questionary.checkbox(
        t_rich("wizard.docker.extras.prompt"),
        choices=choices,
    ).ask()

    if selected is None:
        _abort()

    if not selected:
        return []

    # -- resolve dependencies -------------------------------------------------
    resolved = resolve_extras(selected)
    resolved_ids = [e.id for e in resolved]

    added_deps = set(resolved_ids) - set(selected)
    if added_deps:
        dep_names = ", ".join(
            DOCKER_EXTRAS_BY_ID[d].name for d in added_deps if d in DOCKER_EXTRAS_BY_ID
        )
        if dep_names:
            console.print(t_rich("wizard.docker.extras.auto_deps", names=dep_names))

    return resolved_ids


def _ask_timezone(console: Console) -> str:
    """Prompt for timezone selection."""
    console.print(
        Panel(
            t_rich("wizard.timezone.body"),
            title=t_rich("wizard.timezone.title"),
            border_style="blue",
            padding=(1, 2),
        ),
    )

    choices = [*_TIMEZONES, _MANUAL_TZ_OPTION]
    selected: str | None = questionary.select(
        t_rich("wizard.timezone.prompt"), choices=choices
    ).ask()
    if selected is None:
        _abort()

    if selected != _MANUAL_TZ_OPTION:
        return str(selected)

    while True:
        manual: str | None = questionary.text(t_rich("wizard.timezone.manual_prompt")).ask()
        if manual is None:
            _abort()
        manual = manual.strip()
        try:
            ZoneInfo(manual)
        except (ZoneInfoNotFoundError, KeyError):
            console.print(t_rich("wizard.timezone.error", tz=manual))
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
            t_rich("wizard.service.body", mechanism=mechanism, trigger=trigger),
            title=t_rich("wizard.service.title"),
            border_style="blue",
            padding=(1, 2),
        ),
    )

    enabled: bool | None = questionary.confirm(
        t_rich("wizard.service.prompt"),
        default=True,
    ).ask()
    if enabled is None:
        _abort()
    console.print()
    return bool(enabled)


# ---------------------------------------------------------------------------
# Config writing
# ---------------------------------------------------------------------------


class _WizardConfig(TypedDict, total=False):
    """Wizard values passed to ``_write_config``."""

    transport: str
    user_timezone: str
    docker_enabled: bool
    docker_extras: list[str] | None
    # Telegram
    telegram_token: str
    allowed_user_ids: list[int] | None
    # Matrix
    matrix_homeserver: str
    matrix_user_id: str
    matrix_password: str
    matrix_allowed_users: list[str] | None
    # Slack
    slack_bot_token: str
    slack_app_token: str
    slack_allowed_channels: list[str] | None
    slack_allowed_users: list[str] | None


def _load_existing_config(config_path: Path) -> dict[str, object]:
    """Load existing config or return empty dict."""
    if config_path.exists():
        try:
            raw = json.loads(config_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            logger.warning(
                "Ignoring invalid config file during onboarding: %s",
                config_path,
            )
        else:
            result: dict[str, object] = raw
            return result
    return {}


def _apply_transport_config(merged: dict[str, object], cfg: _WizardConfig) -> None:
    """Write transport-specific keys into *merged*."""
    transport = cfg.get("transport", "telegram")
    if transport == "telegram":
        merged["telegram_token"] = cfg.get("telegram_token", "")
        merged["allowed_user_ids"] = cfg.get("allowed_user_ids") or []
    elif transport == "matrix":
        matrix_section = merged.get("matrix")
        if not isinstance(matrix_section, dict):
            matrix_section = {}
            merged["matrix"] = matrix_section
        matrix_section["homeserver"] = cfg.get("matrix_homeserver", "")
        matrix_section["user_id"] = cfg.get("matrix_user_id", "")
        matrix_section["password"] = cfg.get("matrix_password", "")
        matrix_section["allowed_users"] = cfg.get("matrix_allowed_users") or []
        matrix_section["store_path"] = "matrix_store"
    else:  # slack
        slack_section = merged.get("slack")
        if not isinstance(slack_section, dict):
            slack_section = {}
            merged["slack"] = slack_section
        slack_section["bot_token"] = cfg.get("slack_bot_token", "")
        slack_section["app_token"] = cfg.get("slack_app_token", "")
        slack_section["allowed_channels"] = cfg.get("slack_allowed_channels") or []
        slack_section["allowed_users"] = cfg.get("slack_allowed_users") or []


def _write_config(cfg: _WizardConfig) -> Path:
    """Write the config file with wizard values merged into defaults."""
    docker_enabled = cfg.get("docker_enabled", False)

    paths = resolve_paths()
    config_path = paths.config_path
    config_path.parent.mkdir(parents=True, exist_ok=True)

    existing = _load_existing_config(config_path)

    defaults = AgentConfig().model_dump(mode="json")
    defaults["gemini_api_key"] = DEFAULT_EMPTY_GEMINI_API_KEY
    merged, _ = deep_merge_config(existing, defaults)
    if merged.get("gemini_api_key") is None:
        merged["gemini_api_key"] = DEFAULT_EMPTY_GEMINI_API_KEY

    merged["transport"] = cfg.get("transport", "telegram")
    merged["transports"] = [merged["transport"]]
    merged["user_timezone"] = cfg.get("user_timezone", "UTC")
    raw_docker = merged.get("docker")
    if isinstance(raw_docker, dict):
        docker_section = raw_docker
    else:
        docker_section = {"enabled": docker_enabled}
        merged["docker"] = docker_section
    docker_section["enabled"] = docker_enabled
    docker_extras = cfg.get("docker_extras")
    if docker_extras is not None:
        docker_section["extras"] = docker_extras

    _apply_transport_config(merged, cfg)

    from ductor_bot.infra.json_store import atomic_json_save

    atomic_json_save(config_path, merged)

    init_workspace(paths)
    return config_path


def _collect_transport_config(console: Console, transport: str) -> _WizardConfig:
    """Collect transport-specific onboarding answers."""
    if transport == "telegram":
        telegram_token = _ask_telegram_token(console)
        console.print()
        allowed_user_ids = _ask_user_id(console)
        console.print()
        return _WizardConfig(
            telegram_token=telegram_token,
            allowed_user_ids=allowed_user_ids,
        )

    if transport == "matrix":
        matrix_homeserver = _ask_matrix_homeserver(console)
        console.print()
        matrix_user_id = _ask_matrix_user_id(console)
        console.print()
        matrix_password = _ask_matrix_password(console)
        console.print()
        matrix_allowed_users = _ask_matrix_allowed_users(console)
        console.print()
        return _WizardConfig(
            matrix_homeserver=matrix_homeserver,
            matrix_user_id=matrix_user_id,
            matrix_password=matrix_password,
            matrix_allowed_users=matrix_allowed_users,
        )

    slack_bot_token = _ask_slack_bot_token(console)
    console.print()
    slack_app_token = _ask_slack_app_token(console)
    console.print()
    slack_allowed_channels = _ask_slack_allowed_channels(console)
    console.print()
    slack_allowed_users = _ask_slack_allowed_users(console)
    console.print()
    return _WizardConfig(
        slack_bot_token=slack_bot_token,
        slack_app_token=slack_app_token,
        slack_allowed_channels=slack_allowed_channels,
        slack_allowed_users=slack_allowed_users,
    )


# ---------------------------------------------------------------------------
# Onboarding flow
# ---------------------------------------------------------------------------


def run_onboarding() -> bool:
    """Run onboarding and return True only when service install succeeded."""
    console = Console()
    console.print()
    _show_banner(console)

    _check_clis(console)
    console.print()

    _show_disclaimer(console)
    console.print()

    transport = _ask_transport(console)
    console.print()
    transport_config = _collect_transport_config(console, transport)

    docker_enabled = _ask_docker(console)
    console.print()

    docker_extras: list[str] = []
    if docker_enabled:
        docker_extras = _ask_docker_extras(console)
        console.print()

    timezone = _ask_timezone(console)
    console.print()

    config_path = _write_config(
        _WizardConfig(
            transport=transport,
            user_timezone=timezone,
            docker_enabled=docker_enabled,
            docker_extras=docker_extras,
            **transport_config,
        )
    )

    paths = resolve_paths()

    # Offer background service setup on Linux with systemd
    run_as_service = _offer_service_install(console)

    action = (
        t_rich("wizard.complete.installing_service")
        if run_as_service
        else t_rich("wizard.complete.starting_bot")
    )
    console.print(
        Panel(
            t_rich("wizard.complete.header")
            + "\n\n"
            + t_rich("wizard.complete.files_header")
            + "\n\n"
            + t_rich("wizard.complete.home", path=paths.ductor_home)
            + "\n"
            + t_rich("wizard.complete.config", path=config_path)
            + "\n"
            + t_rich("wizard.complete.workspace", path=paths.workspace)
            + "\n"
            + t_rich("wizard.complete.logs", path=paths.logs_dir)
            + "\n\n"
            + action,
            title=t_rich("wizard.complete.title"),
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
            t_rich("wizard.reset.body", home=ductor_home),
            title=t_rich("wizard.reset.title"),
            border_style="yellow",
            padding=(1, 2),
        ),
    )

    # Docker cleanup offer
    if docker_container and shutil.which("docker"):
        console.print()
        console.print(
            Panel(
                t_rich(
                    "wizard.reset.docker.body",
                    container=docker_container,
                    image=docker_image,
                ),
                title=t_rich("wizard.reset.docker.title"),
                border_style="blue",
                padding=(1, 2),
            ),
        )
        remove_docker: bool | None = questionary.confirm(
            t_rich("wizard.reset.docker.prompt"),
            default=True,
        ).ask()
        if remove_docker is None:
            _abort()
        if remove_docker:
            console.print(t_rich("wizard.reset.docker.removing"))
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
            console.print(t_rich("wizard.reset.docker.done"))

    # Final confirmation
    console.print()
    confirmed: bool | None = questionary.confirm(
        t_rich("wizard.reset.confirm.prompt"),
        default=False,
    ).ask()
    if not confirmed:
        _abort()

    from ductor_bot.infra.fs import robust_rmtree

    robust_rmtree(ductor_home)
    if ductor_home.exists():
        console.print(t_rich("wizard.reset.confirm.warning", home=ductor_home) + "\n")
    else:
        console.print(t_rich("wizard.reset.confirm.deleted") + "\n")
