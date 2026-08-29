"""Skill browser for ``/skills``.

Two levels, because a real setup has ~70 skills and a flat list is unusable on a
phone: the root lists plugin groups with counts, and a group page lists its
skills as clipboard buttons.

Tapping a skill copies ``/name `` rather than sending it, since a skill almost
always needs arguments — the user pastes it, adds the request, then sends. That
also keeps invocation explicit, which is the point of skills marked
``disable-model-invocation``.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ductor_bot.i18n import t
from ductor_bot.orchestrator.selectors.models import Button, ButtonGrid, SelectorResponse
from ductor_bot.workspace.skill_catalog import PERSONAL_GROUP, Skill, group_counts, load_catalog

if TYPE_CHECKING:
    from pathlib import Path

    from ductor_bot.orchestrator.core import Orchestrator

logger = logging.getLogger(__name__)

SK_PREFIX = "sk:"

_GROUPS_PER_ROW = 2
_SKILLS_PER_ROW = 2
#: Telegram rejects callback_data over 64 bytes, so group names are addressed by
#: index into the sorted group list rather than by name.
_ROOT = "root"
_DESC_CHARS = 90


def is_skills_selector_callback(data: str) -> bool:
    """Return True if *data* belongs to the skills browser."""
    return data.startswith(SK_PREFIX)


def _config_dir() -> Path:
    """Where Claude Code keeps its config for this agent.

    Honours ``CLAUDE_CONFIG_DIR`` the same way the CLI does — with it set, even
    ``.claude.json`` moves inside that directory.
    """
    import os
    from pathlib import Path as _Path

    env = os.environ.get("CLAUDE_CONFIG_DIR", "").strip()
    if env:
        return _Path(env).expanduser()
    return _Path.home() / ".claude"


def _catalog() -> list[Skill]:
    return load_catalog(_config_dir())


def _short(text: str) -> str:
    text = " ".join(text.split())
    if len(text) <= _DESC_CHARS:
        return text
    return text[: _DESC_CHARS - 1].rstrip() + "…"


def _group_label(group: str) -> str:
    return t("skills.personal_label") if group == PERSONAL_GROUP else group


def skills_root(_orch: Orchestrator) -> SelectorResponse:
    """Root view: one button per plugin group, with skill counts."""
    skills = _catalog()
    if not skills:
        return SelectorResponse(text=t("skills.none_found"))

    counts = group_counts(skills)
    groups = list(counts)
    buttons = [
        Button(text=f"{_group_label(g)} ({counts[g]})", callback_data=f"{SK_PREFIX}g:{i}")
        for i, g in enumerate(groups)
    ]
    rows = [buttons[i : i + _GROUPS_PER_ROW] for i in range(0, len(buttons), _GROUPS_PER_ROW)]
    header = t("skills.header", total=len(skills), groups=len(groups))
    # Claude Code's own skills are compiled into the binary rather than shipped
    # as SKILL.md files, so no filesystem catalog can list them. Say so instead
    # of quietly under-reporting.
    return SelectorResponse(
        text=f"{header}\n\n{t('skills.pick_group')}\n\n{t('skills.builtin_note')}",
        buttons=ButtonGrid(rows=rows),
    )


def skills_group(_orch: Orchestrator, index: int) -> SelectorResponse:
    """Group view: skills as clipboard buttons plus their descriptions."""
    skills = _catalog()
    groups = list(group_counts(skills))
    if not 0 <= index < len(groups):
        return skills_root(_orch)

    group = groups[index]
    members = [s for s in skills if s.group == group]

    lines = [t("skills.group_header", group=_group_label(group), count=len(members)), ""]
    for s in members:
        lock = " 🔒" if s.slash_only else ""
        lines.append(f"`{s.command}`{lock}")
        if s.description:
            lines.append(f"  {_short(s.description)}")

    # Telegram uses copy_text (the Bot API makes the two mutually exclusive), but
    # Matrix renders buttons as reactions and Slack has no clipboard button at
    # all. Those transports fall back to callback_data, so each button needs its own
    # payload or every skill in the group would trigger the same action.
    buttons = [
        Button(
            text=f"{s.name}{' 🔒' if s.slash_only else ''}",
            callback_data=f"{SK_PREFIX}s:{index}:{i}",
            copy_text=f"{s.command} ",
        )
        for i, s in enumerate(members)
    ]
    rows = [buttons[i : i + _SKILLS_PER_ROW] for i in range(0, len(buttons), _SKILLS_PER_ROW)]
    rows.append([Button(text=t("skills.btn_back"), callback_data=f"{SK_PREFIX}{_ROOT}")])

    return SelectorResponse(
        text="\n".join(lines) + f"\n\n{t('skills.tap_hint')}", buttons=ButtonGrid(rows=rows)
    )


def find_skill(_orch: Orchestrator, name: str) -> Skill | None:
    """Look up one skill by name, case-insensitively."""
    wanted = name.lstrip("/").strip().lower()
    return next((s for s in _catalog() if s.name.lower() == wanted), None)


def skill_detail(_orch: Orchestrator, name: str) -> SelectorResponse:
    """Full description for ``/skills <name>``."""
    skill = find_skill(_orch, name)
    if skill is None:
        return SelectorResponse(text=t("skills.not_found", name=name))

    lines = [
        t("skills.detail_header", name=skill.name, group=_group_label(skill.group)),
        "",
        skill.description or t("skills.no_description"),
    ]
    if skill.slash_only:
        lines += ["", t("skills.slash_only_note")]
    buttons = ButtonGrid(
        rows=[
            [
                Button(
                    text=t("skills.btn_copy", name=skill.name),
                    callback_data=f"{SK_PREFIX}{_ROOT}",
                    copy_text=f"{skill.command} ",
                )
            ],
            [Button(text=t("skills.btn_back"), callback_data=f"{SK_PREFIX}{_ROOT}")],
        ]
    )
    return SelectorResponse(text="\n".join(lines), buttons=buttons)


def skill_detail_at(_orch: Orchestrator, group_index: int, skill_index: int) -> SelectorResponse:
    """Detail view for one skill, addressed by group and position.

    Indexes rather than names keep callback_data well inside Telegram's 64-byte
    limit regardless of how long a skill is called.
    """
    skills = _catalog()
    groups = list(group_counts(skills))
    if not 0 <= group_index < len(groups):
        return skills_root(_orch)
    members = [s for s in skills if s.group == groups[group_index]]
    if not 0 <= skill_index < len(members):
        return skills_group(_orch, group_index)
    return skill_detail(_orch, members[skill_index].name)


def handle_skills_callback(orch: Orchestrator, data: str) -> SelectorResponse:
    """Route an ``sk:*`` callback.

    ``sk:g:<group>`` opens a group, ``sk:s:<group>:<skill>`` opens one skill.
    Malformed or out-of-range payloads fall back rather than raising.
    """
    payload = data[len(SK_PREFIX) :]

    if payload.startswith("s:"):
        parts = payload[2:].split(":")
        try:
            group_index, skill_index = int(parts[0]), int(parts[1])
        except (IndexError, ValueError):
            logger.debug("Bad skills payload: %r", payload)
            return skills_root(orch)
        return skill_detail_at(orch, group_index, skill_index)

    if payload.startswith("g:"):
        raw = payload[2:]
        try:
            return skills_group(orch, int(raw))
        except ValueError:
            logger.debug("Bad skills group index: %r", raw)

    return skills_root(orch)
