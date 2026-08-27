"""Picker for how often the Consult topic is wiped.

Offered as presets rather than a free-form schedule: this is set from a phone,
and the choice that matters is "how long does anything I say here survive".
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ductor_bot.i18n import t
from ductor_bot.messenger.telegram.consult_wipe import SCHEDULES, schedule_label
from ductor_bot.orchestrator.selectors.models import Button, ButtonGrid, SelectorResponse

if TYPE_CHECKING:
    from ductor_bot.orchestrator.core import Orchestrator

logger = logging.getLogger(__name__)

CNS_PREFIX = "cns:"
_BUTTONS_PER_ROW = 2


def is_consult_selector_callback(data: str) -> bool:
    return data.startswith(CNS_PREFIX)


def consult_selector(orch: Orchestrator) -> SelectorResponse:
    """Show the current wipe schedule and the alternatives."""
    current = orch.config.consult_wipe
    hour = orch.config.consult_wipe_hour

    lines = [t("consult.header"), "", t("consult.explains")]
    buttons = [
        Button(
            text=f"✅ {schedule_label(name, hour)}" if name == current else schedule_label(name, hour),
            callback_data=f"{CNS_PREFIX}{index}",
        )
        for index, name in enumerate(SCHEDULES)
    ]
    rows = [buttons[i : i + _BUTTONS_PER_ROW] for i in range(0, len(buttons), _BUTTONS_PER_ROW)]
    return SelectorResponse(text="\n".join(lines), buttons=ButtonGrid(rows=rows))


def resolve_choice(index: int) -> str | None:
    """Map a callback index to a schedule id."""
    if 0 <= index < len(SCHEDULES):
        return SCHEDULES[index]
    return None


def parse_callback(data: str) -> int | None:
    try:
        return int(data[len(CNS_PREFIX) :])
    except ValueError:
        logger.debug("Bad consult callback: %r", data)
        return None
