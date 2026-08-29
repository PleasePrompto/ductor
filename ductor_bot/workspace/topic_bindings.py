"""Which folder a chat or topic works in, and how it got there.

A binding is a record of consent. It exists only because someone tapped a
button naming this folder for this topic, which is what separates it from
``project_roots``: that map is a *catalogue* of directories the user is willing
to work in, and an entry there is an offer, never a decision.

The two used to be the same thing, resolved by matching a topic's name. Topic
names are learned from ``forum_topic_created`` events and cached in memory, so
every restart silently un-matched every mapping and work ran in the shared
workspace with nothing said. Failing quietly in the direction of "somewhere
else entirely" is the reason this store exists.

The choice is persisted so a restart does not re-ask. The message held while
asking is not, matching ``PersonaStore``: replaying a queued instruction after
a restart, with nobody watching, is worse than asking twice.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from ductor_bot.infra.atomic_io import atomic_text_save

logger = logging.getLogger(__name__)

#: Marker for "explicitly the shared workspace". Distinct from absent, which
#: means unanswered — the difference decides whether the user is asked again.
SHARED_WORKSPACE = ""


class BindingStore:
    """Folder bindings keyed by session storage key."""

    def __init__(self, path: Path) -> None:
        self._path = path
        self._bound: dict[str, str] = self._load()
        # Held prompts stay in memory only; see the module docstring.
        self._pending: dict[str, str] = {}
        # Conversations that must never hold a binding; see protect().
        self._protected: set[str] = set()

    # -- persistence ----------------------------------------------------------

    def _load(self) -> dict[str, str]:
        if not self._path.is_file():
            return {}
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError, TypeError) as exc:
            # Broad on purpose: a damaged store must degrade to "nobody has
            # chosen yet" and ask again, never prevent startup.
            logger.warning("Cannot read binding store %s: %s", self._path, exc)
            return {}
        return {k: str(v) for k, v in data.items()} if isinstance(data, dict) else {}

    def _save(self) -> None:
        try:
            atomic_text_save(self._path, json.dumps(self._bound, indent=2) + "\n")
        except OSError as exc:
            logger.warning("Cannot write binding store %s: %s", self._path, exc)

    # -- protection -----------------------------------------------------------

    def protect(self, key: str) -> None:
        """Mark *key* as a conversation that may never hold a binding.

        Used for the General thread of a forum group. General's messages carry
        no ``message_thread_id``, so they collapse onto the chat-level key that
        a private chat would use — which meant a message typed outside a topic
        could bind a folder and start a fresh conversation in another topic's
        project directory, with the folder picker as the only warning.

        A protected key reads as "answered, shared workspace": the gate never
        asks, ``resolve`` never yields a directory, and ``set`` refuses. That
        covers stale entries written before this rule existed as well as any
        future caller, which is why the check lives here rather than at each
        of the four paths that can write a binding.

        Held in memory on purpose. Unlike the topic *names* whose in-memory
        cache failed (it was consulted when no update was present), this is
        re-derived from every incoming update, and every path that could bind
        is reached by an update from the same chat — so the mark is always set
        before anything can consult it.
        """
        self._protected.add(key)

    def is_protected(self, key: str) -> bool:
        """True when *key* may not be bound to a folder."""
        return key in self._protected

    # -- bindings -------------------------------------------------------------

    def has_choice(self, key: str) -> bool:
        """True when this chat has answered, including 'shared workspace'."""
        return key in self._protected or key in self._bound

    def get(self, key: str) -> str | None:
        """The bound directory, ``SHARED_WORKSPACE`` for an explicit none.

        ``None`` means unanswered. Never guessed: the caller asks rather than
        picking something plausible.
        """
        if key in self._protected:
            return SHARED_WORKSPACE
        return self._bound.get(key)

    def resolve(self, key: str) -> Path | None:
        """The bound directory as a usable path, or ``None``.

        ``None`` covers unanswered, the shared workspace, and a binding whose
        directory has since been deleted or renamed. Callers treat all three as
        "no project root"; the gate distinguishes them via ``has_choice``.
        """
        if key in self._protected:
            return None
        raw = self._bound.get(key)
        if not raw:
            return None
        path = Path(raw).expanduser()
        if not path.is_dir():
            logger.warning("Binding for %s points at a missing directory: %s", key, raw)
            return None
        return path

    def set(self, key: str, directory: str) -> bool:
        """Record the choice. False when *key* is protected and nothing changed."""
        if key in self._protected:
            logger.warning("Refused to bind protected conversation %s to %s", key, directory)
            return False
        self._bound[key] = directory
        self._save()
        return True

    def clear(self, key: str) -> None:
        """Forget the binding, so the next message asks again.

        Deliberately *not* called by /new or /reset: a folder is a property of
        the topic, a persona is a property of the conversation, and the two have
        different lifetimes.
        """
        if self._bound.pop(key, None) is not None:
            self._save()
        self._pending.pop(key, None)

    # -- held prompts ---------------------------------------------------------

    def hold(self, key: str, prompt: str) -> None:
        """Keep the message that triggered the question."""
        self._pending[key] = prompt

    def take(self, key: str) -> str | None:
        """Return and forget the held message."""
        return self._pending.pop(key, None)
