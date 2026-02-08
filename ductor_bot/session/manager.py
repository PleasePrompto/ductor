"""Session lifecycle: creation, freshness checks, reset. JSON-based persistence."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import tempfile
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path

from ductor_bot.config import AgentConfig, resolve_user_timezone

logger = logging.getLogger(__name__)


@dataclass
class SessionData:
    """Active session state."""

    session_id: str
    chat_id: int
    provider: str = "claude"
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

    async def resolve_session(
        self, chat_id: int, *, provider: str | None = None
    ) -> tuple[SessionData, bool]:
        """Returns (session, is_new). Reuses if fresh, creates if stale."""
        sessions = await self._load()
        key = str(chat_id)
        existing = sessions.get(key)

        prov = provider or self._config.provider

        if existing and self._is_fresh(existing):
            if provider and existing.provider != prov:
                logger.info("Provider switch %s -> %s, resetting session", existing.provider, prov)
                existing.session_id = ""
                existing.provider = prov
                existing.message_count = 0
                await self._save(sessions)
                return existing, True
            if not existing.session_id:
                return existing, True
            return existing, False

        new = SessionData(
            session_id="",
            chat_id=chat_id,
            provider=prov,
        )
        sessions[key] = new
        await self._save(sessions)
        logger.info("Session created provider=%s", prov)
        return new, True

    async def get_active(self, chat_id: int) -> SessionData | None:
        """Return the current session for chat_id without creating one."""
        sessions = await self._load()
        return sessions.get(str(chat_id))

    async def reset_session(self, chat_id: int) -> SessionData:
        """Force-create a new session (empty ID, filled by CLI on first call)."""
        sessions = await self._load()
        new = SessionData(
            session_id="",
            chat_id=chat_id,
            provider=self._config.provider,
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
        """Update session metrics and persist."""
        session.last_active = datetime.now(UTC).isoformat()
        session.message_count += 1
        session.total_cost_usd += cost_usd
        session.total_tokens += tokens

        sessions = await self._load()
        sessions[str(session.chat_id)] = session
        await self._save(sessions)

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

        reset_hour = self._config.daily_reset_hour
        tz = resolve_user_timezone(self._config.user_timezone)
        now_local = now.astimezone(tz)
        last_local = last.astimezone(tz)
        today_reset = now_local.replace(hour=reset_hour, minute=0, second=0, microsecond=0)
        crossed_reset = now_local >= today_reset and last_local < today_reset
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
