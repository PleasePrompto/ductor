"""Entry point: python -m ductor_bot."""

from __future__ import annotations

import asyncio
import json
import logging
import shutil
import signal
import sys
from collections.abc import Callable

from rich.console import Console

# Re-exports from cli_commands — referenced by main() dispatch and by
# tests that patch ductor_bot.__main__.<name>.
from ductor_bot.cli_commands.agents import cmd_agents as _cmd_agents
from ductor_bot.cli_commands.api_cmd import cmd_api as _cmd_api
from ductor_bot.cli_commands.docker import cmd_docker as _cmd_docker
from ductor_bot.cli_commands.install import cmd_install as _cmd_install
from ductor_bot.cli_commands.lifecycle import (
    cmd_restart as _cmd_restart,
)
from ductor_bot.cli_commands.lifecycle import (
    start_bot as _start_bot,
)
from ductor_bot.cli_commands.lifecycle import (
    stop_bot as _stop_bot,
)
from ductor_bot.cli_commands.lifecycle import (
    uninstall as _uninstall,
)
from ductor_bot.cli_commands.lifecycle import (
    upgrade as _upgrade,
)
from ductor_bot.cli_commands.service import cmd_service as _cmd_service
from ductor_bot.cli_commands.status import (
    print_status as _print_status,
)
from ductor_bot.cli_commands.status import (
    print_usage as _print_usage,
)
from ductor_bot.config import (
    DEFAULT_EMPTY_GEMINI_API_KEY,
    AgentConfig,
    deep_merge_config,
)
from ductor_bot.i18n import t_rich
from ductor_bot.infra.json_store import atomic_json_save
from ductor_bot.workspace.init import init_workspace
from ductor_bot.workspace.paths import resolve_paths

logger = logging.getLogger(__name__)

_console = Console()


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

    transports = _normalized_transport_list(data)
    if not transports:
        transports = [data.get("transport", "telegram")]
    for t in transports:
        checker = _IS_CONFIGURED_CHECKS.get(t, _is_configured_telegram)
        if not checker(data):
            return False
    return True


def _is_configured_telegram(data: dict[str, object]) -> bool:
    token = data.get("telegram_token", "")
    users = data.get("allowed_user_ids", [])
    return bool(token) and not str(token).startswith("YOUR_") and bool(users)


def _is_configured_matrix(data: dict[str, object]) -> bool:
    mx = data.get("matrix", {})
    if not isinstance(mx, dict):
        return False
    return bool(mx.get("homeserver")) and bool(mx.get("user_id"))


def _is_configured_slack(data: dict[str, object]) -> bool:
    slack = data.get("slack", {})
    if not isinstance(slack, dict):
        return False
    has_tokens = bool(slack.get("bot_token")) and bool(slack.get("app_token"))
    has_targets = bool(slack.get("allowed_users")) or bool(slack.get("allowed_channels"))
    return has_tokens and has_targets


def _normalized_transport_list(data: dict[str, object]) -> list[str]:
    """Return normalized transport list from raw config data."""
    raw_transports = data.get("transports", [])
    transports = list(raw_transports) if isinstance(raw_transports, list) else []
    primary = data.get("transport", "telegram")
    if not isinstance(primary, str) or not primary:
        primary = "telegram"
    if not transports:
        return [primary]
    if transports[0] != primary:
        return [primary]
    return [t for t in transports if isinstance(t, str)]


_IS_CONFIGURED_CHECKS: dict[str, Callable[[dict[str, object]], bool]] = {
    "telegram": _is_configured_telegram,
    "matrix": _is_configured_matrix,
    "slack": _is_configured_slack,
}


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
            defaults["gemini_api_key"] = DEFAULT_EMPTY_GEMINI_API_KEY
            defaults.pop("api", None)  # Beta: only written by `ductor api enable`
            atomic_json_save(config_path, defaults)
            logger.info("Created default config at %s", config_path)

    try:
        user_data: dict[str, object] = json.loads(config_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        logger.exception("Failed to parse config at %s", config_path)
        sys.exit(1)

    normalized_existing = False
    if user_data.get("gemini_api_key") is None:
        user_data["gemini_api_key"] = DEFAULT_EMPTY_GEMINI_API_KEY
        normalized_existing = True
    normalized_transports = _normalized_transport_list(user_data)
    if user_data.get("transports") != normalized_transports:
        user_data["transports"] = normalized_transports
        normalized_existing = True
    if user_data.get("transport") != normalized_transports[0]:
        user_data["transport"] = normalized_transports[0]
        normalized_existing = True

    defaults = AgentConfig().model_dump(mode="json")
    defaults["gemini_api_key"] = DEFAULT_EMPTY_GEMINI_API_KEY
    defaults.pop("api", None)  # Beta: only written by `ductor api enable`
    merged, changed = deep_merge_config(user_data, defaults)
    changed = changed or normalized_existing

    if changed:
        atomic_json_save(config_path, merged)
        logger.info("Extended config with new default fields")

    init_workspace(paths)
    return AgentConfig.model_validate(merged)


# ---------------------------------------------------------------------------
# Bot lifecycle
# ---------------------------------------------------------------------------


def _validate_transports(config: AgentConfig) -> None:
    """Run transport-specific config validators for all active transports."""
    for t in config.transports:
        validator = _TRANSPORT_VALIDATORS.get(t)
        if validator:
            validator(config)


async def run_bot(config: AgentConfig) -> int:
    """Validate config and run the bot via AgentSupervisor.

    The supervisor manages the main agent and dynamically created sub-agents
    from ``agents.json``.  If no sub-agents are defined, the supervisor runs
    only the main agent — behaviour is identical to the old single-bot path.

    Returns the exit code from the bot (``0`` = clean, ``42`` = restart requested).
    """
    paths = resolve_paths(ductor_home=config.ductor_home)
    _validate_transports(config)

    from ductor_bot.infra.pidlock import acquire_lock, release_lock
    from ductor_bot.multiagent.supervisor import AgentSupervisor

    acquire_lock(pid_file=paths.ductor_home / "bot.pid", kill_existing=True)

    supervisor = AgentSupervisor(config)
    exit_code = 0
    loop = asyncio.get_running_loop()
    current_task = asyncio.current_task()
    installed_signals: list[signal.Signals] = []

    def _request_shutdown() -> None:
        if current_task is not None and not current_task.done():
            current_task.cancel()

    if current_task is not None and sys.platform != "win32":
        for sig in (signal.SIGTERM, signal.SIGINT):
            try:
                loop.add_signal_handler(sig, _request_shutdown)
            except (NotImplementedError, RuntimeError, ValueError):
                continue
            installed_signals.append(sig)

    try:
        exit_code = await supervisor.start()
    except asyncio.CancelledError:
        logger.info("Termination signal received, shutting down gracefully...")
    except KeyboardInterrupt:
        logger.info("Shutting down...")
    finally:
        for sig in installed_signals:
            loop.remove_signal_handler(sig)
        await supervisor.stop_all()
        release_lock(pid_file=paths.ductor_home / "bot.pid")
    return exit_code


# Backward-compat alias for external scripts that call run_telegram().
run_telegram = run_bot


def _validate_telegram_config(config: AgentConfig) -> None:
    """Validate Telegram transport requirements."""
    missing_token = not config.telegram_token or config.telegram_token.startswith("YOUR_")
    needs_users = not config.allowed_user_ids
    if missing_token or needs_users:
        _console.print(t_rich("config.incomplete"))
        sys.exit(1)


def _validate_matrix_config(config: AgentConfig) -> None:
    """Validate Matrix transport requirements."""
    m = config.matrix
    hint = t_rich("config.onboarding_hint")
    if not m.homeserver:
        _console.print(t_rich("config.matrix_no_homeserver", hint=hint))
        sys.exit(1)
    if not m.user_id:
        _console.print(t_rich("config.matrix_no_user", hint=hint))
        sys.exit(1)
    if not m.password and not m.access_token:
        _console.print(t_rich("config.matrix_no_auth", hint=hint))
        sys.exit(1)
    if not m.allowed_rooms and not m.allowed_users:
        _console.print(t_rich("config.matrix_no_target", hint=hint))
        sys.exit(1)


def _validate_slack_config(config: AgentConfig) -> None:
    """Validate Slack transport requirements."""
    slack = config.slack
    has_tokens = bool(slack.bot_token) and bool(slack.app_token)
    has_targets = bool(slack.allowed_users) or bool(slack.allowed_channels)
    if not has_tokens or not has_targets:
        _console.print(t_rich("config.incomplete"))
        sys.exit(1)


_TRANSPORT_VALIDATORS: dict[str, Callable[[AgentConfig], None]] = {
    "telegram": _validate_telegram_config,
    "matrix": _validate_matrix_config,
    "slack": _validate_slack_config,
}


# ---------------------------------------------------------------------------
# CLI command handlers
# ---------------------------------------------------------------------------


def _cmd_status() -> None:
    """Show bot status or hint to configure."""
    from rich.panel import Panel

    _console.print()
    if _is_configured():
        _print_status()
    else:
        _console.print(
            Panel(
                t_rich("status.not_configured"),
                title="[bold]Status[/bold]",
                border_style="yellow",
                padding=(1, 2),
            ),
        )
    _console.print()


def _cmd_setup(verbose: bool) -> None:
    """Run onboarding (with smart reset if already configured), then start."""
    from ductor_bot.cli.init_wizard import run_onboarding, run_smart_reset

    _stop_bot()
    paths = resolve_paths()
    if _is_configured():
        run_smart_reset(paths.ductor_home)
    service_installed = run_onboarding()
    if service_installed:
        return
    _start_bot(verbose)


def _default_action(verbose: bool) -> None:
    """Auto-onboarding if unconfigured, then start bot."""
    if not _is_configured():
        from ductor_bot.cli.init_wizard import run_onboarding

        service_installed = run_onboarding()
        if service_installed:
            return
    _start_bot(verbose)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

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
    "docker": "docker",
    "api": "api",
    "agents": "agents",
    "install": "install",
}

_Action = Callable[[], None]


def main() -> None:
    """CLI entry point."""
    args = sys.argv[1:]
    commands = [a for a in args if not a.startswith("-")]
    verbose = "--verbose" in args or "-v" in args

    if "--version" in args or "-V" in args:
        from ductor_bot import __version__

        _console.print(f"ductor {__version__}")
        return

    if "--help" in args or "-h" in args:
        commands.append("help")

    # Resolve first matching command
    action = next((_COMMANDS[c] for c in commands if c in _COMMANDS), None)

    dispatch: dict[str, _Action] = {
        "help": _print_usage,
        "status": _cmd_status,
        "stop": _stop_bot,
        "restart": _cmd_restart,
        "upgrade": _upgrade,
        "uninstall": _uninstall,
        "setup": lambda: _cmd_setup(verbose),
        "service": lambda: _cmd_service(args),
        "docker": lambda: _cmd_docker(args),
        "api": lambda: _cmd_api(args),
        "agents": lambda: _cmd_agents(args),
        "install": lambda: _cmd_install(args),
    }

    handler = dispatch.get(action) if action else None
    if handler is not None:
        handler()
    else:
        _default_action(verbose)


if __name__ == "__main__":
    main()
