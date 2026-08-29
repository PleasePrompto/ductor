"""Tests for skill discovery.

The fixture mirrors a real installation's traps: two versions of one plugin, a
plugin shipping the same skill under two paths, and a disabled plugin still
present on disk.
"""

from __future__ import annotations

import json
from pathlib import Path

from ductor_bot.workspace.skill_catalog import (
    PERSONAL_GROUP,
    group_counts,
    load_catalog,
)


def _skill(path: Path, name: str, *, description: str = "d", slash_only: bool = False) -> None:
    path.mkdir(parents=True, exist_ok=True)
    fm = [f"name: {name}", f"description: {description}"]
    if slash_only:
        fm.append("disable-model-invocation: true")
    (path / "SKILL.md").write_text("---\n" + "\n".join(fm) + "\n---\n\nbody\n")


def _config(tmp_path: Path) -> Path:
    cfg = tmp_path / ".claude"
    cache = cfg / "plugins" / "cache"

    # personal skill
    _skill(cfg / "skills" / "CTOwithMonkeyArmy", "CTOwithMonkeyArmy", slash_only=True)

    # enabled plugin, two versions on disk — only 6.3.0 is registered
    _skill(cache / "official" / "superpowers" / "6.2.0" / "skills" / "old", "brainstorming")
    _skill(
        cache / "official" / "superpowers" / "6.3.0" / "skills" / "brainstorming", "brainstorming"
    )
    _skill(
        cache / "official" / "superpowers" / "6.3.0" / "skills" / "writing-plans", "writing-plans"
    )

    # enabled plugin shipping one skill under two paths
    _skill(cache / "pony" / "ponytail" / "4.7.0" / "skills" / "ponytail-review", "ponytail-review")
    _skill(
        cache / "pony" / "ponytail" / "4.7.0" / ".openclaw" / "skills" / "ponytail-review",
        "ponytail-review",
    )

    # disabled plugin, still on disk
    _skill(cache / "cf" / "cloudflare" / "1.0.0" / "skills" / "wrangler", "wrangler")

    (cfg / "settings.json").write_text(
        json.dumps(
            {
                "enabledPlugins": {
                    "superpowers@official": True,
                    "ponytail@pony": True,
                    "cloudflare@cf": False,
                }
            }
        )
    )
    (cfg / "plugins" / "installed_plugins.json").write_text(
        json.dumps(
            {
                "plugins": {
                    "superpowers@official": [
                        {"installPath": str(cache / "official" / "superpowers" / "6.3.0")}
                    ],
                    "ponytail@pony": [{"installPath": str(cache / "pony" / "ponytail" / "4.7.0")}],
                    "cloudflare@cf": [{"installPath": str(cache / "cf" / "cloudflare" / "1.0.0")}],
                }
            }
        )
    )
    return cfg


def test_only_registered_version_is_used(tmp_path: Path) -> None:
    """A stale plugin version on disk must not contribute skills."""
    skills = load_catalog(_config(tmp_path))
    brainstorming = [s for s in skills if s.name == "brainstorming"]
    assert len(brainstorming) == 1
    assert "6.3.0" in str(brainstorming[0].path)


def test_duplicate_paths_in_one_plugin_are_deduped(tmp_path: Path) -> None:
    """skills/ and .openclaw/skills/ are the same skill, not two."""
    skills = load_catalog(_config(tmp_path))
    assert len([s for s in skills if s.name == "ponytail-review"]) == 1


def test_disabled_plugins_are_excluded(tmp_path: Path) -> None:
    skills = load_catalog(_config(tmp_path))
    assert not [s for s in skills if s.name == "wrangler"]


def test_personal_skills_are_found_and_grouped(tmp_path: Path) -> None:
    skills = load_catalog(_config(tmp_path))
    personal = [s for s in skills if s.group == PERSONAL_GROUP]
    assert [s.name for s in personal] == ["CTOwithMonkeyArmy"]


def test_slash_only_flag_is_read(tmp_path: Path) -> None:
    skills = load_catalog(_config(tmp_path))
    by_name = {s.name: s for s in skills}
    assert by_name["CTOwithMonkeyArmy"].slash_only is True
    assert by_name["brainstorming"].slash_only is False


def test_command_property(tmp_path: Path) -> None:
    skills = load_catalog(_config(tmp_path))
    assert {s.command for s in skills} >= {"/brainstorming", "/CTOwithMonkeyArmy"}


def test_group_counts_put_personal_first(tmp_path: Path) -> None:
    counts = group_counts(load_catalog(_config(tmp_path)))
    assert next(iter(counts)) == PERSONAL_GROUP
    assert counts == {PERSONAL_GROUP: 1, "ponytail": 1, "superpowers": 2}


def test_missing_config_dir_is_not_fatal(tmp_path: Path) -> None:
    assert load_catalog(tmp_path / "nope") == []


def test_description_with_colons_survives_parsing(tmp_path: Path) -> None:
    """Skill descriptions routinely contain colons; a naive split would truncate."""
    cfg = tmp_path / ".claude"
    _skill(cfg / "skills" / "x", "x", description="Use when: a, b: c — and more")
    (cfg / "settings.json").write_text("{}")
    skills = load_catalog(cfg)
    assert skills[0].description == "Use when: a, b: c — and more"


def test_symlinked_skill_dirs_are_found(tmp_path: Path) -> None:
    """ductor's skill-sync links shared skills in; rglob alone would miss them."""
    cfg = tmp_path / ".claude"
    real = tmp_path / "shared" / "skill-creator"
    _skill(real, "skill-creator")
    (cfg / "skills").mkdir(parents=True)
    (cfg / "skills" / "skill-creator").symlink_to(real, target_is_directory=True)
    (cfg / "settings.json").write_text("{}")

    names = {s.name for s in load_catalog(cfg)}
    assert "skill-creator" in names
