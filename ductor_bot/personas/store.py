"""Per-conversation persona choices, and the prompts held while choosing.

Kept beside the session store rather than inside it: the choice has to be made
*before* a session exists — that is the point of asking on a new conversation —
and the existing session record is created by the first run.

The chosen persona is persisted so a restart does not re-ask. The held prompt is
not: if the bot restarts between the question and the answer, the user taps
again and their message is gone. Persisting a queued instruction across a
restart, to be executed later without them watching, is worse than asking twice.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from ductor_bot.infra.atomic_io import atomic_text_save

logger = logging.getLogger(__name__)

#: Marker for "explicitly no persona". Distinct from absent, which means
#: unanswered — the difference decides whether the user is asked again.
NO_PERSONA = ""


class PersonaStore:
    """Chosen personas keyed by session storage key."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._chosen: dict[str, str] = self._load()
        # Held prompts stay in memory only; see the module docstring.
        self._pending: dict[str, str] = {}

    # -- persistence ----------------------------------------------------------

    def _load(self) -> dict[str, str]:
        if not self._path.is_file():
            return {}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            # Broad on purpose: a damaged or unreadable store must degrade to
            # "nobody has chosen yet" and ask again, never prevent startup.
            logger.warning("Cannot read persona store %s: %s", self._path, exc)
            return {}
        return {k: str(v) for k, v in data.items()} if isinstance(data, dict) else {}

    def _save(self) -> None:
        try:
            atomic_text_save(self._path, json.dumps(self._chosen, indent=2) + "\n")
        except OSError as exc:
            logger.warning("Cannot write persona store %s: %s", self._path, exc)

    # -- choices --------------------------------------------------------------

    def has_choice(self, key: str) -> bool:
        """True when this conversation has answered, including 'no persona'."""
        return key in self._chosen

    def get(self, key: str) -> str | None:
        """The chosen persona, ``NO_PERSONA`` for an explicit none, else ``None``."""
        return self._chosen.get(key)

    def set(self, key: str, persona: str) -> None:
        self._chosen[key] = persona
        self._save()

    def clear(self, key: str) -> None:
        """Forget the choice, so the next message asks again.

        Called on /new and /reset: a fresh conversation should pick its own
        persona rather than inheriting one from work that has ended.
        """
        if self._chosen.pop(key, None) is not None:
            self._save()
        self._pending.pop(key, None)

    # -- held prompts ---------------------------------------------------------

    def hold(self, key: str, prompt: str) -> None:
        """Keep the message that triggered the question."""
        self._pending[key] = prompt

    def take(self, key: str) -> str | None:
        """Return and forget the held message."""
        return self._pending.pop(key, None)

    def peek(self, key: str) -> str | None:
        return self._pending.get(key)
