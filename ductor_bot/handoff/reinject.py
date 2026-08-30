"""Which conversations are owed a handoff re-injection.

Compaction keeps the same session id, so ``is_new`` is False on the turn after
it and the handoff would never be put back in front of the model — consolidation
would write a careful document that nobody then reads. The boundary sets a flag;
the next turn takes it.

Memory only, like the other pending-state holders beside it: a flag that
survived a restart would re-inject into a conversation that has moved on.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ductor_bot.session.key import SessionKey


class ReinjectFlags:
    """One pending re-injection per conversation."""

    def __init__(self) -> None:
        self._pending: set[tuple[int, int | None]] = set()

    def mark(self, key: SessionKey) -> None:
        """Record that this conversation should be re-shown its handoff."""
        self._pending.add(key.lock_key)

    def take(self, key: SessionKey) -> bool:
        """True once after a mark, then False until marked again."""
        try:
            self._pending.remove(key.lock_key)
        except KeyError:
            return False
        return True
