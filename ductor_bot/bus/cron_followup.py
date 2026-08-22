"""Durable follow-up context for interactive unicast cron results."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from ductor_bot.infra.json_store import atomic_json_save, load_json
from ductor_bot.session.key import SessionKey

if TYPE_CHECKING:
    from ductor_bot.bus.envelope import Envelope

logger = logging.getLogger(__name__)

_TRANSPORT_ALIASES = {
    "telegram": "tg",
    "matrix": "mx",
    "slack": "sl",
    "websocket": "api",
    "websocket_api": "api",
}


def canonical_transport(value: str) -> str:
    """Return the short transport id used by session and follow-up keys."""
    stripped = value.strip().lower()
    return _TRANSPORT_ALIASES.get(stripped, stripped or "tg")


def _key_for(transport: str, chat_id: int, topic_id: int | None) -> str:
    """Build the canonical storage key for one transport/chat/topic."""
    return SessionKey(
        transport=canonical_transport(transport),
        chat_id=chat_id,
        topic_id=topic_id,
    ).storage_key


@dataclass(frozen=True, slots=True)
class CronFollowupContext:
    """The cron message that the next user turn should answer."""

    title: str
    result_text: str
    status: str
    transport: str
    chat_id: int
    topic_id: int | None
    created_at: float
    expires_at: float
    envelope_id: str = ""

    @property
    def storage_key(self) -> str:
        """Return the canonical transport/chat/topic key."""
        return _key_for(self.transport, self.chat_id, self.topic_id)

    def to_dict(self) -> dict[str, Any]:
        """Serialize the context to the durable JSON representation."""
        return {
            "title": self.title,
            "result_text": self.result_text,
            "status": self.status,
            "transport": canonical_transport(self.transport),
            "chat_id": self.chat_id,
            "topic_id": self.topic_id,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "envelope_id": self.envelope_id,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, object]) -> CronFollowupContext | None:  # noqa: PLR0911
        """Parse one context defensively, returning ``None`` for bad data."""
        try:
            title = data.get("title")
            result_text = data.get("result_text")
            status = data.get("status", "")
            transport = data.get("transport", "tg")
            chat_id = data.get("chat_id")
            topic_id = data.get("topic_id")
            created_at = data.get("created_at")
            expires_at = data.get("expires_at")
            if not isinstance(title, str) or not isinstance(result_text, str):
                return None
            if not isinstance(status, str) or not isinstance(transport, str):
                return None
            if isinstance(chat_id, bool) or not isinstance(chat_id, (int, float, str)):
                return None
            if isinstance(topic_id, bool):
                return None
            if topic_id is not None and not isinstance(topic_id, (int, float, str)):
                return None
            if not isinstance(created_at, (int, float)) or isinstance(created_at, bool):
                return None
            if not isinstance(expires_at, (int, float)) or isinstance(expires_at, bool):
                return None
            parsed_chat_id = int(chat_id)
            parsed_topic_id = int(topic_id) if topic_id is not None else None
            envelope_id = data.get("envelope_id", "")
            if not isinstance(envelope_id, str):
                envelope_id = ""
            return cls(
                title=title,
                result_text=result_text,
                status=status,
                transport=canonical_transport(transport),
                chat_id=parsed_chat_id,
                topic_id=parsed_topic_id,
                created_at=float(created_at),
                expires_at=float(expires_at),
                envelope_id=envelope_id,
            )
        except (TypeError, ValueError, OverflowError):
            return None


def is_interactive_cron_result(result: str) -> bool:
    """Return whether a cron result asks the user for a follow-up.

    Cron reports are intentionally delivered raw.  Only explicit question or
    confirmation language creates follow-up state; an ordinary success/error
    report therefore keeps its existing behaviour and does not make the next
    unrelated user message inherit cron context.
    """
    if not result.strip():
        return False

    confirmation_markers = (
        "est-ce que",
        "est ce que",
        "souhaites-tu",
        "souhaitez-vous",
        "veux-tu",
        "voulez-vous",
        "dois-je",
        "faut-il",
        "merci de confirmer",
        "action à confirmer",
        "action a confirmer",
        "à confirmer",
        "a confirmer",
        "confirmation requise",
        "confirme-tu",
        "confirmez-vous",
        "please confirm",
        "would you like",
        "could you confirm",
        "shall i",
    )
    folded = result.casefold()
    if any(marker in folded for marker in confirmation_markers):
        return True

    # Most cron-generated user questions are rendered as a sentence ending in
    # '?', often after a Markdown list item.  Restrict the check to a line so
    # a stray question mark in an identifier cannot create state by itself.
    return any(line.rstrip().endswith(("?", "\uff1f")) for line in result.splitlines())


def build_cron_followup_prompt(context: CronFollowupContext, answer: str) -> str:
    """Attach a user's answer to the original cron report/question."""
    return (
        "[CRON FOLLOW-UP RESPONSE]\n"
        "The user is answering the interactive scheduled report below. "
        "Use the report as the question's context and respond to the user.\n\n"
        f"Original cron message/question:\nTASK: {context.title}\n\n"
        f"{context.result_text}\n\n"
        f"User response:\n{answer}\n"
        "[END CRON FOLLOW-UP RESPONSE]"
    )


class CronFollowupStore:
    """Atomically persist one expiring interactive cron context per key."""

    _VERSION = 1
    _DEFAULT_TTL_SECONDS = 48 * 60 * 60

    def __init__(
        self,
        path: Path,
        *,
        ttl_seconds: float = _DEFAULT_TTL_SECONDS,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._path = path
        self._ttl_seconds = max(float(ttl_seconds), 1.0)
        self._clock = clock or time.time
        self._lock = asyncio.Lock()

    async def record(self, envelope: Envelope) -> None:
        """Record an interactive unicast cron envelope after delivery."""
        now = self._clock()
        context = CronFollowupContext(
            title=str(envelope.metadata.get("title", "Cron report")),
            result_text=envelope.result_text,
            status=envelope.status,
            transport=canonical_transport(envelope.transport),
            chat_id=envelope.chat_id,
            topic_id=envelope.topic_id,
            created_at=now,
            expires_at=now + self._ttl_seconds,
            envelope_id=envelope.envelope_id,
        )
        async with self._lock:
            entries = await asyncio.to_thread(self._load_entries)
            self._prune(entries, now)
            entries[context.storage_key] = context.to_dict()
            await asyncio.to_thread(self._save_entries, entries)

    async def consume(self, key: SessionKey) -> CronFollowupContext | None:
        """Consume the matching context once, pruning expired entries."""
        now = self._clock()
        storage_key = _key_for(key.transport, key.chat_id, key.topic_id)
        async with self._lock:
            entries = await asyncio.to_thread(self._load_entries)
            changed = self._prune(entries, now)
            raw = entries.pop(storage_key, None)
            changed = raw is not None or changed
            if changed:
                await asyncio.to_thread(self._save_entries, entries)
            if not isinstance(raw, Mapping):
                return None
            context = CronFollowupContext.from_dict(raw)
            if context is None or context.expires_at <= now:
                return None
            return context

    def _load_entries(self) -> dict[str, dict[str, object]]:
        """Load only well-shaped entries from the JSON file."""
        data = load_json(self._path)
        if not isinstance(data, dict):
            return {}
        raw_entries = data.get("entries")
        if not isinstance(raw_entries, dict):
            return {}
        return {
            str(key): dict(value)
            for key, value in raw_entries.items()
            if isinstance(key, str) and isinstance(value, Mapping)
        }

    def _save_entries(self, entries: dict[str, dict[str, object]]) -> None:
        """Write the complete store atomically."""
        atomic_json_save(
            self._path,
            {"version": self._VERSION, "entries": entries},
        )

    @staticmethod
    def _prune(entries: dict[str, dict[str, object]], now: float) -> bool:
        """Drop malformed and expired entries; return whether anything changed."""
        changed = False
        for key, raw in list(entries.items()):
            context = CronFollowupContext.from_dict(raw)
            if context is None or context.expires_at <= now:
                del entries[key]
                changed = True
        return changed
