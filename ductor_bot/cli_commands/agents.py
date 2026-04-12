"""Agent management CLI subcommands (``ductor agents ...``)."""

from __future__ import annotations

import json
import re
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from ductor_bot.i18n import t_rich
from ductor_bot.workspace.paths import DuctorPaths, resolve_paths

_console = Console()

_AGENTS_SUBCOMMANDS = frozenset({"list", "add", "remove"})

_CLAUDE_MODELS = ["opus", "sonnet", "haiku"]

_MATRIX_USER_RE = re.compile(r"^@[a-z0-9._=/+-]+:[a-z0-9.-]+$", re.IGNORECASE)


def _parse_agents_subcommand(args: list[str]) -> tuple[str | None, list[str]]:
    """Extract the subcommand and remaining args after 'agents'."""
    found = False
    sub: str | None = None
    rest: list[str] = []
    for a in args:
        if not found and a == "agents":
            found = True
            continue
        if found and sub is None and not a.startswith("-"):
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


def _load_model_choices(provider: str) -> list[str]:
    """Load available model names for a provider from cached config files."""
    paths = resolve_paths()
    config_dir = paths.ductor_home / "config"

    if provider == "claude":
        return list(_CLAUDE_MODELS)

    if provider in ("codex", "openai"):
        cache = config_dir / "codex_models.json"
        if cache.is_file():
            try:
                data = json.loads(cache.read_text(encoding="utf-8"))
                models = data.get("models", [])
                return [m["id"] for m in models if isinstance(m, dict) and "id" in m]
            except (json.JSONDecodeError, OSError):
                pass
        return ["gpt-5.4"]

    if provider == "gemini":
        cache = config_dir / "gemini_models.json"
        if cache.is_file():
            try:
                data = json.loads(cache.read_text(encoding="utf-8"))
                return data.get("models", [])
            except (json.JSONDecodeError, OSError):
                pass
        return ["gemini-2.5-pro"]

    return []


def _default_model(provider: str) -> str:
    """Return the default model for a provider."""
    if provider == "claude":
        return "sonnet"
    if provider in ("codex", "openai"):
        paths = resolve_paths()
        cache = paths.ductor_home / "config" / "codex_models.json"
        if cache.is_file():
            try:
                data = json.loads(cache.read_text(encoding="utf-8"))
                for m in data.get("models", []):
                    if isinstance(m, dict) and m.get("is_default"):
                        return m["id"]
            except (json.JSONDecodeError, OSError):
                pass
        return "gpt-5.4"
    if provider == "gemini":
        return "gemini-2.5-pro"
    return ""


def print_agents_help() -> None:
    """Print the agents subcommand help table."""
    _console.print()
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(style="bold green", min_width=36)
    table.add_column()
    table.add_row("ductor agents", "List all sub-agents and their config")
    table.add_row("ductor agents list", "List all sub-agents and their config")
    table.add_row("ductor agents add <name>", "Add a new sub-agent (interactive)")
    table.add_row("ductor agents add <name> --transport ...", "Add a new sub-agent (CLI flags)")
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
    table.add_column(t_rich("agents.col_transport"))
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
        transport = str(agent.get("transport", "telegram"))
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

        table.add_row(name, transport, status_display + crash_info, uptime_display, prov, mdl)

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


def _validate_matrix_user_id(user_id: str) -> bool:
    """Validate Matrix user ID format (@localpart:domain)."""
    return bool(_MATRIX_USER_RE.match(user_id))


def _prompt_telegram(name: str) -> dict[str, object] | None:
    """Collect Telegram-specific fields. Returns partial agent dict or None on cancel."""
    import questionary

    token: str | None = questionary.text(
        t_rich("agents.add.prompt_token", name=name),
    ).ask()
    if not token or not token.strip():
        return None

    users_raw: str | None = questionary.text(
        t_rich("agents.add.prompt_users"),
    ).ask()
    if users_raw is None:
        return None

    user_ids = _parse_int_list(users_raw)

    groups_raw: str | None = questionary.text(
        t_rich("agents.add.prompt_groups"),
        default="",
    ).ask()
    if groups_raw is None:
        return None

    group_ids = _parse_int_list(groups_raw, allow_negative=True)

    return {
        "telegram_token": token.strip(),
        "allowed_user_ids": user_ids,
        "allowed_group_ids": group_ids,
    }


def _prompt_matrix(name: str) -> dict[str, object] | None:
    """Collect Matrix-specific fields. Returns partial agent dict or None on cancel."""
    import questionary

    homeserver: str | None = questionary.text(
        t_rich("agents.add.prompt_homeserver"),
    ).ask()
    if not homeserver or not homeserver.strip():
        return None
    homeserver = homeserver.strip()
    if not homeserver.startswith("https://"):
        _console.print(t_rich("agents.add.invalid_homeserver"))
        return None

    user_id: str | None = questionary.text(
        t_rich("agents.add.prompt_user_id"),
    ).ask()
    if not user_id or not user_id.strip():
        return None
    user_id = user_id.strip()
    if not _validate_matrix_user_id(user_id):
        _console.print(t_rich("agents.add.invalid_user_id"))
        return None

    password: str | None = questionary.password(
        t_rich("agents.add.prompt_password"),
    ).ask()
    if password is None:
        return None

    allowed_users_raw: str | None = questionary.text(
        t_rich("agents.add.prompt_allowed_users"),
        default="",
    ).ask()
    if allowed_users_raw is None:
        return None

    allowed_users: list[str] = []
    for uid in allowed_users_raw.split(","):
        uid = uid.strip()
        if not uid:
            continue
        if not _validate_matrix_user_id(uid):
            _console.print(t_rich("agents.add.invalid_allowed_user", uid=uid))
            return None
        allowed_users.append(uid)

    allowed_rooms_raw: str | None = questionary.text(
        t_rich("agents.add.prompt_allowed_rooms"),
        default="",
    ).ask()
    if allowed_rooms_raw is None:
        return None

    allowed_rooms: list[str] = []
    for rid in allowed_rooms_raw.split(","):
        rid = rid.strip()
        if rid:
            allowed_rooms.append(rid)

    matrix_cfg: dict[str, object] = {
        "homeserver": homeserver,
        "user_id": user_id,
        "allowed_rooms": allowed_rooms,
        "allowed_users": allowed_users,
        "store_path": "matrix_store",
    }
    if password and password.strip():
        matrix_cfg["password"] = password.strip()

    return {
        "transport": "matrix",
        "matrix": matrix_cfg,
    }


def _prompt_provider_model() -> tuple[str, str] | None:
    """Interactively prompt for provider and a provider-aware model. Returns (provider, model) or None."""
    import questionary

    provider: str | None = questionary.select(
        t_rich("agents.add.prompt_provider"),
        choices=["claude", "codex", "gemini"],
        default="claude",
    ).ask()
    if provider is None:
        return None

    model_choices = _load_model_choices(provider)
    default_model = _default_model(provider)

    if model_choices:
        model: str | None = questionary.select(
            t_rich("agents.add.prompt_model"),
            choices=model_choices,
            default=default_model if default_model in model_choices else model_choices[0],
        ).ask()
    else:
        model = questionary.text(
            t_rich("agents.add.prompt_model"),
            default=default_model,
        ).ask()

    if model is None:
        return None

    return provider, model.strip()


def _parse_add_flags(rest: list[str]) -> tuple[str | None, dict[str, str | None]]:
    """Parse ``ductor agents add <name> [--flag value ...]``.

    Returns (name, flags_dict). Flags use long-form ``--key value`` pairs.
    """
    name: str | None = None
    flags: dict[str, str | None] = {}
    i = 0
    while i < len(rest):
        arg = rest[i]
        if arg.startswith("--"):
            key = arg[2:]
            # Peek for value
            if i + 1 < len(rest) and not rest[i + 1].startswith("--"):
                flags[key] = rest[i + 1]
                i += 2
            else:
                flags[key] = None
                i += 1
        else:
            if name is None:
                name = arg
            i += 1
    return name, flags


def agents_add(rest: list[str]) -> None:
    """Add a new sub-agent, interactively or via CLI flags.

    Interactive:
        ductor agents add <name>

    CLI (Telegram):
        ductor agents add <name> --transport telegram --token TOKEN
            --users ID1,ID2 [--groups GID1,GID2] [--provider P] [--model M]

    CLI (Matrix):
        ductor agents add <name> --transport matrix --homeserver URL
            --user-id @bot:srv [--password PASS] [--allowed-users @u:srv,...]
            [--allowed-rooms !id:srv,...] [--provider P] [--model M]
    """
    import questionary

    parsed_name, flags = _parse_add_flags(rest)
    cli_mode = bool(flags)

    paths = resolve_paths()
    agents = load_agents_registry(paths)
    name = validate_agent_name(parsed_name, agents)
    if name is None:
        return

    # --- CLI mode: build agent from flags directly ---
    if cli_mode:
        transport = flags.get("transport")
        if not transport:
            # Auto-detect from flags
            if "homeserver" in flags or "user-id" in flags:
                transport = "matrix"
            elif "token" in flags:
                transport = "telegram"
            else:
                _console.print("[bold red]Specify --transport or provide --token / --homeserver.[/bold red]")
                return

        provider = flags.get("provider", "claude")
        model = flags.get("model")
        if not model:
            model = _default_model(provider or "claude")

        if transport == "telegram":
            token = flags.get("token")
            if not token:
                _console.print("[bold red]--token is required for Telegram agents.[/bold red]")
                return
            users_raw = flags.get("users", "")
            user_ids = _parse_int_list(users_raw or "")
            groups_raw = flags.get("groups", "")
            group_ids = _parse_int_list(groups_raw or "", allow_negative=True)
            new_agent: dict[str, object] = {
                "name": name,
                "telegram_token": token,
                "allowed_user_ids": user_ids,
                "allowed_group_ids": group_ids,
                "provider": provider,
                "model": model,
            }
        elif transport == "matrix":
            homeserver = flags.get("homeserver")
            user_id = flags.get("user-id")
            if not homeserver:
                _console.print("[bold red]--homeserver is required for Matrix agents.[/bold red]")
                return
            if not homeserver.startswith("https://"):
                _console.print(t_rich("agents.add.invalid_homeserver"))
                return
            if not user_id:
                _console.print("[bold red]--user-id is required for Matrix agents.[/bold red]")
                return
            if not _validate_matrix_user_id(user_id):
                _console.print(t_rich("agents.add.invalid_user_id"))
                return

            allowed_users: list[str] = []
            for uid in (flags.get("allowed-users", "") or "").split(","):
                uid = uid.strip()
                if uid:
                    allowed_users.append(uid)

            allowed_rooms: list[str] = []
            for rid in (flags.get("allowed-rooms", "") or "").split(","):
                rid = rid.strip()
                if rid:
                    allowed_rooms.append(rid)

            matrix_cfg: dict[str, object] = {
                "homeserver": homeserver,
                "user_id": user_id,
                "allowed_rooms": allowed_rooms,
                "allowed_users": allowed_users,
                "store_path": "matrix_store",
            }
            password = flags.get("password")
            if password:
                matrix_cfg["password"] = password

            new_agent = {
                "name": name,
                "transport": "matrix",
                "matrix": matrix_cfg,
                "provider": provider,
                "model": model,
            }
        else:
            _console.print(f"[bold red]Unknown transport '{transport}'. Use 'telegram' or 'matrix'.[/bold red]")
            return

        agents.append(new_agent)
        from ductor_bot.infra.json_store import atomic_json_save
        agents_path = paths.ductor_home / "agents.json"
        atomic_json_save(agents_path, agents)
        _console.print(t_rich("agents.add.done", name=name))
        _console.print(t_rich("agents.add.auto_start"))
        return

    # --- Interactive mode ---
    transport_choice: str | None = questionary.select(
        t_rich("agents.add.prompt_transport"),
        choices=["telegram", "matrix"],
        default="telegram",
    ).ask()
    if transport_choice is None:
        _console.print(t_rich("agents.add.cancelled"))
        return

    if transport_choice == "telegram":
        transport_fields = _prompt_telegram(name)
    else:
        transport_fields = _prompt_matrix(name)

    if transport_fields is None:
        _console.print(t_rich("agents.add.cancelled"))
        return

    pm = _prompt_provider_model()
    if pm is None:
        _console.print(t_rich("agents.add.cancelled"))
        return
    provider_val, model_val = pm

    new_agent_interactive: dict[str, object] = {"name": name}
    new_agent_interactive.update(transport_fields)
    new_agent_interactive["provider"] = provider_val
    new_agent_interactive["model"] = model_val
    agents.append(new_agent_interactive)

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
