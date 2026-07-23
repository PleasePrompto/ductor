"""Session lifecycle: creation, freshness checks, reset. JSON-based persistence."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ductor_bot.config import AgentConfig, resolve_user_timezone
from ductor_bot.infra.json_store import atomic_json_save, load_json
from ductor_bot.session.key import SessionKey

logger = logging.getLogger(__name__)


def _as_mapping(value: object) -> Mapping[str, object] | None:
    """Return value as string-key mapping when possible."""
    if isinstance(value, Mapping):
        return value
    return None


def _as_str(value: object, *, default: str) -> str:
    """Return value as string (or default for ``None``)."""
    if value is None:
        return default
    if isinstance(value, str):
        return value
    return str(value)


def _as_optional_str(value: object) -> str | None:
    """Return optional value as string."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    return str(value)


def _as_optional_int(value: object) -> int | None:
    """Return optional value coerced to int (best effort)."""
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if not isinstance(value, str):
        return None
    try:
        return int(value)
    except ValueError:
        return None


def _as_optional_float(value: object) -> float | None:
    """Return optional value coerced to float (best effort)."""
    if value is None:
        return None
    if isinstance(value, float):
        return value
    if isinstance(value, int):
        return float(value)
    if not isinstance(value, str):
        return None
    try:
        return float(value)
    except ValueError:
        return None


@dataclass
class ProviderSessionData:
    """Provider-local session state."""

    session_id: str = ""
    message_count: int = 0
    total_cost_usd: float = 0.0
    total_tokens: int = 0


@dataclass(init=False)
class SessionData:
    """Active session state with provider-isolated IDs and metrics."""

    transport: str
    chat_id: int
    topic_id: int | None
    topic_name: str | None
    provider: str
    model: str
    reasoning_effort: str
    created_at: str
    last_active: str
    provider_sessions: dict[str, ProviderSessionData] = field(default_factory=dict)

    def __init__(self, chat_id: int, **raw: object) -> None:
        """Create session data from current or legacy serialized fields."""
        transport = _as_str(raw.pop("transport", "tg"), default="tg")
        topic_id = _as_optional_int(raw.pop("topic_id", None))
        topic_name = _as_optional_str(raw.pop("topic_name", None))
        provider = _as_str(raw.pop("provider", "claude"), default="claude")
        model = _as_str(raw.pop("model", "opus"), default="opus")
        reasoning_effort = _as_str(raw.pop("reasoning_effort", ""), default="")
        created_at = _as_str(raw.pop("created_at", ""), default="")
        last_active = _as_str(raw.pop("last_active", ""), default="")
        provider_sessions = _as_mapping(raw.pop("provider_sessions", None))

        # Backward compatibility for old JSON/tests.
        session_id = _as_optional_str(raw.pop("session_id", None))
        message_count = _as_optional_int(raw.pop("message_count", None))
        total_cost_usd = _as_optional_float(raw.pop("total_cost_usd", None))
        total_tokens = _as_optional_int(raw.pop("total_tokens", None))

        self.transport = transport
        self.chat_id = chat_id
        self.topic_id = topic_id
        self.topic_name = topic_name
        self.provider = provider
        self.model = model
        self.reasoning_effort = reasoning_effort

        now = datetime.now(UTC).isoformat()
        self.created_at = created_at or now
        self.last_active = last_active or now

        migrated = self._coerce_provider_sessions(provider_sessions)
        has_legacy_fields = any(
            value is not None for value in (session_id, message_count, total_cost_usd, total_tokens)
        )
        if provider_sessions is None and has_legacy_fields:
            migrated[self.provider] = ProviderSessionData(
                session_id=session_id or "",
                message_count=message_count or 0,
                total_cost_usd=total_cost_usd or 0.0,
                total_tokens=total_tokens or 0,
            )
        self.provider_sessions = migrated

        if raw:
            logger.warning("SessionData: unknown keys ignored: %s", list(raw.keys()))

    @property
    def session_key(self) -> SessionKey:
        """Composite key for this session."""
        return SessionKey(
            transport=self.transport,
            chat_id=self.chat_id,
            topic_id=self.topic_id,
        )

    @property
    def session_id(self) -> str:
        """Session ID for the currently active provider."""
        current = self.provider_sessions.get(self.provider)
        return current.session_id if current is not None else ""

    @session_id.setter
    def session_id(self, value: str) -> None:
        self._current_provider_data().session_id = value

    @property
    def message_count(self) -> int:
        """Message count for the currently active provider."""
        current = self.provider_sessions.get(self.provider)
        return current.message_count if current is not None else 0

    @message_count.setter
    def message_count(self, value: int) -> None:
        self._current_provider_data().message_count = value

    @property
    def total_cost_usd(self) -> float:
        """Total cost for the currently active provider."""
        current = self.provider_sessions.get(self.provider)
        return current.total_cost_usd if current is not None else 0.0

    @total_cost_usd.setter
    def total_cost_usd(self, value: float) -> None:
        self._current_provider_data().total_cost_usd = value

    @property
    def total_tokens(self) -> int:
        """Total token usage for the currently active provider."""
        current = self.provider_sessions.get(self.provider)
        return current.total_tokens if current is not None else 0

    @total_tokens.setter
    def total_tokens(self, value: int) -> None:
        self._current_provider_data().total_tokens = value

    def _current_provider_data(self) -> ProviderSessionData:
        """Get/create provider-local state for the active provider."""
        current = self.provider_sessions.get(self.provider)
        if current is None:
            current = ProviderSessionData()
            self.provider_sessions[self.provider] = current
        return current

    def clear_provider_session(self, provider: str) -> None:
        """Drop one provider-local session and metrics."""
        self.provider_sessions.pop(provider, None)

    @staticmethod
    def _coerce_provider_sessions(
        raw: Mapping[str, object] | None,
    ) -> dict[str, ProviderSessionData]:
        """Normalize serialized provider state to dataclass instances."""
        if not raw:
            return {}
        out: dict[str, ProviderSessionData] = {}
        for provider, value in raw.items():
            if isinstance(value, ProviderSessionData):
                out[provider] = value
                continue
            if not isinstance(value, dict):
                continue
            out[provider] = ProviderSessionData(
                session_id=str(value.get("session_id", "") or ""),
                message_count=SessionData._safe_int(value.get("message_count", 0)),
                total_cost_usd=SessionData._safe_float(value.get("total_cost_usd", 0.0)),
                total_tokens=SessionData._safe_int(value.get("total_tokens", 0)),
            )
        return out

    @staticmethod
    def _safe_int(value: object) -> int:
        """Best-effort integer conversion for legacy/corrupt payloads."""
        if isinstance(value, bool):
            return int(value)
        candidate: str | int | float = value if isinstance(value, (int, float, str)) else str(value)
        try:
            return int(candidate)
        except (TypeError, ValueError):
            return 0

    @staticmethod
    def _safe_float(value: object) -> float:
        """Best-effort float conversion for legacy/corrupt payloads."""
        if isinstance(value, bool):
            return float(value)
        if isinstance(value, (int, float)):
            return float(value)
        if isinstance(value, str):
            try:
                return float(value)
            except ValueError:
                return 0.0
        try:
            return float(str(value))
        except (TypeError, ValueError):
            return 0.0


TopicNameResolver = Callable[[int, int], str]
"""Callback: (chat_id, topic_id) → human-readable topic name."""


class SessionManager:
    """Manages session lifecycle with JSON file persistence."""

    def __init__(self, sessions_path: Path, config: AgentConfig) -> None:
        self._path = sessions_path
        self._config = config
        self._lock = asyncio.Lock()
        self._topic_name_resolver: TopicNameResolver | None = None

    def set_topic_name_resolver(self, resolver: TopicNameResolver) -> None:
        """Register a callback that resolves ``(chat_id, topic_id)`` to a name."""
        self._topic_name_resolver = resolver

    def resolve_topic_name(self, chat_id: int, topic_id: int | None) -> str | None:
        """Resolve a topic id to its human-readable name via the registered resolver.

        Returns ``None`` when *topic_id* is ``None`` or no resolver is set.
        """
        if topic_id is None or self._topic_name_resolver is None:
            return None
        return self._topic_name_resolver(chat_id, topic_id)

    def _apply_topic_name(self, session: SessionData) -> bool:
        """Fill ``topic_name`` from the resolver when missing. Returns True if changed."""
        if session.topic_id is None or self._topic_name_resolver is None:
            return False
        if session.topic_name:
            return False
        session.topic_name = self._topic_name_resolver(session.chat_id, session.topic_id)
        return True

    async def _preserve_existing_session(
        self,
        existing: SessionData,
        sessions: dict[str, SessionData],
        preserve_existing_target: bool,
        effort: str,
    ) -> tuple[SessionData, bool] | None:
        """Preserve the existing model/provider target, capturing effort on first use.

        Returns (session, is_new) when preserved, else None to fall through to
        the normal resolve/apply path.
        """
        if not (
            preserve_existing_target
            and bool(existing.provider.strip())
            and bool(existing.model.strip())
        ):
            return None
        save = self._apply_topic_name(existing)
        if not existing.reasoning_effort.strip():
            existing.reasoning_effort = effort
            save = True
        if save:
            await self._save(sessions)
        return existing, not bool(existing.session_id)

    async def resolve_session(
        self,
        key: SessionKey,
        *,
        provider: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
        preserve_existing_target: bool = False,
    ) -> tuple[SessionData, bool]:
        """Returns (session, is_new). Reuses if fresh, creates if stale."""
        sessions = await self._load()
        skey = key.storage_key
        existing = sessions.get(skey)

        prov = provider or self._config.provider
        model_name = model or self._config.model
        effort = reasoning_effort or self._config.reasoning_effort

        if existing and self._is_fresh(existing):
            preserved = await self._preserve_existing_session(
                existing, sessions, preserve_existing_target, effort
            )
            if preserved is not None:
                return preserved
            changed = False
            if existing.provider != prov:
                logger.info("Provider switch %s -> %s", existing.provider, prov)
                existing.provider = prov
                changed = True
            if existing.model != model_name:
                existing.model = model_name
                changed = True
            if existing.reasoning_effort != effort:
                existing.reasoning_effort = effort
                changed = True
            if self._apply_topic_name(existing):
                changed = True
            if changed:
                await self._save(sessions)
            return existing, not bool(existing.session_id)

        topic_name: str | None = None
        if key.topic_id is not None and self._topic_name_resolver is not None:
            topic_name = self._topic_name_resolver(key.chat_id, key.topic_id)

        new = SessionData(
            chat_id=key.chat_id,
            transport=key.transport,
            topic_id=key.topic_id,
            topic_name=topic_name,
            provider=prov,
            model=model_name,
            reasoning_effort=effort,
            provider_sessions={},
        )
        sessions[skey] = new
        await self._save(sessions)
        logger.info("Session created provider=%s model=%s", prov, model_name)
        return new, True

    async def get_active(self, key: SessionKey) -> SessionData | None:
        """Return the current session for *key* without creating one."""
        sessions = await self._load()
        return sessions.get(key.storage_key)

    async def resolve_session_target(
        self,
        key: SessionKey,
        *,
        provider: str,
        model: str,
        reasoning_effort: str | None = None,
    ) -> tuple[SessionData, bool]:
        """Atomically record the model/effort target for *key*'s next session.

        Persists the picked ``provider``/``model``/``reasoning_effort`` onto the
        session the next message will resolve, so a ``/model`` change in a topic
        survives even when the topic has no session yet or a stale one. Runs
        under ``self._lock``. Returns ``(session, created)`` where ``created``
        is ``True`` when the target provider has no resumable history.

        Staleness is per-provider: a session that is only stale because the
        *target* provider's bucket hit the message cap keeps its other buckets
        (only the target bucket is reset). Session-wide staleness (idle timeout
        or daily reset) replaces the whole shell, matching ``resolve_session``.
        """
        async with self._lock:
            sessions = await self._load()
            skey = key.storage_key
            existing = sessions.get(skey)

            if existing is not None and self._time_fresh(existing):
                target_bucket = existing.provider_sessions.get(provider)
                bucket_count = target_bucket.message_count if target_bucket is not None else 0
                if self._within_message_limit(bucket_count):
                    # Fresh for the target provider: retarget in place and keep
                    # every provider's bucket history intact.
                    if self._retarget_session(existing, provider, model, reasoning_effort):
                        await self._save(sessions)
                    return existing, False
                # Otherwise fresh, but the target bucket hit the message cap.
                # Reset only that bucket so switching back to another provider
                # keeps its history, then retarget onto the fresh bucket.
                existing.provider_sessions.pop(provider, None)
                self._retarget_session(existing, provider, model, reasoning_effort)
                await self._save(sessions)
                logger.info("Target bucket reset provider=%s model=%s", provider, model)
                return existing, True

            topic_name: str | None = None
            if key.topic_id is not None and self._topic_name_resolver is not None:
                topic_name = self._topic_name_resolver(key.chat_id, key.topic_id)

            new = SessionData(
                chat_id=key.chat_id,
                transport=key.transport,
                topic_id=key.topic_id,
                topic_name=topic_name,
                provider=provider,
                model=model,
                reasoning_effort=reasoning_effort or "",
                provider_sessions={},
            )
            sessions[skey] = new
            await self._save(sessions)
            logger.info("Session created provider=%s model=%s", provider, model)
            return new, True

    def _retarget_session(
        self,
        session: SessionData,
        provider: str,
        model: str,
        reasoning_effort: str | None,
    ) -> bool:
        """Point *session* at provider/model/effort in place. Returns True if changed.

        ``reasoning_effort=None`` leaves the existing effort untouched (mirrors
        ``sync_session_target``); an explicit value overwrites it.
        """
        changed = False
        if session.provider != provider:
            logger.info("Provider switch %s -> %s", session.provider, provider)
            session.provider = provider
            changed = True
        if session.model != model:
            session.model = model
            changed = True
        if reasoning_effort is not None and session.reasoning_effort != reasoning_effort:
            session.reasoning_effort = reasoning_effort
            changed = True
        if self._apply_topic_name(session):
            changed = True
        return changed

    async def list_active_for_chat(self, chat_id: int) -> list[SessionData]:
        """Return all fresh sessions belonging to *chat_id*."""
        sessions = await self._load()
        return [s for s in sessions.values() if s.chat_id == chat_id and self._is_fresh(s)]

    async def list_all(self) -> list[SessionData]:
        """Return all persisted sessions (fresh or stale)."""
        sessions = await self._load()
        return list(sessions.values())

    async def reset_provider_session(
        self,
        key: SessionKey,
        provider: str,
        model: str,
    ) -> SessionData:
        """Reset only one provider-local session and keep all others intact."""
        sessions = await self._load()
        skey = key.storage_key
        current = sessions.get(skey)
        if current is None:
            current = SessionData(
                chat_id=key.chat_id,
                transport=key.transport,
                topic_id=key.topic_id,
                provider=provider,
                model=model,
                provider_sessions={},
            )
        else:
            current.clear_provider_session(provider)
            current.provider = provider
            current.model = model
            current.last_active = datetime.now(UTC).isoformat()
        sessions[skey] = current
        await self._save(sessions)
        logger.info("Provider session reset provider=%s model=%s", provider, model)
        return current

    async def update_session(
        self,
        session: SessionData,
        cost_usd: float = 0.0,
        tokens: int = 0,
    ) -> None:
        """Update session metrics and persist.

        Serialized via ``_lock`` to prevent lost-update races when concurrent
        callers (e.g. heartbeat + normal flow) update the same session.
        """
        async with self._lock:
            sessions = await self._load()
            key = session.session_key.storage_key
            current = sessions.get(key)
            if current is None:
                current = session
            else:
                # Apply mutable identity fields from caller, but keep counters
                # from the latest persisted record to avoid stale overwrites.
                self._merge_provider_sessions(current, session)
                current.provider = session.provider
                current.model = session.model
                if session.topic_name and not current.topic_name:
                    current.topic_name = session.topic_name

            current.last_active = datetime.now(UTC).isoformat()
            current.message_count += 1
            current.total_cost_usd += cost_usd
            current.total_tokens += tokens
            sessions[key] = current
            await self._save(sessions)

            # Keep caller reference in sync with persisted aggregate values.
            session.provider = current.provider
            session.model = current.model
            session.last_active = current.last_active
            session.provider_sessions = self._clone_provider_sessions(current.provider_sessions)
            session.message_count = current.message_count
            session.total_cost_usd = current.total_cost_usd
            session.total_tokens = current.total_tokens

    async def preserve_session_identity(self, session: SessionData) -> None:
        """Persist session IDs/provider metadata without counting a completed turn."""
        async with self._lock:
            sessions = await self._load()
            key = session.session_key.storage_key
            current = sessions.get(key)
            if current is None:
                current = session
            else:
                self._merge_provider_sessions(current, session)
                current.provider = session.provider
                current.model = session.model
                if session.topic_name and not current.topic_name:
                    current.topic_name = session.topic_name

            current.last_active = datetime.now(UTC).isoformat()
            sessions[key] = current
            await self._save(sessions)

            session.provider = current.provider
            session.model = current.model
            session.last_active = current.last_active
            session.provider_sessions = self._clone_provider_sessions(current.provider_sessions)

    @staticmethod
    def _clone_provider_sessions(
        provider_sessions: dict[str, ProviderSessionData],
    ) -> dict[str, ProviderSessionData]:
        """Deep-clone provider-local state to avoid shared mutable references."""
        return {
            provider: ProviderSessionData(
                session_id=data.session_id,
                message_count=data.message_count,
                total_cost_usd=data.total_cost_usd,
                total_tokens=data.total_tokens,
            )
            for provider, data in provider_sessions.items()
        }

    @staticmethod
    def _merge_provider_sessions(current: SessionData, incoming: SessionData) -> None:
        """Merge provider state while preventing stale snapshots from regressing counters."""
        for provider, data in incoming.provider_sessions.items():
            existing = current.provider_sessions.get(provider)
            if existing is None:
                current.provider_sessions[provider] = ProviderSessionData(
                    session_id=data.session_id,
                    message_count=data.message_count,
                    total_cost_usd=data.total_cost_usd,
                    total_tokens=data.total_tokens,
                )
                continue
            if data.session_id:
                existing.session_id = data.session_id
            existing.message_count = max(existing.message_count, data.message_count)
            existing.total_cost_usd = max(existing.total_cost_usd, data.total_cost_usd)
            existing.total_tokens = max(existing.total_tokens, data.total_tokens)

    async def sync_session_target(
        self,
        session: SessionData,
        *,
        provider: str | None = None,
        model: str | None = None,
        reasoning_effort: str | None = None,
    ) -> None:
        """Persist provider/model/effort changes without touching activity counters."""
        async with self._lock:
            sessions = await self._load()
            skey = session.session_key.storage_key
            current = sessions.get(skey)
            if current is None:
                return

            changed = False
            if provider is not None and current.provider != provider:
                current.provider = provider
                changed = True
            if model is not None and current.model != model:
                current.model = model
                changed = True
            if reasoning_effort is not None and current.reasoning_effort != reasoning_effort:
                current.reasoning_effort = reasoning_effort
                changed = True

            needs_model_migration = False
            if not changed:
                needs_model_migration = await asyncio.to_thread(
                    self._raw_entry_missing_model,
                    skey,
                )
            if not changed and not needs_model_migration:
                return

            sessions[skey] = current
            await self._save(sessions)

            # Keep caller reference aligned with persisted target.
            session.provider = current.provider
            session.model = current.model
            session.reasoning_effort = current.reasoning_effort

    def _raw_entry_missing_model(self, storage_key: str) -> bool:
        """Return True when raw session JSON exists but has no ``model`` key.

        Handles both new prefixed keys (``"tg:1"``) and legacy unprefixed
        keys (``"1"``) that may still be on disk before the first save
        migrates them.
        """
        if not self._path.exists():
            return False
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return False
        # Try the canonical key first, then fall back to legacy variants.
        entry = data.get(storage_key)
        if entry is None:
            for raw_key in data:
                if SessionKey.parse(raw_key).storage_key == storage_key:
                    entry = data[raw_key]
                    break
        return isinstance(entry, dict) and "model" not in entry

    def _within_message_limit(self, message_count: int) -> bool:
        """False when the message cap is configured and *message_count* reached it."""
        max_messages = self._config.max_session_messages
        if max_messages is not None and message_count >= max_messages:
            logger.debug("Session fresh check: fresh=no reason=max_messages")
            return False
        return True

    def _time_fresh(self, session: SessionData) -> bool:
        """Session-wide freshness: idle timeout and daily reset.

        Independent of any provider bucket's message count, so callers can
        distinguish a whole-session expiry (idle/daily) from a single provider
        bucket merely hitting the message cap.
        """
        now = datetime.now(UTC)
        try:
            last = datetime.fromisoformat(session.last_active)
        except (ValueError, TypeError):
            logger.warning("Corrupt session timestamp: %r, treating as stale", session.last_active)
            return False

        timeout = self._config.idle_timeout_minutes
        if timeout > 0 and (now - last).total_seconds() >= timeout * 60:
            logger.debug("Session fresh check: fresh=no reason=idle_timeout")
            return False

        if self._config.daily_reset_enabled:
            reset_hour = self._config.daily_reset_hour
            tz = resolve_user_timezone(self._config.user_timezone)
            now_local = now.astimezone(tz)
            last_local = last.astimezone(tz)
            today_reset = now_local.replace(hour=reset_hour, minute=0, second=0, microsecond=0)
            if now_local >= today_reset:
                # Today's reset boundary has passed — check if session predates it.
                crossed_reset = last_local < today_reset
            else:
                # Today's reset hasn't occurred yet — check against yesterday's boundary.
                # This catches sessions created before yesterday's reset_hour that are
                # still active when queried before today's reset_hour.
                yesterday_reset = today_reset - timedelta(days=1)
                crossed_reset = last_local < yesterday_reset
            if crossed_reset:
                logger.debug("Session fresh check: fresh=no reason=daily_reset")
                return False

        return True

    def _is_fresh(self, session: SessionData) -> bool:
        if not self._within_message_limit(session.message_count):
            return False
        if not self._time_fresh(session):
            return False
        logger.debug("Session fresh check: fresh=yes reason=still_valid")
        return True

    async def _load(self) -> dict[str, SessionData]:
        """Load sessions from JSON file.

        Handles migration from legacy unprefixed keys (``"12345"``,
        ``"12345:99"``) to transport-prefixed keys (``"tg:12345"``,
        ``"tg:12345:99"``).
        """

        def _read() -> dict[str, SessionData]:
            data = load_json(self._path)
            if data is None:
                return {}
            result: dict[str, SessionData] = {}
            for k, v in data.items():
                parsed = SessionKey.parse(k)
                if "topic_id" not in v and parsed.topic_id is not None:
                    v["topic_id"] = parsed.topic_id
                # Propagate transport from the parsed key into the dict
                # so SessionData picks it up (legacy entries lack it).
                if "transport" not in v:
                    v["transport"] = parsed.transport
                sd = SessionData(**v)
                # Re-key under the canonical prefixed storage key
                result[parsed.storage_key] = sd
            return result

        return await asyncio.to_thread(_read)

    async def _save(self, sessions: dict[str, SessionData]) -> None:
        """Atomically write sessions to JSON file."""

        def _write() -> None:
            atomic_json_save(self._path, {k: asdict(v) for k, v in sessions.items()})

        await asyncio.to_thread(_write)
