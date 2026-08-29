"""Discovery of the personas a user has defined.

A persona is a Claude Code agent: a Markdown file in ``<config>/agents/`` whose
frontmatter carries a name and description. Passing ``--agent <name>`` makes it
govern the run.

Nothing here guesses. A persona is only ever offered because the user wrote the
file, and only ever applied because the user chose it.
"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Persona:
    """One selectable agent."""

    name: str
    description: str
    path: Path


def config_dir() -> Path:
    """Where Claude Code keeps its configuration.

    Honours ``CLAUDE_CONFIG_DIR`` the same way the CLI does, so a bot running
    against a non-default config directory finds that directory's agents.
    """
    env = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if env:
        return Path(env).expanduser()
    return Path.home() / ".claude"


def _parse_frontmatter(text: str) -> dict[str, str]:
    """Extract the leading ``---`` block as flat key -> value.

    Not a YAML parser: agent descriptions routinely contain colons and commas,
    so only a line beginning at column 0 opens a new key and everything else
    continues the current value.
    """
    if not text.startswith("---"):
        return {}
    out: dict[str, str] = {}
    key: str | None = None
    for line in text.splitlines()[1:]:
        if line.strip() == "---":
            break
        if line[:1] not in (" ", "\t") and ":" in line:
            raw_key, _, value = line.partition(":")
            key = raw_key.strip()
            out[key] = value.strip().strip("\"'")
        elif key is not None and line.strip():
            out[key] = f"{out[key]} {line.strip()}".strip()
    return out


def load_personas(config: Path | None = None) -> list[Persona]:
    """Return the personas defined for this installation, name-sorted.

    An empty list is a normal state, not an error: most installations have no
    agents, and the feature stays invisible for them.
    """
    base = (config or config_dir()) / "agents"
    if not base.is_dir():
        return []

    personas: list[Persona] = []
    try:
        entries = sorted(base.glob("*.md"))
    except OSError as exc:
        logger.warning("Cannot read personas from %s: %s", base, exc)
        return []

    for file in entries:
        try:
            meta = _parse_frontmatter(file.read_text(encoding="utf-8", errors="replace"))
        except OSError:
            logger.debug("Unreadable persona file: %s", file)
            continue
        name = meta.get("name") or file.stem
        personas.append(Persona(name=name, description=meta.get("description", ""), path=file))

    return sorted(personas, key=lambda p: p.name.lower())


def is_known(name: str, config: Path | None = None) -> bool:
    """True when *name* is a defined persona."""
    return any(p.name == name for p in load_personas(config))
