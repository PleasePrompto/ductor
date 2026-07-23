"""Docker management CLI subcommands (``ductor docker ...``)."""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from collections.abc import Callable
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from ductor_bot.i18n import t_rich
from ductor_bot.workspace.paths import resolve_paths

_console = Console()

_DOCKER_SUBCOMMANDS = frozenset(
    {
        "rebuild",
        "enable",
        "disable",
        "mount",
        "unmount",
        "mounts",
        "extras",
        "extras-add",
        "extras-remove",
    }
)


def _parse_docker_subcommand(args: list[str]) -> str | None:
    """Extract the subcommand after 'docker' from CLI args."""
    found = False
    for a in args:
        if a.startswith("-"):
            continue
        if not found and a == "docker":
            found = True
            continue
        if found:
            return a if a in _DOCKER_SUBCOMMANDS else None
    return None


def _parse_docker_mount_arg(args: list[str]) -> str | None:
    """Extract the path argument after 'docker mount/unmount' from CLI args.

    Expects the form: ``ductor docker mount <path>`` where *args* is
    ``sys.argv[1:]`` (no ``ductor`` prefix).  Non-flag positionals are
    ``docker`` (1), ``mount``/``unmount`` (2), ``<path>`` (3).
    """
    positionals = [a for a in args if not a.startswith("-")]
    # positionals: ['docker', 'mount', '<path>']
    return positionals[2] if len(positionals) >= 3 else None


def print_docker_help() -> None:
    """Print the docker subcommand help table."""
    _console.print()
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(style="bold green", min_width=36)
    table.add_column()
    table.add_row("ductor docker rebuild", "Remove container & image, rebuild on next start")
    table.add_row("ductor docker enable", "Enable Docker sandboxing")
    table.add_row("ductor docker disable", "Disable Docker sandboxing")
    table.add_row("ductor docker mount <path>", "Mount a host directory into the sandbox")
    table.add_row("ductor docker unmount <path>", "Remove a mounted directory")
    table.add_row("ductor docker mounts", "List all mounted directories")
    table.add_row("ductor docker extras", "List available and installed extras")
    table.add_row("ductor docker extras-add <id>", "Add an extra package")
    table.add_row("ductor docker extras-remove <id>", "Remove an extra package")
    _console.print(
        Panel(table, title="[bold]Docker Commands[/bold]", border_style="blue", padding=(1, 0)),
    )
    _console.print()


def docker_read_config() -> tuple[Path, dict[str, object]] | None:
    """Read config.json and return (path, data) or None."""
    paths = resolve_paths()
    config_path = paths.config_path
    if not config_path.exists():
        _console.print(t_rich("docker.config_not_found"))
        return None
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        _console.print(t_rich("docker.config_read_error"))
        return None
    return config_path, data


def _stop_docker_container(container_name: str) -> None:
    """Stop and remove a Docker container."""
    if not shutil.which("docker"):
        return
    _console.print(t_rich("lifecycle.stopping_docker", name=container_name))
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
    _console.print(t_rich("lifecycle.docker_stopped"))


def docker_set_enabled(*, enabled: bool) -> None:
    """Set docker.enabled in config.json and handle running state."""
    result = docker_read_config()
    if result is None:
        return
    config_path, data = result

    docker = data.setdefault("docker", {})
    if not isinstance(docker, dict):
        data["docker"] = docker = {}
    from ductor_bot.infra.json_store import atomic_json_save

    docker["enabled"] = enabled
    atomic_json_save(config_path, data)

    if not enabled:
        container = str(docker.get("container_name", "ductor-sandbox"))
        _stop_docker_container(container)

    if enabled:
        _console.print(t_rich("docker.enabled"))
    else:
        _console.print(t_rich("docker.disabled"))
    _console.print(t_rich("docker.restart_hint"))


def docker_rebuild() -> None:
    """Stop bot, remove container and image, so they get rebuilt on restart."""
    from ductor_bot.cli_commands.lifecycle import stop_bot

    if not shutil.which("docker"):
        _console.print("[bold red]Docker not found.[/bold red]")
        return

    result = docker_read_config()
    container = "ductor-sandbox"
    image = "ductor-sandbox"
    if result is not None:
        _, data = result
        docker = data.get("docker", {})
        if isinstance(docker, dict):
            container = str(docker.get("container_name", container))
            image = str(docker.get("image_name", image))

    _console.print(t_rich("docker.rebuild.stopping"))
    stop_bot()

    _console.print(t_rich("docker.rebuild.removing_container", name=container))
    subprocess.run(["docker", "rm", "-f", container], capture_output=True, check=False)

    _console.print(t_rich("docker.rebuild.removing_image", name=image))
    subprocess.run(["docker", "rmi", image], capture_output=True, check=False)

    _console.print(t_rich("docker.rebuild.done"))


def _expand_path(raw: str) -> Path:
    """Expand env vars and ``~`` in a path string."""
    return Path(os.path.expandvars(raw)).expanduser()


def _docker_get_mounts(data: dict[str, object]) -> list[object]:
    """Return the ``docker.mounts`` list from config, ensuring it exists."""
    docker = data.setdefault("docker", {})
    if not isinstance(docker, dict):
        data["docker"] = docker = {}
    raw = docker.get("mounts")
    if not isinstance(raw, list):
        raw = []
        docker["mounts"] = raw
    return raw


def _is_duplicate_mount(mounts: list[object], resolved_str: str) -> bool:
    """Return True if *resolved_str* already exists in the mount list."""
    for existing in mounts:
        if not isinstance(existing, str):
            continue
        try:
            if str(_expand_path(existing).resolve()) == resolved_str:
                return True
        except OSError:
            continue
    return False


def docker_mount(args: list[str]) -> None:
    """Add a host directory to the Docker sandbox mounts."""
    raw_path = _parse_docker_mount_arg(args)
    if not raw_path:
        _console.print(t_rich("docker.mount.usage"))
        return

    expanded = _expand_path(raw_path)
    try:
        resolved = expanded.resolve(strict=True)
    except OSError:
        _console.print(t_rich("docker.mount.not_exist", path=raw_path))
        return
    if not resolved.is_dir():
        _console.print(t_rich("docker.mount.not_dir", path=raw_path))
        return

    result = docker_read_config()
    if result is None:
        return
    config_path, data = result
    mounts = _docker_get_mounts(data)
    resolved_str = str(resolved)

    if _is_duplicate_mount(mounts, resolved_str):
        _console.print(t_rich("docker.mount.already", path=resolved))
        return

    from ductor_bot.infra.json_store import atomic_json_save

    mounts.append(resolved_str)
    atomic_json_save(config_path, data)

    from ductor_bot.infra.docker import resolve_mount_target

    pair = resolve_mount_target(resolved_str, set())
    target_info = f" -> [cyan]{pair[1]}[/cyan]" if pair else ""
    _console.print(t_rich("docker.mount.done", path=resolved, target=target_info))
    _console.print(t_rich("docker.mount.apply_hint"))


def _find_mount_entry(mounts: list[object], raw_path: str) -> str | None:
    """Find a matching entry in the mounts list by exact, resolved, or basename match."""
    expanded = _expand_path(raw_path)
    try:
        resolved_str = str(expanded.resolve())
    except OSError:
        resolved_str = str(expanded)
    query_basename = expanded.name

    for entry in mounts:
        if not isinstance(entry, str):
            continue
        if entry in (raw_path, resolved_str):
            return entry
        try:
            if str(_expand_path(entry).resolve()) == resolved_str:
                return entry
        except OSError:
            pass
        if Path(entry).name == query_basename:
            return entry
    return None


def docker_unmount(args: list[str]) -> None:
    """Remove a host directory from the Docker sandbox mounts."""
    raw_path = _parse_docker_mount_arg(args)
    if not raw_path:
        _console.print(t_rich("docker.unmount.usage"))
        return

    result = docker_read_config()
    if result is None:
        return
    config_path, data = result

    docker = data.get("docker", {})
    if not isinstance(docker, dict) or not isinstance(docker.get("mounts"), list):
        _console.print(t_rich("docker.mounts.empty"))
        return
    mounts: list[object] = docker["mounts"]

    to_remove = _find_mount_entry(mounts, raw_path)
    if to_remove is None:
        _console.print(t_rich("docker.unmount.not_found", path=raw_path))
        return

    from ductor_bot.infra.json_store import atomic_json_save

    mounts.remove(to_remove)
    atomic_json_save(config_path, data)
    _console.print(t_rich("docker.unmount.done", path=to_remove))
    _console.print(t_rich("docker.mount.apply_hint"))


def docker_list_mounts() -> None:
    """List all configured Docker sandbox mounts."""
    result = docker_read_config()
    if result is None:
        return
    _, data = result

    docker = data.get("docker", {})
    mounts = docker.get("mounts", []) if isinstance(docker, dict) else []
    if not isinstance(mounts, list) or not mounts:
        _console.print(t_rich("docker.mounts.empty"))
        _console.print(t_rich("docker.mounts.add_hint"))
        return

    from ductor_bot.infra.docker import resolve_mount_target

    table = Table(show_header=True, box=None, padding=(0, 2))
    table.add_column("Host Path", style="bold")
    table.add_column("Container Path", style="cyan")
    table.add_column("Status")

    used_names: set[str] = set()
    for entry in mounts:
        if not isinstance(entry, str):
            continue
        pair = resolve_mount_target(entry, used_names)
        if pair is not None:
            host_resolved, container_target = pair
            table.add_row(str(host_resolved), container_target, "[green]OK[/green]")
        else:
            table.add_row(entry, "-", "[red]not found[/red]")

    _console.print(table)


# -- extras subcommands -----------------------------------------------------


def _docker_get_extras(data: dict[str, object]) -> list[object]:
    """Return the ``docker.extras`` list from config, ensuring it exists."""
    docker = data.setdefault("docker", {})
    if not isinstance(docker, dict):
        data["docker"] = docker = {}
    raw = docker.get("extras")
    if not isinstance(raw, list):
        raw = []
        docker["extras"] = raw
    return raw


def docker_extras_list() -> None:
    """List available and selected Docker extras."""
    from ductor_bot.infra.docker_extras import extras_for_display

    result = docker_read_config()
    selected: set[str] = set()
    if result is not None:
        _, data = result
        docker = data.get("docker", {})
        if isinstance(docker, dict):
            raw = docker.get("extras", [])
            if isinstance(raw, list):
                selected = {str(e) for e in raw}

    table = Table(show_header=True, box=None, padding=(0, 2))
    table.add_column(t_rich("docker.extras.col_package"), style="bold", min_width=18)
    table.add_column(t_rich("docker.extras.col_description"), min_width=30)
    table.add_column("ID", style="dim")
    table.add_column("Size", style="cyan", justify="right")
    table.add_column(t_rich("docker.extras.col_status"))

    for category, extras in extras_for_display():
        table.add_row(f"[bold yellow]{category}[/bold yellow]", "", "", "", "")
        for extra in extras:
            status = (
                t_rich("docker.extras.selected")
                if extra.id in selected
                else t_rich("docker.extras.not_selected")
            )
            table.add_row(
                f"  {extra.name}", extra.description, extra.id, extra.size_estimate, status
            )

    _console.print(table)
    if selected:
        _console.print()
        _console.print(t_rich("docker.extras.apply_hint"))
    _console.print()
    _console.print(t_rich("docker.extras.add_hint"))
    _console.print(t_rich("docker.extras.remove_hint"))


def docker_extras_add(args: list[str]) -> None:
    """Add an extra to the Docker config."""
    from ductor_bot.infra.docker_extras import DOCKER_EXTRAS_BY_ID, resolve_extras

    positionals = [a for a in args if not a.startswith("-")]
    extra_id = positionals[2] if len(positionals) >= 3 else None

    if not extra_id:
        _console.print(t_rich("docker.extras.usage_add"))
        _console.print(t_rich("docker.extras.see_ids"))
        return

    if extra_id not in DOCKER_EXTRAS_BY_ID:
        _console.print(t_rich("docker.extras.unknown", id=extra_id))
        _console.print(t_rich("docker.extras.see_ids"))
        return

    result = docker_read_config()
    if result is None:
        return
    config_path, data = result
    extras = _docker_get_extras(data)
    current_set = {str(e) for e in extras}

    if extra_id in current_set:
        _console.print(t_rich("docker.extras.already_installed", id=extra_id))
        return

    # Collect the extra plus its transitive dependencies (topologically sorted,
    # dependencies before dependents), skipping ones already installed.
    new_ids = [e.id for e in resolve_extras([extra_id]) if e.id not in current_set]

    from ductor_bot.infra.json_store import atomic_json_save

    extras.extend(new_ids)
    atomic_json_save(config_path, data)

    added_names = [DOCKER_EXTRAS_BY_ID[i].name for i in new_ids]
    _console.print(t_rich("docker.extras.added", names=", ".join(added_names)))
    dep_ids = [i for i in new_ids if i != extra_id]
    if dep_ids:
        dep_names = [DOCKER_EXTRAS_BY_ID[i].name for i in dep_ids]
        _console.print(t_rich("docker.extras.auto_deps", names=", ".join(dep_names)))
    _console.print(t_rich("docker.extras.rebuild_hint"))


def docker_extras_remove(args: list[str]) -> None:
    """Remove an extra from the Docker config."""
    from ductor_bot.infra.docker_extras import DOCKER_EXTRAS, DOCKER_EXTRAS_BY_ID

    positionals = [a for a in args if not a.startswith("-")]
    extra_id = positionals[2] if len(positionals) >= 3 else None

    if not extra_id:
        _console.print(t_rich("docker.extras.usage_remove"))
        _console.print(t_rich("docker.extras.see_installed"))
        return

    result = docker_read_config()
    if result is None:
        return
    config_path, data = result
    extras = _docker_get_extras(data)
    current_set = {str(e) for e in extras}

    if extra_id not in current_set:
        _console.print(t_rich("docker.extras.not_installed", id=extra_id))
        return

    # Warn about reverse dependencies that are still installed.
    dependents = [
        e.name
        for e in DOCKER_EXTRAS
        if extra_id in e.depends_on and e.id in current_set and e.id != extra_id
    ]
    if dependents:
        _console.print(
            t_rich(
                "docker.extras.dep_warning",
                dependents=", ".join(dependents),
                name=DOCKER_EXTRAS_BY_ID[extra_id].name,
            )
        )

    from ductor_bot.infra.json_store import atomic_json_save

    extras.remove(extra_id)
    atomic_json_save(config_path, data)

    name = DOCKER_EXTRAS_BY_ID[extra_id].name if extra_id in DOCKER_EXTRAS_BY_ID else extra_id
    _console.print(t_rich("docker.extras.removed", name=name))
    _console.print(t_rich("docker.extras.rebuild_hint"))


def cmd_docker(args: list[str]) -> None:
    """Handle 'ductor docker <subcommand>'."""
    sub = _parse_docker_subcommand(args)
    if sub is None:
        print_docker_help()
        return

    dispatch: dict[str, Callable[[], None]] = {
        "rebuild": docker_rebuild,
        "enable": lambda: docker_set_enabled(enabled=True),
        "disable": lambda: docker_set_enabled(enabled=False),
        "mount": lambda: docker_mount(args),
        "unmount": lambda: docker_unmount(args),
        "mounts": docker_list_mounts,
        "extras": docker_extras_list,
        "extras-add": lambda: docker_extras_add(args),
        "extras-remove": lambda: docker_extras_remove(args),
    }
    _console.print()
    dispatch[sub]()
    _console.print()
