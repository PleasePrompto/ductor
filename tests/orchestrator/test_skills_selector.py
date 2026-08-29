"""Tests for the /skills browser."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from ductor_bot.orchestrator.selectors.skills_selector import (
    SK_PREFIX,
    handle_skills_callback,
    is_skills_selector_callback,
    skill_detail,
    skills_group,
    skills_root,
)
from ductor_bot.workspace.skill_catalog import Skill

_SKILLS = [
    Skill("CTOwithMonkeyArmy", "Delegate to cheaper models", "personal", True, Path("/p/SKILL.md")),
    Skill(
        "brainstorming", "Explore intent before building", "superpowers", False, Path("/s/SKILL.md")
    ),
    Skill("writing-plans", "Turn a spec into a plan", "superpowers", False, Path("/s2/SKILL.md")),
]


def _orch() -> Any:
    return MagicMock()


def _patch_catalog(skills: list[Skill] | None = None) -> Any:
    return patch(
        "ductor_bot.orchestrator.selectors.skills_selector.load_catalog",
        return_value=_SKILLS if skills is None else skills,
    )


def _buttons(resp: Any) -> list[Any]:
    assert resp.buttons is not None
    return [b for row in resp.buttons.rows for b in row]


def test_is_skills_selector_callback() -> None:
    assert is_skills_selector_callback(f"{SK_PREFIX}root")
    assert not is_skills_selector_callback("acc:work")


def test_root_lists_groups_with_counts() -> None:
    with _patch_catalog():
        resp = skills_root(_orch())
    labels = [b.text for b in _buttons(resp)]
    assert labels == ["personal (1)", "superpowers (2)"]


def test_root_reports_totals() -> None:
    with _patch_catalog():
        resp = skills_root(_orch())
    assert "3" in resp.text  # total skills
    assert "2" in resp.text  # groups


def test_empty_catalog_has_no_buttons() -> None:
    with _patch_catalog([]):
        resp = skills_root(_orch())
    assert resp.buttons is None


def test_group_page_uses_clipboard_buttons() -> None:
    """Tapping a skill must copy its command, not fire a callback."""
    with _patch_catalog():
        resp = skills_group(_orch(), 1)  # superpowers
    skill_buttons = [b for b in _buttons(resp) if b.copy_text is not None]
    assert {b.copy_text for b in skill_buttons} == {"/brainstorming ", "/writing-plans "}


def test_group_page_marks_slash_only() -> None:
    with _patch_catalog():
        resp = skills_group(_orch(), 0)  # personal
    assert "🔒" in resp.text
    assert any("🔒" in b.text for b in _buttons(resp))


def test_group_page_has_back_button() -> None:
    with _patch_catalog():
        resp = skills_group(_orch(), 0)
    assert any(b.callback_data == f"{SK_PREFIX}root" for b in _buttons(resp))


def test_out_of_range_group_falls_back_to_root() -> None:
    with _patch_catalog():
        resp = skills_group(_orch(), 99)
    assert [b.text for b in _buttons(resp)] == ["personal (1)", "superpowers (2)"]


def test_callback_routes_to_group() -> None:
    with _patch_catalog():
        resp = handle_skills_callback(_orch(), f"{SK_PREFIX}g:1")
    assert "superpowers" in resp.text


def test_callback_with_bad_index_falls_back_to_root() -> None:
    with _patch_catalog():
        resp = handle_skills_callback(_orch(), f"{SK_PREFIX}g:notanint")
    assert [b.text for b in _buttons(resp)] == ["personal (1)", "superpowers (2)"]


def test_detail_shows_full_description_and_lock_note() -> None:
    with _patch_catalog():
        resp = skill_detail(_orch(), "CTOwithMonkeyArmy")
    assert "Delegate to cheaper models" in resp.text
    assert "🔒" in resp.text
    assert any(b.copy_text == "/CTOwithMonkeyArmy " for b in _buttons(resp))


def test_detail_accepts_leading_slash_and_is_case_insensitive() -> None:
    with _patch_catalog():
        resp = skill_detail(_orch(), "/BRAINSTORMING")
    assert "Explore intent before building" in resp.text


def test_detail_unknown_skill() -> None:
    with _patch_catalog():
        resp = skill_detail(_orch(), "nope")
    assert "nope" in resp.text
    assert resp.buttons is None


def test_config_dir_honours_env(tmp_path: Path) -> None:
    """Discovery must follow CLAUDE_CONFIG_DIR, as the CLI does.

    Asserted on the group page rather than the root view: the root only shows
    group names and counts, so an assertion there passes even when the override
    is ignored.
    """
    from ductor_bot.orchestrator.selectors import skills_selector

    (tmp_path / "skills" / "only-here").mkdir(parents=True)
    (tmp_path / "skills" / "only-here" / "SKILL.md").write_text(
        "---\nname: only-here\ndescription: proves the override was used\n---\n"
    )
    (tmp_path / "settings.json").write_text(json.dumps({}))

    with patch.dict("os.environ", {"CLAUDE_CONFIG_DIR": str(tmp_path)}):
        root = skills_selector.skills_root(_orch())
        group = skills_selector.skills_group(_orch(), 0)

    assert "personal (1)" in [b.text for b in _buttons(root)]
    assert "/only-here" in group.text
    assert any(b.copy_text == "/only-here " for b in _buttons(group))


# -- per-skill callbacks (transports without clipboard buttons) ----------------


def test_each_skill_button_has_distinct_callback_data() -> None:
    """Matrix renders buttons as reactions and Slack drops copy_text, so shared
    callback_data would make every skill in a group do the same thing."""
    with _patch_catalog():
        resp = skills_group(_orch(), 1)  # superpowers
    skill_buttons = [b for b in _buttons(resp) if b.copy_text is not None]
    payloads = [b.callback_data for b in skill_buttons]
    assert len(set(payloads)) == len(payloads)
    assert payloads == [f"{SK_PREFIX}s:1:0", f"{SK_PREFIX}s:1:1"]


def test_per_skill_callback_opens_that_skills_detail() -> None:
    with _patch_catalog():
        resp = handle_skills_callback(_orch(), f"{SK_PREFIX}s:1:1")
    assert "Turn a spec into a plan" in resp.text  # writing-plans, not brainstorming


def test_per_skill_callback_data_stays_within_telegram_limit() -> None:
    long_name = "z" * 300
    skills = [Skill(long_name, "d", "superpowers", False, Path("/s/SKILL.md"))]
    with _patch_catalog(skills):
        resp = skills_group(_orch(), 0)
    for b in _buttons(resp):
        assert len(b.callback_data.encode()) <= 64


def test_out_of_range_skill_index_falls_back_to_the_group() -> None:
    with _patch_catalog():
        resp = handle_skills_callback(_orch(), f"{SK_PREFIX}s:1:99")
    assert "superpowers" in resp.text


def test_out_of_range_group_in_skill_payload_falls_back_to_root() -> None:
    with _patch_catalog():
        resp = handle_skills_callback(_orch(), f"{SK_PREFIX}s:99:0")
    assert [b.text for b in _buttons(resp)] == ["personal (1)", "superpowers (2)"]


def test_malformed_skill_payload_falls_back_to_root() -> None:
    with _patch_catalog():
        for bad in (f"{SK_PREFIX}s:", f"{SK_PREFIX}s:1", f"{SK_PREFIX}s:a:b"):
            resp = handle_skills_callback(_orch(), bad)
            assert [b.text for b in _buttons(resp)] == ["personal (1)", "superpowers (2)"]
