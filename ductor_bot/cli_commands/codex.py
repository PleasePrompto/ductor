"""Codex helper CLI subcommands (``ductor codex ...``)."""

from __future__ import annotations

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from ductor_bot.cli.codex_handoff import (
    CodexResumeTarget,
    build_codex_resume_command,
    find_resume_target,
    latest_resume_target,
    load_resume_targets,
    run_codex_resume,
)
from ductor_bot.workspace.paths import resolve_paths

_console = Console()

_CODEX_SUBCOMMANDS = frozenset({"resume"})


def _parse_codex_args(args: list[str]) -> tuple[str | None, list[str]]:
    found_codex = False
    sub: str | None = None
    rest: list[str] = []
    for arg in args:
        if not found_codex:
            if arg == "codex":
                found_codex = True
            continue
        if sub is None:
            if arg.startswith("-"):
                rest.append(arg)
                continue
            sub = arg if arg in _CODEX_SUBCOMMANDS else None
            if sub is None:
                return None, []
            continue
        rest.append(arg)
    return sub, rest


def _wants_help(args: list[str]) -> bool:
    return "--help" in args or "-h" in args or "help" in args


def print_codex_help() -> None:
    """Print Codex helper command help."""
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(style="bold green", min_width=36)
    table.add_column()
    table.add_row("ductor codex resume", "List ductor-known Codex sessions")
    table.add_row("ductor codex resume @name", "Resume a named session in the Codex TUI")
    table.add_row("ductor codex resume --main", "Resume the newest main chat Codex session")
    table.add_row("ductor codex resume --last-phone", "Resume the newest ductor Codex session")
    table.add_row("ductor codex resume --print @name", "Print the desktop resume command")
    table.add_row("ductor codex resume --help", "Show resume examples and notes")
    _console.print(Panel(table, title="[bold]Codex Commands[/bold]", border_style="blue", padding=(1, 0)))


def cmd_codex(args: list[str]) -> None:
    """Handle ``ductor codex <subcommand>``."""
    sub, rest = _parse_codex_args(args)
    if _wants_help(args) and sub != "resume":
        print_codex_help()
        return
    if sub != "resume":
        print_codex_help()
        return
    if _wants_help(rest):
        print_codex_resume_help()
        return
    _cmd_resume(rest)


def print_codex_resume_help() -> None:
    """Print detailed help for ``ductor codex resume``."""
    table = Table(show_header=False, box=None, padding=(0, 2))
    table.add_column(style="bold green", min_width=40)
    table.add_column()
    table.add_row("ductor codex resume", "List resumable Codex sessions known to ductor")
    table.add_row("ductor codex resume @pm99", "Open named session @pm99 in interactive Codex")
    table.add_row("ductor codex resume --main", "Open the newest main-chat Codex session")
    table.add_row("ductor codex resume --last-phone", "Open the newest Codex session ductor knows about")
    table.add_row("ductor codex resume --print @pm99", "Print the underlying codex command")
    notes = (
        "Ductor uses `codex exec`, so desktop handoff uses "
        "`codex resume --include-non-interactive --all --cd <project> <session_id>`."
    )
    _console.print(
        Panel(
            table,
            title="[bold]Codex Desktop Resume[/bold]",
            subtitle=notes,
            border_style="blue",
            padding=(1, 0),
        )
    )


def _cmd_resume(rest: list[str]) -> None:
    paths = resolve_paths()
    print_only = _consume_flag(rest, "--print")
    main_only = _consume_flag(rest, "--main")
    last_phone = _consume_flag(rest, "--last-phone")
    selector = next((item for item in rest if not item.startswith("-")), "")

    if selector:
        target = find_resume_target(paths, selector)
        if target is None:
            _console.print(f"[red]No ductor Codex session found for {selector!r}.[/red]")
            _print_resume_list(load_resume_targets(paths))
            return
    elif main_only or last_phone:
        target = latest_resume_target(paths, main_only=main_only)
        if target is None:
            scope = "main chat " if main_only else ""
            _console.print(f"[yellow]No {scope}Codex sessions with a session id were found.[/yellow]")
            return
    else:
        _print_resume_list(load_resume_targets(paths))
        return

    command = build_codex_resume_command(target.session_id, target.working_dir)
    if print_only:
        _console.print(command)
        return

    _console.print(f"[dim]Launching:[/dim] {command}")
    raise SystemExit(run_codex_resume(target.session_id, target.working_dir))


def _consume_flag(rest: list[str], flag: str) -> bool:
    found = flag in rest
    while flag in rest:
        rest.remove(flag)
    return found


def _print_resume_list(targets: list[CodexResumeTarget]) -> None:
    if not targets:
        _console.print("[yellow]No ductor-known Codex sessions with a session id were found.[/yellow]")
        _console.print("Use Telegram to run a Codex turn first, then try again.")
        return
    table = Table(show_header=True, box=None, padding=(0, 2))
    table.add_column("Target", style="bold")
    table.add_column("Model")
    table.add_column("Project")
    table.add_column("Command")
    for target in targets:
        table.add_row(
            target.target,
            target.model or "-",
            target.working_dir or "-",
            build_codex_resume_command(target.session_id, target.working_dir),
        )
    _console.print(Panel(table, title="[bold]Ductor Codex Sessions[/bold]", border_style="blue"))
