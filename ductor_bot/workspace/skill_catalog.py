"""Discovery of the skills a Claude Code session will actually load.

Globbing the plugin cache over-reports badly: several versions of the same
plugin can sit side by side (e.g. superpowers 6.2.0 alongside 6.3.0), some
plugins ship the same skill under two paths (``skills/`` and
``.openclaw/skills/``), and disabled plugins stay on disk. A real installation
can therefore hold substantially more ``SKILL.md`` files than it has loadable
skills.

So the catalog follows the same two files Claude Code does:

* ``settings.json`` -> ``enabledPlugins`` decides which plugins count at all
* ``plugins/installed_plugins.json`` -> ``installPath`` picks the active version

and de-duplicates by skill name within a plugin, preferring the shallowest path.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

#: Skills the model may not auto-invoke; only an explicit slash command loads
#: them. Mirrors Claude Code's ``disable-model-invocation`` frontmatter key.
_SLASH_ONLY_KEY = "disable-model-invocation"

PERSONAL_GROUP = "personal"


@dataclass(frozen=True, slots=True)
class Skill:
    """One loadable skill."""

    name: str
    description: str
    group: str
    slash_only: bool
    path: Path

    @property
    def command(self) -> str:
        """The slash command that loads this skill."""
        return f"/{self.name}"


def _parse_frontmatter(text: str) -> dict[str, str]:
    """Extract the YAML-ish frontmatter block as flat key -> value.

    Deliberately not a YAML parser: skill frontmatter is flat, and descriptions
    routinely contain colons, quotes and commas that a naive split would mangle.
    Only the first colon on a line starting at column 0 opens a new key;
    everything else is a continuation of the current value.
    """
    if not text.startswith("---"):
        return {}
    lines = text.splitlines()
    out: dict[str, str] = {}
    key: str | None = None
    for line in lines[1:]:
        if line.strip() == "---":
            break
        if line[:1] not in (" ", "\t") and ":" in line:
            raw_key, _, value = line.partition(":")
            key = raw_key.strip()
            out[key] = value.strip().strip("\"'")
        elif key is not None and line.strip():
            out[key] = f"{out[key]} {line.strip()}".strip()
    return out


def _read_skill(skill_md: Path, group: str) -> Skill | None:
    try:
        text = skill_md.read_text(encoding="utf-8", errors="replace")
    except OSError:
        logger.debug("Unreadable skill file: %s", skill_md)
        return None
    meta = _parse_frontmatter(text)
    # Fall back to the directory name: a skill without frontmatter still loads.
    name = meta.get("name") or skill_md.parent.name
    slash_only = meta.get(_SLASH_ONLY_KEY, "").lower() in ("true", "yes", "1")
    return Skill(
        name=name,
        description=meta.get("description", ""),
        group=group,
        slash_only=slash_only,
        path=skill_md,
    )


def _skill_files(root: Path) -> set[Path]:
    """Every SKILL.md under *root*, including inside symlinked skill directories.

    ``rglob`` does not descend into symlinked directories, and skill dirs are
    routinely symlinks — ductor's own skill-sync links shared skills into
    ``~/.claude/skills``. Direct children are therefore checked explicitly.
    """
    found = set(root.rglob("SKILL.md"))
    try:
        for child in root.iterdir():
            if child.is_dir():  # follows symlinks, unlike rglob
                candidate = child / "SKILL.md"
                if candidate.is_file():
                    found.add(candidate)
    except OSError:
        pass
    return found


def _collect(root: Path, group: str) -> list[Skill]:
    """Find skills under *root*, keeping the shallowest path per skill name."""
    if not root.is_dir():
        return []
    best: dict[str, Skill] = {}
    for skill_md in sorted(_skill_files(root)):
        skill = _read_skill(skill_md, group)
        if skill is None:
            continue
        current = best.get(skill.name)
        if current is None or len(skill.path.parts) < len(current.path.parts):
            best[skill.name] = skill
    return list(best.values())


def _enabled_plugin_paths(config_dir: Path) -> dict[str, Path]:
    """Map ``plugin@marketplace`` -> active install path, enabled plugins only."""
    settings = config_dir / "settings.json"
    registry = config_dir / "plugins" / "installed_plugins.json"
    try:
        enabled = json.loads(settings.read_text(encoding="utf-8")).get("enabledPlugins") or {}
        installed = json.loads(registry.read_text(encoding="utf-8")).get("plugins") or {}
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Cannot read plugin registry: %s", exc)
        return {}

    paths: dict[str, Path] = {}
    for key, is_on in enabled.items():
        if not is_on:
            continue
        entries = installed.get(key) or []
        if not entries:
            continue
        install_path = entries[0].get("installPath")
        if install_path:
            paths[key] = Path(install_path)
    return paths


def load_catalog(config_dir: Path) -> list[Skill]:
    """Return every skill a session under *config_dir* can load, name-sorted."""
    skills: list[Skill] = _collect(config_dir / "skills", PERSONAL_GROUP)

    for key, install_path in _enabled_plugin_paths(config_dir).items():
        group = key.split("@", 1)[0]
        skills.extend(_collect(install_path, group))

    return sorted(skills, key=lambda s: (s.group != PERSONAL_GROUP, s.group, s.name.lower()))


def group_counts(skills: list[Skill]) -> dict[str, int]:
    """Skill count per group, personal first then alphabetical."""
    counts: dict[str, int] = {}
    for s in skills:
        counts[s.group] = counts.get(s.group, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: (kv[0] != PERSONAL_GROUP, kv[0].lower())))
