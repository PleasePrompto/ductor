"""Track in-flight CLI turns for crash recovery.

Persists the currently running foreground turn per chat so it can be
recovered after a crash or restart.  Uses the same atomic JSON utilities
as the rest of the infra layer.
"""

from __future__ import annotations

import logging
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ductor_bot.infra.json_store import atomic_json_save, load_json

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class InflightTurn:
    """State of a single in-flight CLI turn."""

    chat_id: str
    provider: str
    model: str
    session_id: str
    prompt_preview: str
    started_at: str
    is_recovery: bool
    path: str  # "normal" | "background"


def _turn_from_dict(data: dict[str, Any]) -> InflightTurn:
    """Reconstruct an InflightTurn from a JSON dict."""
    return InflightTurn(
        chat_id=str(data.get("chat_id", "")),
        provider=str(data.get("provider", "")),
        model=str(data.get("model", "")),
        session_id=str(data.get("session_id", "")),
        prompt_preview=str(data.get("prompt_preview", "")),
        started_at=str(data.get("started_at", "")),
        is_recovery=bool(data.get("is_recovery", False)),
        path=str(data.get("path", "normal")),
    )


class InflightTracker:
    """Write/remove inflight state atomically for crash recovery."""

    def __init__(self, path: Path) -> None:
        self._path = path

    def begin(self, turn: InflightTurn) -> None:
        """Mark a turn as in-flight (atomic write)."""
        data = self._load_raw()
        data[str(turn.chat_id)] = asdict(turn)
        atomic_json_save(self._path, {"turns": data})

    def complete(self, chat_id: str) -> None:
        """Remove a completed turn (atomic write)."""
        data = self._load_raw()
        key = str(chat_id)
        if key not in data:
            return
        del data[key]
        if data:
            atomic_json_save(self._path, {"turns": data})
        else:
            self._path.unlink(missing_ok=True)

    def load_interrupted(self, *, max_age_seconds: float) -> list[InflightTurn]:
        """Load turns that were in-flight at last shutdown.

        Filters:
        - ``is_recovery=True`` entries are never recovered (no infinite loops)
        - Entries older than *max_age_seconds* are dropped
        """
        data = self._load_raw()
        now = datetime.now(UTC)
        result: list[InflightTurn] = []
        for entry in data.values():
            turn = _turn_from_dict(entry)
            if turn.is_recovery:
                continue
            if not turn.chat_id:
                continue
            try:
                started = datetime.fromisoformat(turn.started_at)
                if started.tzinfo is None:
                    started = started.replace(tzinfo=UTC)
                age = (now - started).total_seconds()
                if age > max_age_seconds:
                    continue
            except (ValueError, TypeError):
                continue
            result.append(turn)
        return result

    def clear(self) -> None:
        """Remove the inflight file entirely."""
        self._path.unlink(missing_ok=True)

    def _load_raw(self) -> dict[str, Any]:
        """Load the raw turns dict from disk."""
        raw = load_json(self._path)
        if raw is None:
            return {}
        turns = raw.get("turns")
        if not isinstance(turns, dict):
            return {}
        return dict(turns)
