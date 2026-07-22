"""Named background sessions with follow-up support and JSON persistence."""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import secrets
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from ductor_bot.infra.json_store import atomic_json_save, load_json
from ductor_bot.session.key import SessionKey

logger = logging.getLogger(__name__)


def interagent_session_name(
    sender: str,
    source_chat_id: int = 0,
    source_topic_id: int | None = None,
) -> str:
    """Return the legacy or chat/topic-scoped inter-agent session name."""
    if not source_chat_id:
        return f"ia-{sender}"
    slug = re.sub(r"[^a-z0-9]+", "-", sender.lower()).strip("-")[:12] or "agent"
    topic = f"t{source_topic_id}" if source_topic_id is not None else "t0"
    digest = hashlib.sha256(f"{sender}\0{source_chat_id}\0{source_topic_id}".encode()).hexdigest()[
        :8
    ]
    return f"{f'ia.{slug}.{topic}'[:30]}.x{digest}"


_ADJECTIVES: tuple[str, ...] = (
    "bold",
    "blue",
    "calm",
    "cool",
    "dark",
    "deep",
    "fast",
    "firm",
    "glad",
    "gold",
    "keen",
    "kind",
    "late",
    "lean",
    "live",
    "long",
    "loud",
    "mint",
    "neat",
    "next",
    "pale",
    "pure",
    "rare",
    "real",
    "red",
    "rich",
    "safe",
    "slim",
    "soft",
    "tall",
    "tidy",
    "tiny",
    "true",
    "vast",
    "warm",
    "wild",
    "wise",
    "wry",
    "zen",
    "zinc",
)

_NOUNS: tuple[str, ...] = (
    "ant",
    "ape",
    "bat",
    "bay",
    "bee",
    "cat",
    "cod",
    "cow",
    "cub",
    "doe",
    "eel",
    "elk",
    "elm",
    "emu",
    "fin",
    "fly",
    "fox",
    "gem",
    "gnu",
    "gull",
    "hare",
    "hawk",
    "ibis",
    "jay",
    "koi",
    "lark",
    "lynx",
    "mole",
    "moth",
    "newt",
    "oak",
    "orb",
    "orca",
    "owl",
    "paw",
    "pike",
    "puma",
    "ray",
    "seal",
    "star",
    "swan",
    "toad",
    "vole",
    "wasp",
    "wolf",
    "wren",
    "yak",
)

MAX_SESSIONS_PER_CHAT = 10
# ponytail: flat per-chat cap for inter-agent sessions; per-sender quotas if it ever matters
MAX_INTERAGENT_SESSIONS_PER_CHAT = 32

_MAX_NAME_ATTEMPTS = 50


def generate_name(existing: set[str]) -> str:
    """Generate a unique compact name (e.g. 'redowl') not in *existing*."""
    for _ in range(_MAX_NAME_ATTEMPTS):
        name = f"{secrets.choice(_ADJECTIVES)}{secrets.choice(_NOUNS)}"
        if name not in existing:
            return name
    # Fallback: append digit
    base = f"{secrets.choice(_ADJECTIVES)}{secrets.choice(_NOUNS)}"
    for i in range(2, 100):
        candidate = f"{base}{i}"
        if candidate not in existing:
            return candidate
    msg = "Could not generate unique session name"
    raise RuntimeError(msg)


@dataclass(slots=True)
class NamedSession:
    """State for a named background session."""

    name: str
    chat_id: int
    provider: str
    model: str
    session_id: str
    prompt_preview: str
    status: str  # "running" | "idle" | "ended"
    created_at: float
    message_count: int = 0
    last_prompt: str = ""
    transport: str = "tg"
    reasoning_effort: str = ""
    topic_id: int | None = None


def _session_from_dict(data: dict[str, Any]) -> NamedSession:
    """Reconstruct a NamedSession from a JSON-serialized dict."""
    return NamedSession(
        name=str(data.get("name", "")),
        chat_id=int(data.get("chat_id", 0)),
        topic_id=int(data["topic_id"]) if data.get("topic_id") is not None else None,
        provider=str(data.get("provider", "")),
        model=str(data.get("model", "")),
        session_id=str(data.get("session_id", "")),
        prompt_preview=str(data.get("prompt_preview", "")),
        status=str(data.get("status", "ended")),
        created_at=float(data.get("created_at", 0.0)),
        message_count=int(data.get("message_count", 0)),
        last_prompt=str(data.get("last_prompt", data.get("prompt_preview", ""))),
        transport=str(data.get("transport", "tg")),
        reasoning_effort=str(data.get("reasoning_effort", "")),
    )


class NamedSessionRegistry:
    """Registry of named sessions with JSON persistence.

    Sessions survive bot restarts. On startup, sessions with status
    ``"running"`` are downgraded to ``"idle"`` (the CLI process is gone
    but the CLI session ID may still be valid for ``--resume``).
    """

    def __init__(self, path: Path) -> None:
        self._path = path
        self._lock = asyncio.Lock()
        self._sessions: dict[tuple[int, str], NamedSession] = {}
        self._recovered_running: dict[tuple[int, str], NamedSession] = {}
        self._load()

    def _load(self) -> None:
        """Load sessions from JSON on disk."""
        raw = load_json(self._path)
        if not raw:
            return
        entries: list[dict[str, Any]] = raw.get("sessions", [])
        for entry in entries:
            ns = _session_from_dict(entry)
            if ns.status == "ended" or not ns.name:
                continue
            # Downgrade stale "running" to "idle" after restart
            if ns.status == "running":
                self._recovered_running[(ns.chat_id, ns.name)] = NamedSession(
                    name=ns.name,
                    chat_id=ns.chat_id,
                    topic_id=ns.topic_id,
                    provider=ns.provider,
                    model=ns.model,
                    session_id=ns.session_id,
                    prompt_preview=ns.prompt_preview,
                    status="idle",
                    created_at=ns.created_at,
                    message_count=ns.message_count,
                    last_prompt=ns.last_prompt,
                    transport=ns.transport,
                    reasoning_effort=ns.reasoning_effort,
                )
                ns.status = "idle"
            self._sessions[(ns.chat_id, ns.name)] = ns
        logger.info("Loaded %d named sessions from %s", len(self._sessions), self._path)

    def _persist(self) -> None:
        """Write all non-ended sessions to JSON."""
        entries = [asdict(ns) for ns in self._sessions.values() if ns.status != "ended"]
        atomic_json_save(self._path, {"sessions": entries})

    def create(  # noqa: PLR0913  -- session identity + provider/model/effort are all intrinsic
        self,
        chat_id: int,
        provider: str,
        model: str,
        prompt_preview: str,
        reasoning_effort: str = "",
        key: SessionKey | None = None,
    ) -> NamedSession:
        """Create a new named session. Raises ValueError if limit exceeded."""
        active = self.active_names(chat_id)
        if len(active) >= MAX_SESSIONS_PER_CHAT:
            msg = f"Too many sessions ({MAX_SESSIONS_PER_CHAT} max)"
            raise ValueError(msg)

        name = generate_name(active)
        session_key = key or SessionKey.telegram(chat_id)
        session = NamedSession(
            name=name,
            chat_id=session_key.chat_id,
            provider=provider,
            model=model,
            session_id="",
            prompt_preview=prompt_preview[:60],
            status="running",
            created_at=time.time(),
            reasoning_effort=reasoning_effort,
            transport=session_key.transport,
            topic_id=session_key.topic_id,
        )
        self._sessions[(chat_id, name)] = session
        self._persist()
        logger.info(
            "Named session created name=%s chat=%d provider=%s",
            name,
            chat_id,
            provider,
        )
        return session

    def get(self, chat_id: int, name: str) -> NamedSession | None:
        """Look up a named session (any status)."""
        return self._sessions.get((chat_id, name))

    def list_active(self, chat_id: int) -> list[NamedSession]:
        """Return all non-ended sessions for *chat_id*, ordered by creation."""
        return sorted(
            (s for s in self._sessions.values() if s.chat_id == chat_id and s.status != "ended"),
            key=lambda s: s.created_at,
        )

    def end_session(self, chat_id: int, name: str) -> bool:
        """Mark a session as ended. Returns True if found and ended."""
        ns = self._sessions.get((chat_id, name))
        if ns is None or ns.status == "ended":
            return False
        ns.status = "ended"
        self._persist()
        logger.info("Named session ended name=%s chat=%d", name, chat_id)
        return True

    def end_all(self, chat_id: int) -> int:
        """End all active sessions for *chat_id*. Returns count ended."""
        count = 0
        for ns in self._sessions.values():
            if ns.chat_id == chat_id and ns.status != "ended":
                ns.status = "ended"
                count += 1
        if count:
            self._persist()
            logger.info("All named sessions ended chat=%d count=%d", chat_id, count)
        return count

    def update_after_response(
        self,
        chat_id: int,
        name: str,
        session_id: str,
        *,
        status: str = "idle",
    ) -> None:
        """Update session state after a CLI response."""
        ns = self._sessions.get((chat_id, name))
        if ns is None:
            return
        if session_id:
            ns.session_id = session_id
        ns.message_count += 1
        ns.status = status
        self._persist()

    def add(self, session: NamedSession) -> None:
        """Add a pre-built session to the registry and persist.

        Use this when the caller needs full control over the session name
        and fields (e.g. inter-agent sessions with deterministic names).
        Inter-agent sessions bypass the ``create()`` quota, so they get their
        own cap here: scoped names grow with every distinct (sender, chat,
        topic) origin and are never ended, which would otherwise leak memory
        and rewrite an ever-growing JSON on every add.
        """
        if session.name.startswith(("ia-", "ia.")):
            self._evict_oldest_interagent(session.chat_id)
        self._sessions[(session.chat_id, session.name)] = session
        self._persist()

    def _evict_oldest_interagent(self, chat_id: int) -> None:
        """Drop oldest non-running inter-agent sessions above the per-chat cap."""
        idle = sorted(
            (
                s
                for s in self._sessions.values()
                if s.chat_id == chat_id
                and s.name.startswith(("ia-", "ia."))
                and s.status != "running"
            ),
            key=lambda s: s.created_at,
        )
        overflow = len(idle) - (MAX_INTERAGENT_SESSIONS_PER_CHAT - 1)
        if overflow <= 0:
            return
        for victim in idle[:overflow]:
            del self._sessions[(victim.chat_id, victim.name)]
            logger.info("Evicted idle inter-agent session %s (cap)", victim.name)

    def mark_running(
        self,
        chat_id: int,
        name: str,
        prompt: str,
        *,
        transport: str | None = None,
        topic_id: int | None = None,
    ) -> None:
        """Mark a session as running and store the prompt for recovery."""
        ns = self._sessions.get((chat_id, name))
        if ns is None:
            return
        ns.status = "running"
        ns.last_prompt = prompt[:4000]
        if transport:
            ns.transport = transport
        if topic_id is not None:
            ns.topic_id = topic_id
        self._persist()

    def pop_recovered_running(
        self, chat_id: int | None = None, *, transport: str | None = None
    ) -> list[NamedSession]:
        """Return sessions that were running at last shutdown, then clear them.

        If *chat_id* is given, only return sessions for that chat.
        If *transport* is given, only return sessions for that transport.
        Excludes inter-agent sessions (``ia-`` or ``ia.`` prefix).
        """
        results: list[NamedSession] = []
        to_remove: list[tuple[int, str]] = []
        for key, ns in self._recovered_running.items():
            if chat_id is not None and ns.chat_id != chat_id:
                continue
            if transport is not None and ns.transport != transport:
                continue
            if ns.name.startswith(("ia-", "ia.")):
                continue
            results.append(ns)
            to_remove.append(key)
        for key in to_remove:
            del self._recovered_running[key]
        return sorted(results, key=lambda s: s.created_at)

    def active_names(self, chat_id: int) -> set[str]:
        """Return active user session names for the quota and name generation.

        Inter-agent sessions (``ia-``/``ia.`` namespace) are excluded: they are
        capped separately in :meth:`add` and must never consume the user's
        ``/session`` quota. Generated animal names cannot collide with the
        ``ia`` namespace, so excluding them here is collision-safe.
        """
        return {
            s.name
            for s in self._sessions.values()
            if s.chat_id == chat_id
            and s.status != "ended"
            and not s.name.startswith(("ia-", "ia."))
        }
