"""Resolve per-topic project roots to working directories.

The ``project_roots`` config maps a topic to a directory the CLI should run
in instead of the shared workspace. Keys are matched in priority order:

1. the human-readable topic name (as shown in Telegram),
2. ``"<chat_id>:<topic_id>"`` — disambiguates equal topic ids across chats,
3. ``"<topic_id>"`` — plain topic id.
"""

from __future__ import annotations

import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def resolve_project_root(
    roots: dict[str, str],
    *,
    chat_id: int,
    topic_id: int | None,
    topic_name: str | None,
) -> str | None:
    """Return the resolved project root for a topic, or ``None``.

    Candidate keys are tried in priority order: *topic_name*, then
    ``"{chat_id}:{topic_id}"``, then ``str(topic_id)``. The first key present
    in *roots* whose path (after ``~`` expansion) is an existing directory
    wins; its absolute resolved path is returned. A configured path that does
    not exist logs a warning and the search continues with lower-priority
    keys.

    Returns ``None`` when *roots* is empty, *topic_id* is ``None`` (general
    chat / no topic), or no candidate matches an existing directory.
    """
    if not roots or topic_id is None:
        return None

    candidates: list[str] = []
    if topic_name:
        candidates.append(topic_name)
    candidates.append(f"{chat_id}:{topic_id}")
    candidates.append(str(topic_id))

    for key in candidates:
        raw = roots.get(key)
        if raw is None:
            continue
        path = Path(raw).expanduser()
        if path.is_dir():
            return str(path.resolve())
        logger.warning("project_roots[%r] points to non-existent directory: %s", key, raw)
    return None
