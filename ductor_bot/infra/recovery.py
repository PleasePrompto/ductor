"""Recovery planner: analyze startup state and plan safe recovery actions.

Inspects inflight foreground turns and named background sessions to
determine which interrupted work can be safely resumed after a restart.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from ductor_bot.infra.inflight import InflightTracker
from ductor_bot.session.named import NamedSession

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class RecoveryAction:
    """A single recovery action to execute on startup."""

    chat_id: int
    kind: str  # "foreground" | "named_session"
    provider: str
    model: str
    session_id: str
    prompt_preview: str
    session_name: str


class RecoveryPlanner:
    """Analyze startup state and plan recovery actions.

    Safety rules:
    - Max 1 foreground recovery per chat_id
    - Skip ``is_recovery=True`` entries (prevents infinite loops)
    - Skip entries older than ``max_age_seconds``
    - Skip inter-agent sessions (``ia-`` or ``ia.`` prefix)
    - Skip named sessions without a session_id (never started)
    - Skip named sessions that are not in "idle" status
    - Use ``--resume`` via session_id when available
    """

    def __init__(
        self,
        inflight: InflightTracker,
        named_sessions: list[NamedSession],
        max_age_seconds: float,
    ) -> None:
        self._inflight = inflight
        self._named_sessions = named_sessions
        self._max_age = max_age_seconds

    def plan(self) -> list[RecoveryAction]:
        """Return list of safe recovery actions."""
        actions: list[RecoveryAction] = []
        actions.extend(self._plan_foreground())
        actions.extend(self._plan_named_sessions())
        return actions

    def _plan_foreground(self) -> list[RecoveryAction]:
        """Plan foreground turn recovery from inflight tracker."""
        interrupted = self._inflight.load_interrupted(max_age_seconds=self._max_age)
        actions: list[RecoveryAction] = []
        seen_chats: set[int] = set()
        for turn in interrupted:
            if turn.chat_id in seen_chats:
                continue
            seen_chats.add(turn.chat_id)
            actions.append(
                RecoveryAction(
                    chat_id=turn.chat_id,
                    kind="foreground",
                    provider=turn.provider,
                    model=turn.model,
                    session_id=turn.session_id,
                    prompt_preview=turn.prompt_preview,
                    session_name="",
                )
            )
        return actions

    def _plan_named_sessions(self) -> list[RecoveryAction]:
        """Plan named session recovery from session registry."""
        actions: list[RecoveryAction] = []
        for ns in self._named_sessions:
            if ns.name.startswith(("ia-", "ia.")):
                continue
            if ns.status != "idle":
                continue
            if not ns.session_id:
                continue
            actions.append(
                RecoveryAction(
                    chat_id=ns.chat_id,
                    kind="named_session",
                    provider=ns.provider,
                    model=ns.model,
                    session_id=ns.session_id,
                    prompt_preview=ns.last_prompt or ns.prompt_preview,
                    session_name=ns.name,
                )
            )
        return actions
