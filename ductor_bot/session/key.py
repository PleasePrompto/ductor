"""Transport-agnostic composite session key."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class SessionKey:
    """Composite session identifier: transport + chat + optional topic/channel.

    ``transport`` identifies the messaging backend (``"tg"`` for Telegram,
    ``"mx"`` for Matrix, ``"api"`` for the WebSocket API, etc.).

    For Telegram forum topics, ``topic_id`` is ``message_thread_id``.
    For the WebSocket API, ``topic_id`` maps to ``channel_id``.
    When ``topic_id`` is ``None``, this is a flat (legacy) session key.
    """

    transport: str = "tg"
    chat_id: str = ""
    topic_id: str | None = None

    @property
    def storage_key(self) -> str:
        """JSON-serializable key for ``sessions.json`` persistence."""
        if self.topic_id is None:
            return f"{self.transport}:{self.chat_id}"
        return f"{self.transport}:{self.chat_id}:{self.topic_id}"

    @property
    def lock_key(self) -> tuple[str, str | None]:
        """Hashable key for per-session lock dictionaries."""
        return (self.chat_id, self.topic_id)

    @classmethod
    def for_transport(cls, transport: str, chat_id: str, topic_id: str | None = None) -> SessionKey:
        """Create a session key for the given transport."""
        return cls(transport=transport, chat_id=chat_id, topic_id=topic_id)

    @classmethod
    def telegram(cls, chat_id: str, topic_id: str | None = None) -> SessionKey:
        """Create a Telegram session key."""
        return cls(transport="tg", chat_id=chat_id, topic_id=topic_id)

    @classmethod
    def matrix(cls, room_id: str) -> SessionKey:
        """Create a Matrix session key from a room ID."""
        return cls(transport="mx", chat_id=room_id)

    @classmethod
    def parse(cls, raw: str) -> SessionKey:
        """Parse a storage key back to ``SessionKey``.

        Handles legacy unprefixed formats (``"12345"``, ``"12345:99"``)
        and new transport-prefixed formats (``"tg:12345"``,
        ``"tg:12345:99"``).  Matrix room IDs (``"mx:!room:server"``)
        contain embedded colons and are handled specially.
        """
        parts = raw.split(":")
        if len(parts) == 1:
            # Legacy: "12345" -> transport="tg"
            return cls(transport="tg", chat_id=parts[0])
        if len(parts) == 2:
            if parts[0].lstrip("-").isdigit():
                # Legacy: "12345:99" -> transport="tg", topic
                return cls(transport="tg", chat_id=parts[0], topic_id=parts[1])
            # New: "tg:12345" -> no topic
            return cls(transport=parts[0], chat_id=parts[1])
        # 3+ parts: "tg:12345:99" or "mx:!room:server[:topic]"
        transport = parts[0]
        rest = raw[len(transport) + 1 :]
        chat_id, topic_id = _split_chat_topic(rest, parts)
        return cls(transport=transport, chat_id=chat_id, topic_id=topic_id)


def _split_chat_topic(rest: str, parts: list[str]) -> tuple[str, str | None]:
    """Split the post-transport portion into (chat_id, topic_id | None).

    Matrix room IDs embed colons (``!localpart:server``), so a simple
    split doesn't work.  This helper detects Matrix IDs by the leading
    ``!`` and accounts for the single embedded colon.
    """
    if rest.startswith("!"):
        # Matrix room ID: "!localpart:server" has exactly one colon.
        # With topic: "!localpart:server:topic" has two.
        inner = rest.split(":")
        if len(inner) <= 2:
            return rest, None
        if len(inner) == 3:
            return ":".join(inner[:2]), inner[2]
        # More colons than expected — treat entire rest as chat_id.
        return rest, None
    # Non-Matrix: simple "chat_id" or "chat_id:topic_id".
    if len(parts) == 3:
        return parts[1], parts[2]
    # Unexpected extra colons — best-effort.
    return ":".join(parts[1:]), None
