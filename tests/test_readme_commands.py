"""The README's command table has to match the bot people actually run.

It drifted badly once: it advertised `/tasks` and `/reset` months after both
were deleted, and omitted `/clear`, `/compact` and `/handoff` — the three most
useful ones. Documentation nobody can trust costs more than none, so the drift
is a test failure rather than a thing to notice later.
"""

from __future__ import annotations

import inspect
import re
from pathlib import Path

from ductor_bot.commands import BOT_COMMANDS
from ductor_bot.orchestrator.core import Orchestrator

README = Path(__file__).resolve().parent.parent / "README.md"
APP = Path(__file__).resolve().parent.parent / "ductor_bot" / "messenger" / "telegram" / "app.py"


def documented() -> set[str]:
    return set(re.findall(r"^\| `/(\w+)", README.read_text(encoding="utf-8"), re.MULTILINE))


def live() -> set[str]:
    app = APP.read_text(encoding="utf-8")
    return (
        set(re.findall(r"register\w*\(\s*[\"']/(\w+)", inspect.getsource(Orchestrator)))
        | {name for name, _desc in BOT_COMMANDS}
        | set(re.findall(r'Command\("(\w+)"', app))
        # /where and /leave are matched by prefix rather than registered.
        | set(re.findall(r'text_lower\.startswith\("/(\w+)"', app))
    )


def test_every_documented_command_exists() -> None:
    """A command in the table that the bot does not answer is a broken promise."""
    ghosts = documented() - live()

    assert not ghosts, f"README documents commands that do not exist: {sorted(ghosts)}"


def test_every_command_is_documented() -> None:
    """An undocumented command is one nobody finds."""
    missing = live() - documented()

    assert not missing, f"commands missing from the README table: {sorted(missing)}"
