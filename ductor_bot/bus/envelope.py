"""Unified message envelope for all delivery paths."""

from __future__ import annotations

import enum
import time
from dataclasses import dataclass, field
from typing import Any


class Origin(enum.Enum):
    """Where the message result came from."""

    BACKGROUND = "background"
    CRON = "cron"
    WEBHOOK_WAKE = "webhook_wake"
    WEBHOOK_CRON = "webhook_cron"
    HEARTBEAT = "heartbeat"
    INTERAGENT = "interagent"
    TASK_RESULT = "task_result"
    TASK_QUESTION = "task_question"
    USER = "user"
    API = "api"


class DeliveryMode(enum.Enum):
    """How the result should be delivered."""

    UNICAST = "unicast"
    BROADCAST = "broadcast"


class LockMode(enum.Enum):
    """Lock acquisition strategy before delivery."""

    REQUIRED = "required"
    NONE = "none"


@dataclass(slots=True)
class Envelope:
    """Unified container for all message routing.

    Every background result, cron output, webhook response, heartbeat alert,
    inter-agent message, and task result is wrapped in an Envelope before
    being submitted to the :class:`MessageBus`.
    """

    # -- Identity --
    origin: Origin
    chat_id: int
    topic_id: int | None = None
    transport: str = "tg"

    # -- Input (for injection into active session) --
    prompt: str = ""
    prompt_preview: str = ""

    # -- Result (what to deliver) --
    result_text: str = ""
    status: str = ""
    is_error: bool = False

    # -- Delivery configuration --
    delivery: DeliveryMode = DeliveryMode.UNICAST
    lock_mode: LockMode = LockMode.NONE
    needs_injection: bool = False

    # -- Delivery acknowledgement (#160) --
    # Transports flip this to False when they swallow a send failure so the
    # submitter can persist/surface the miss. Best-effort: only the unicast
    # cron path currently reports it; broadcast stays untracked.
    delivered: bool = True
    delivery_error: str = ""

    # -- Origin-specific metadata --
    metadata: dict[str, Any] = field(default_factory=dict)

    # -- Telegram-specific --
    reply_to_message_id: int | None = None
    thread_id: int | None = None

    # -- Tracking --
    envelope_id: str = ""
    created_at: float = field(default_factory=time.time)
    elapsed_seconds: float = 0.0

    # -- Provider context --
    provider: str = ""
    model: str = ""
    session_name: str = ""
    session_id: str = ""

    @property
    def lock_key(self) -> tuple[int, int | None]:
        """Key for per-session lock acquisition."""
        return (self.chat_id, self.topic_id)
