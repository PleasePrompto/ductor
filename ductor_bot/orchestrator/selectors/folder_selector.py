"""Folder picker for the binding gate and for ``/folder``.

Folders are offered, never inferred. The catalogue is ``project_roots``; the
choice is what gets recorded. A conversation works in the folder its user
picked, or in the shared workspace because they picked that.

``Shared workspace`` is always offered. Without it a topic could never be used
for plain conversation, and a gate with no acceptable answer is a lock rather
than a question. It is a choice, not a default — nothing selects it for you.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import TYPE_CHECKING

from ductor_bot.i18n import t
from ductor_bot.orchestrator.selectors.models import Button, ButtonGrid, SelectorResponse
from ductor_bot.workspace.topic_bindings import SHARED_WORKSPACE

if TYPE_CHECKING:
    from collections.abc import Mapping

    from ductor_bot.orchestrator.core import Orchestrator
    from ductor_bot.session.key import SessionKey

logger = logging.getLogger(__name__)

FLD_PREFIX = "fld:"

#: Roots are addressed by index: names are user-chosen and Telegram caps
#: callback_data at 64 bytes.
_SHARED_INDEX = -1
_BUTTONS_PER_ROW = 2


def is_folder_selector_callback(data: str) -> bool:
    """Return True if *data* belongs to the folder picker."""
    return data.startswith(FLD_PREFIX)


def _catalogue(roots: Mapping[str, str]) -> list[tuple[str, str]]:
    """Configured roots that currently exist, as ``(label, path)``.

    A root that has been deleted or renamed is dropped rather than offered:
    binding to it would only produce a working directory that cannot be used.
    """
    return [(label, path) for label, path in sorted(roots.items()) if Path(path).expanduser().is_dir()]


def folder_selector(
    orch: Orchestrator, key: SessionKey, *, asking: bool = False
) -> SelectorResponse:
    """Build the picker. *asking* renders the gate's wording."""
    roots = _catalogue(orch.config.project_roots)
    current = orch.bindings.get(key.storage_key)

    header = t("folder.ask_header") if asking else t("folder.header")

    shared_button = Button(
        text=(
            f"✅ {t('folder.shared_label')}"
            if current == SHARED_WORKSPACE and orch.bindings.has_choice(key.storage_key)
            else t("folder.shared_label")
        ),
        callback_data=f"{FLD_PREFIX}{_SHARED_INDEX}",
    )

    if not roots:
        # Nothing configured: the workspace is the only place work can happen,
        # so there is one honest option rather than an empty keyboard.
        return SelectorResponse(
            text=f"{header}\n\n{t('folder.none_defined')}",
            buttons=ButtonGrid(rows=[[shared_button]]),
        )

    lines = [header, ""]
    for label, path in roots:
        mark = "✅ " if current == path else ""
        lines.append(f"{mark}`{label}`")

    buttons = [
        Button(
            text=f"✅ {label}" if current == path else label,
            callback_data=f"{FLD_PREFIX}{i}",
        )
        for i, (label, path) in enumerate(roots)
    ]
    rows = [buttons[i : i + _BUTTONS_PER_ROW] for i in range(0, len(buttons), _BUTTONS_PER_ROW)]
    rows.append([shared_button])

    return SelectorResponse(text="\n".join(lines), buttons=ButtonGrid(rows=rows))


def resolve_choice(roots: Mapping[str, str], index: int) -> str | None:
    """Map a callback index to a directory, ``SHARED_WORKSPACE``, or ``None``."""
    if index == _SHARED_INDEX:
        return SHARED_WORKSPACE
    catalogue = _catalogue(roots)
    if 0 <= index < len(catalogue):
        return catalogue[index][1]
    return None


def parse_callback(data: str) -> int | None:
    """Extract the index from ``fld:<index>``."""
    try:
        return int(data[len(FLD_PREFIX) :])
    except ValueError:
        logger.debug("Bad folder callback: %r", data)
        return None
