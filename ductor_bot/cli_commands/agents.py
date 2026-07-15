"""Agent management CLI subcommands (``ductor agents ...``)."""

from __future__ import annotations

import json

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from ductor_bot.i18n import t_rich
from ductor_bot.workspace.paths import DuctorPaths, resolve_paths

_console = Console()

_AGENTS_SUBCOMMANDS = frozenset({"list", "add", "remove"})


def _parse_agents_subcommand(args: list[str]) -> tuple[str | None, list[str]]:
    """Extract the subcommand and remaining args after 'agents'."""
    found = False
    sub: str | None = None
    rest: list[str] = []
    for a in args:
        if a.startswith("-"):
            continue
        if not found and a == "agents":
            found = True
            continue
        if found and sub is None:
            sub = a if a in _AGENTS_SUBCOMMANDS else None
            if sub is None:
                # Unknown subcommand — show help
                return None, []
            continue
        if found and sub is not None:
            rest.append(a)
    if found and sub is None:
        # bare "ductor agents" → default to list
        return "list", []
    return sub, rest


def print_agents_help() -> None:
    """Print the agents subcommand help table."""
    _console.print()
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(style="bold green", min_width=36)
    table.add_column()
    table.add_row("ductor agents", "List all sub-agents and their config")
    table.add_row("ductor agents list", "List all sub-agents and their config")
    table.add_row("ductor agents add <name>", "Add a new sub-agent (interactive)")
    table.add_row("ductor agents remove <name>", "Remove a sub-agent")
    _console.print(
        Panel(table, title="[bold]Agent Commands[/bold]", border_style="blue", padding=(1, 0)),
    )
    _console.print()


def load_agents_registry(paths: DuctorPaths) -> list[dict[str, object]]:
    """Load sub-agent definitions from agents.json (raw dicts)."""
    agents_path = paths.ductor_home / "agents.json"
    if not agents_path.is_file():
        return []
    try:
        raw = json.loads(agents_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return []
    return raw if isinstance(raw, list) else []


def fetch_live_health() -> dict[str, dict[str, object]]:
    """Query the internal API for live agent health. Returns empty dict on failure."""
    import urllib.request

    from ductor_bot.multiagent.internal_api import _DEFAULT_PORT

    paths = resolve_paths()
    port = _DEFAULT_PORT
    config_path = paths.config_path
    if config_path.exists():
        try:
            cfg = json.loads(config_path.read_text(encoding="utf-8"))
            port = int(cfg.get("interagent_port", _DEFAULT_PORT))
        except (json.JSONDecodeError, OSError, ValueError):
            pass

    try:
        req = urllib.request.Request(f"http://127.0.0.1:{port}/interagent/health")
        opener = urllib.request.build_opener()
        with opener.open(req, timeout=2) as resp:
            data = json.loads(resp.read())
    except Exception:
        return {}
    else:
        result: dict[str, dict[str, object]] = data.get("agents", {})
        return result


def print_agents_status(agents: list[dict[str, object]], *, bot_running: bool = False) -> None:
    """Print a status table for all sub-agents with optional live health."""
    live_health = fetch_live_health() if bot_running else {}

    table = Table(show_header=True, box=None, padding=(0, 2))
    table.add_column(t_rich("agents.col_agent"), style="bold")
    table.add_column(t_rich("agents.col_status"))
    table.add_column(t_rich("agents.col_uptime"))
    table.add_column(t_rich("agents.col_provider"))
    table.add_column(t_rich("agents.col_model"))

    status_style = {
        "running": t_rich("agents.status_running"),
        "starting": t_rich("agents.status_starting"),
        "crashed": t_rich("agents.status_crashed"),
        "stopped": t_rich("agents.status_stopped"),
    }

    for agent in agents:
        name = str(agent.get("name", "?"))
        prov = str(agent.get("provider", "inherited"))
        mdl = str(agent.get("model", "inherited"))

        health = live_health.get(name, {})
        status = str(health.get("status", "unknown")) if health else "—"
        uptime = str(health.get("uptime", "")) if health else ""
        status_display = status_style.get(status, f"[dim]{status}[/dim]")

        crash_info = ""
        if status == "crashed" and health.get("last_crash_error"):
            error = str(health["last_crash_error"])[:80]
            crash_info = f"\n  [dim red]{error}[/dim red]"

        restart_count = health.get("restart_count", 0) if health else 0
        uptime_display = uptime
        if restart_count:
            uptime_display += f" [dim](restarts: {restart_count})[/dim]"

        table.add_row(name, status_display + crash_info, uptime_display, prov, mdl)

    _console.print(
        Panel(
            table,
            title=f"[bold]Sub-Agents ({len(agents)})[/bold]",
            border_style="blue",
            padding=(1, 0),
        ),
    )


def _parse_int_list(raw: str, *, allow_negative: bool = False) -> list[int]:
    """Parse a comma-separated string of integers."""
    result: list[int] = []
    for part in raw.split(","):
        stripped = part.strip()
        digits = stripped.lstrip("-") if allow_negative else stripped
        if digits and digits.isdigit():
            result.append(int(stripped))
    return result


def validate_agent_name(name: str | None, agents: list[dict[str, object]]) -> str | None:
    """Validate an agent name for ``ductor agents add``. Returns clean name or None on error."""
    if not name:
        _console.print(t_rich("agents.add.usage"))
        return None
    name = name.lower().strip()
    if name == "main":
        _console.print(t_rich("agents.add.reserved"))
        return None
    if any(str(a.get("name", "")).lower() == name for a in agents):
        _console.print(t_rich("agents.add.exists", name=name))
        return None
    return name


def agents_list() -> None:
    """List all sub-agents from agents.json."""
    paths = resolve_paths()
    agents = load_agents_registry(paths)
    if not agents:
        _console.print(t_rich("agents.empty"))
        _console.print(t_rich("agents.add_hint"))
        return
    # Check if bot is running for live health
    pid_file = paths.ductor_home / "bot.pid"
    bot_running = False
    if pid_file.exists():
        try:
            pid = int(pid_file.read_text(encoding="utf-8").strip())
            from ductor_bot.infra.pidlock import _is_process_alive

            bot_running = _is_process_alive(pid)
        except (ValueError, OSError):
            pass
    print_agents_status(agents, bot_running=bot_running)


def agents_add(rest: list[str]) -> None:
    """Add a new sub-agent interactively."""
    import questionary

    paths = resolve_paths()
    agents = load_agents_registry(paths)
    name = validate_agent_name(rest[0] if rest else None, agents)
    if name is None:
        return

    token: str | None = questionary.text(
        t_rich("agents.add.prompt_token", name=name),
    ).ask()
    if not token or not token.strip():
        _console.print(t_rich("agents.add.cancelled"))
        return

    users_raw: str | None = questionary.text(
        t_rich("agents.add.prompt_users"),
    ).ask()
    if users_raw is None:
        _console.print(t_rich("agents.add.cancelled"))
        return

    user_ids = _parse_int_list(users_raw)

    groups_raw: str | None = questionary.text(
        t_rich("agents.add.prompt_groups"),
        default="",
    ).ask()
    if groups_raw is None:
        _console.print(t_rich("agents.add.cancelled"))
        return

    group_ids = _parse_int_list(groups_raw, allow_negative=True)

    provider: str | None = questionary.select(
        t_rich("agents.add.prompt_provider"),
        choices=["claude", "codex", "gemini", "antigravity", "grok"],
        default="claude",
    ).ask()
    if provider is None:
        _console.print(t_rich("agents.add.cancelled"))
        return

    model: str | None = questionary.text(
        t_rich("agents.add.prompt_model"),
        default="sonnet",
    ).ask()
    if model is None:
        _console.print(t_rich("agents.add.cancelled"))
        return

    new_agent: dict[str, object] = {
        "name": name,
        "telegram_token": token.strip(),
        "allowed_user_ids": user_ids,
        "allowed_group_ids": group_ids,
        "provider": provider,
        "model": model.strip(),
    }
    agents.append(new_agent)

    from ductor_bot.infra.json_store import atomic_json_save

    agents_path = paths.ductor_home / "agents.json"
    atomic_json_save(agents_path, agents)

    _console.print(t_rich("agents.add.done", name=name))
    _console.print(t_rich("agents.add.auto_start"))


def agents_remove(rest: list[str]) -> None:
    """Remove a sub-agent from agents.json."""
    import questionary

    name = rest[0] if rest else None
    if not name:
        _console.print(t_rich("agents.remove.usage"))
        return

    name = name.lower().strip()
    paths = resolve_paths()
    agents = load_agents_registry(paths)
    match = [a for a in agents if str(a.get("name", "")).lower() == name]
    if not match:
        _console.print(t_rich("agents.remove.not_found", name=name))
        return

    confirmed: bool | None = questionary.confirm(
        t_rich("agents.remove.confirm", name=name),
        default=False,
    ).ask()
    if not confirmed:
        _console.print(t_rich("agents.add.cancelled"))
        return

    from ductor_bot.infra.json_store import atomic_json_save

    remaining = [a for a in agents if str(a.get("name", "")).lower() != name]
    agents_path = paths.ductor_home / "agents.json"
    atomic_json_save(agents_path, remaining)
    _console.print(t_rich("agents.remove.done", name=name))
    _console.print(t_rich("agents.remove.data_hint", path=paths.ductor_home / "agents" / name))


def cmd_agents(args: list[str]) -> None:
    """Handle 'ductor agents [subcommand]'."""
    sub, rest = _parse_agents_subcommand(args)
    if sub is None:
        print_agents_help()
        return

    _console.print()
    if sub == "list":
        agents_list()
    elif sub == "add":
        agents_add(rest)
    elif sub == "remove":
        agents_remove(rest)
    _console.print()
