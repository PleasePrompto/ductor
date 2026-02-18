"""Session lifecycle: creation, freshness checks, reset. JSON-based persistence."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from ductor_bot.config import AgentConfig, resolve_user_timezone

logger = logging.getLogger(__name__)


@dataclass
class SessionData:
    """Active session state."""

    session_id: str
    chat_id: int
    provider: str = "claude"
    model: str = "opus"
    created_at: str = ""
    last_active: str = ""
    message_count: int = 0
    total_cost_usd: float = 0.0
    total_tokens: int = 0

    def __post_init__(self) -> None:
        now = datetime.now(UTC).isoformat()
        if not self.created_at:
            self.created_at = now
        if not self.last_active:
            self.last_active = now


class SessionManager:
    """Manages session lifecycle with JSON file persistence."""

    def __init__(self, sessions_path: Path, config: AgentConfig) -> None:
        self._path = sessions_path
        self._config = config
        self._lock = asyncio.Lock()

    async def resolve_session(
        self,
        chat_id: int,
        *,
        provider: str | None = None,
        model: str | None = None,
    ) -> tuple[SessionData, bool]:
        """Returns (session, is_new). Reuses if fresh, creates if stale."""
        sessions = await self._load()
        key = str(chat_id)
        existing = sessions.get(key)

        prov = provider or self._config.provider
        model_name = model or self._config.model

        if existing and self._is_fresh(existing):
            if provider and existing.provider != prov:
                logger.info("Provider switch %s -> %s, resetting session", existing.provider, prov)
                existing.session_id = ""
                existing.provider = prov
                existing.model = model_name
                existing.message_count = 0
                await self._save(sessions)
                return existing, True
            if existing.model != model_name:
                existing.model = model_name
                await self._save(sessions)
            if not existing.session_id:
                return existing, True
            return existing, False

        new = SessionData(
            session_id="",
            chat_id=chat_id,
            provider=prov,
            model=model_name,
        )
        sessions[key] = new
        await self._save(sessions)
        logger.info("Session created provider=%s model=%s", prov, model_name)
        return new, True

    async def get_active(self, chat_id: int) -> SessionData | None:
        """Return the current session for chat_id without creating one."""
        sessions = await self._load()
        return sessions.get(str(chat_id))

    async def reset_session(
        self,
        chat_id: int,
        *,
        provider: str | None = None,
        model: str | None = None,
    ) -> SessionData:
        """Force-create a new session (empty ID, filled by CLI on first call)."""
        sessions = await self._load()
        prov = provider or self._config.provider
        model_name = model or self._config.model
        new = SessionData(
            session_id="",
            chat_id=chat_id,
            provider=prov,
            model=model_name,
        )
        sessions[str(chat_id)] = new
        await self._save(sessions)
        logger.info("Session reset")
        return new

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
            key = str(session.chat_id)
            current = sessions.get(key)
            if current is None:
                current = session
            else:
                # Apply mutable identity fields from caller, but keep counters
                # from the latest persisted record to avoid stale overwrites.
                current.session_id = session.session_id
                current.provider = session.provider
                current.model = session.model

            current.last_active = datetime.now(UTC).isoformat()
            current.message_count += 1
            current.total_cost_usd += cost_usd
            current.total_tokens += tokens
            sessions[key] = current
            await self._save(sessions)

            # Keep caller reference in sync with persisted aggregate values.
            session.last_active = current.last_active
            session.message_count = current.message_count
            session.total_cost_usd = current.total_cost_usd
            session.total_tokens = current.total_tokens

    async def sync_session_target(
        self,
        session: SessionData,
        *,
        provider: str | None = None,
        model: str | None = None,
    ) -> None:
        """Persist provider/model changes without touching activity counters."""
        async with self._lock:
            sessions = await self._load()
            key = str(session.chat_id)
            current = sessions.get(key)
            if current is None:
                return

            changed = False
            if provider is not None and current.provider != provider:
                current.provider = provider
                changed = True
            if model is not None and current.model != model:
                current.model = model
                changed = True

            needs_model_migration = False
            if not changed:
                needs_model_migration = await asyncio.to_thread(
                    self._raw_entry_missing_model,
                    session.chat_id,
                )
            if not changed and not needs_model_migration:
                return

            sessions[key] = current
            await self._save(sessions)

            # Keep caller reference aligned with persisted target.
            session.provider = current.provider
            session.model = current.model

    def _raw_entry_missing_model(self, chat_id: int) -> bool:
        """Return True when raw session JSON exists but has no ``model`` key."""
        if not self._path.exists():
            return False
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return False
        entry = data.get(str(chat_id))
        return isinstance(entry, dict) and "model" not in entry

    def _is_fresh(self, session: SessionData) -> bool:
        now = datetime.now(UTC)
        try:
            last = datetime.fromisoformat(session.last_active)
        except (ValueError, TypeError):
            logger.warning("Corrupt session timestamp: %r, treating as stale", session.last_active)
            return False

        if (
            self._config.max_session_messages is not None
            and session.message_count >= self._config.max_session_messages
        ):
            logger.debug("Session fresh check: fresh=no reason=max_messages")
            return False

        timeout = self._config.idle_timeout_minutes
        if timeout > 0:
            idle_seconds = (now - last).total_seconds()
            if idle_seconds >= timeout * 60:
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

        logger.debug("Session fresh check: fresh=yes reason=still_valid")
        return True

    async def _load(self) -> dict[str, SessionData]:
        """Load sessions from JSON file."""

        def _read() -> dict[str, SessionData]:
            if not self._path.exists():
                return {}
            try:
                data = json.loads(self._path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                logger.warning("Corrupt sessions file, starting fresh")
                return {}
            return {k: SessionData(**v) for k, v in data.items()}

        return await asyncio.to_thread(_read)

    async def _save(self, sessions: dict[str, SessionData]) -> None:
        """Atomically write sessions to JSON file."""

        def _write() -> None:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            data = {k: asdict(v) for k, v in sessions.items()}
            content = json.dumps(data, indent=2)
            tmp_fd, tmp_path_str = tempfile.mkstemp(dir=self._path.parent, suffix=".tmp")
            os.close(tmp_fd)
            tmp = Path(tmp_path_str)
            try:
                tmp.write_text(content, encoding="utf-8")
                tmp.replace(self._path)
            except Exception:
                tmp.unlink(missing_ok=True)
                raise

        await asyncio.to_thread(_write)
