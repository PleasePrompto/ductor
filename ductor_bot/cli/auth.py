"""CLI auth detection via filesystem checks."""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum, unique
from pathlib import Path
from shutil import which
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)


@unique
class AuthStatus(StrEnum):
    """Provider authentication state."""

    AUTHENTICATED = "authenticated"
    INSTALLED = "installed"
    NOT_FOUND = "not_found"


@dataclass(frozen=True, slots=True)
class AuthResult:
    """Result of a provider auth check."""

    provider: str
    status: AuthStatus
    auth_file: Path | None = None
    auth_age: datetime | None = None

    @property
    def is_authenticated(self) -> bool:
        return self.status == AuthStatus.AUTHENTICATED

    @property
    def age_human(self) -> str:
        """Human-readable age of the auth file."""
        if self.auth_age is None:
            return ""
        return format_age(self.auth_age)


def format_age(dt: datetime) -> str:
    """Format a datetime as a human-readable relative age string."""
    delta = datetime.now(UTC) - dt
    seconds = int(delta.total_seconds())
    if seconds < 0:
        return "just now"
    if seconds < 60:
        return f"{seconds}s ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"


def check_claude_auth() -> AuthResult:
    """Check Claude Code CLI auth via ``~/.claude/.credentials.json``."""
    claude_dir = Path.home() / ".claude"
    credentials = claude_dir / ".credentials.json"

    if credentials.is_file():
        mtime = datetime.fromtimestamp(credentials.stat().st_mtime, tz=UTC)
        result = AuthResult("claude", AuthStatus.AUTHENTICATED, credentials, mtime)
        logger.debug("Auth check provider=%s status=%s", result.provider, result.status)
        return result

    if claude_dir.is_dir():
        result = AuthResult("claude", AuthStatus.INSTALLED)
        logger.debug("Auth check provider=%s status=%s", result.provider, result.status)
        return result

    result = AuthResult("claude", AuthStatus.NOT_FOUND)
    logger.debug("Auth check provider=%s status=%s", result.provider, result.status)
    return result


def check_codex_auth() -> AuthResult:
    """Check Codex CLI auth via ``$CODEX_HOME/auth.json``."""
    codex_home = Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex")))
    auth_file = codex_home / "auth.json"
    version_file = codex_home / "version.json"

    if auth_file.is_file():
        mtime = datetime.fromtimestamp(auth_file.stat().st_mtime, tz=UTC)
        result = AuthResult("codex", AuthStatus.AUTHENTICATED, auth_file, mtime)
        logger.debug("Auth check provider=%s status=%s", result.provider, result.status)
        return result

    if version_file.is_file():
        result = AuthResult("codex", AuthStatus.INSTALLED)
        logger.debug("Auth check provider=%s status=%s", result.provider, result.status)
        return result

    result = AuthResult("codex", AuthStatus.NOT_FOUND)
    logger.debug("Auth check provider=%s status=%s", result.provider, result.status)
    return result


def check_gemini_auth() -> AuthResult:
    """Check Gemini CLI auth via command availability."""
    has_cli = which("gemini") is not None or which("npx") is not None
    has_key = "GEMINI_API_KEY" in os.environ

    if has_key:
        return AuthResult("gemini", AuthStatus.AUTHENTICATED)

    if has_cli:
        # If we have the CLI, we assume authenticated because it uses internal
        # persistence (browser login). We'll know for sure during first call.
        return AuthResult("gemini", AuthStatus.AUTHENTICATED)

    return AuthResult("gemini", AuthStatus.NOT_FOUND)


_CHECKERS: dict[str, Callable[[], AuthResult]] = {
    "claude": check_claude_auth,
    "codex": check_codex_auth,
    "gemini": check_gemini_auth,
}


def check_all_auth() -> dict[str, AuthResult]:
    """Check auth for all known providers."""
    return {name: fn() for name, fn in _CHECKERS.items()}
