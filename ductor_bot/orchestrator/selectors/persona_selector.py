"""Persona picker for ``/persona`` and for new conversations.

Personas are offered, never inferred. There is no default and no fallback: a
conversation runs under the persona its user chose, or under none because they
chose none.

``Default`` appears only when the installation defines no personas at all, so
somebody else's checkout is not left with a dead menu. Where personas exist,
omitting the escape hatch is deliberate — an "unset" option would quietly become
the path of least resistance and undo the point of asking.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ductor_bot.i18n import t
from ductor_bot.orchestrator.selectors.models import Button, ButtonGrid, SelectorResponse
from ductor_bot.personas.catalog import load_personas
from ductor_bot.personas.store import NO_PERSONA

if TYPE_CHECKING:
    from ductor_bot.orchestrator.core import Orchestrator
    from ductor_bot.session.key import SessionKey

logger = logging.getLogger(__name__)

PRS_PREFIX = "prs:"

#: Personas are addressed by index: names are user-chosen and Telegram caps
#: callback_data at 64 bytes.
_DEFAULT_INDEX = -1
_BUTTONS_PER_ROW = 2
_DESC_CHARS = 80


def is_persona_selector_callback(data: str) -> bool:
    """Return True if *data* belongs to the persona picker."""
    return data.startswith(PRS_PREFIX)


def _short(text: str) -> str:
    text = " ".join(text.split())
    if len(text) <= _DESC_CHARS:
        return text
    return text[: _DESC_CHARS - 1].rstrip() + "…"


def _label(name: str) -> str:
    return name or t("persona.default_label")


def persona_selector(
    orch: Orchestrator, key: SessionKey, *, asking: bool = False
) -> SelectorResponse:
    """Build the picker. *asking* renders the new-conversation wording."""
    personas = load_personas()
    current = orch.personas.get(key.storage_key)

    header = t("persona.ask_header") if asking else t("persona.header")

    if not personas:
        # No agents defined: offer the single honest option rather than an
        # empty keyboard.
        return SelectorResponse(
            text=f"{header}\n\n{t('persona.none_defined')}",
            buttons=ButtonGrid(
                rows=[
                    [
                        Button(
                            text=t("persona.default_label"),
                            callback_data=f"{PRS_PREFIX}{_DEFAULT_INDEX}",
                        )
                    ]
                ]
            ),
        )

    lines = [header, ""]
    for p in personas:
        mark = "✅ " if p.name == current else ""
        lines.append(f"{mark}`{p.name}`")
        if p.description:
            lines.append(f"  {_short(p.description)}")

    buttons = [
        Button(
            text=f"✅ {p.name}" if p.name == current else p.name,
            callback_data=f"{PRS_PREFIX}{i}",
        )
        for i, p in enumerate(personas)
    ]
    rows = [buttons[i : i + _BUTTONS_PER_ROW] for i in range(0, len(buttons), _BUTTONS_PER_ROW)]

    return SelectorResponse(text="\n".join(lines), buttons=ButtonGrid(rows=rows))


def resolve_choice(index: int) -> str | None:
    """Map a callback index to a persona name, ``NO_PERSONA``, or ``None``."""
    if index == _DEFAULT_INDEX:
        return NO_PERSONA
    personas = load_personas()
    if 0 <= index < len(personas):
        return personas[index].name
    return None


def parse_callback(data: str) -> int | None:
    """Extract the index from ``prs:<index>``."""
    try:
        return int(data[len(PRS_PREFIX) :])
    except ValueError:
        logger.debug("Bad persona callback: %r", data)
        return None
