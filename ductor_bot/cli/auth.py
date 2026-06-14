"""CLI auth detection via filesystem checks."""

from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum, unique
from pathlib import Path
from typing import TYPE_CHECKING

from ductor_bot.cli.antigravity_runtime import antigravity_process_env
from ductor_bot.cli.gemini_utils import find_gemini_cli
from ductor_bot.config import NULLISH_TEXT_VALUES
from ductor_bot.infra.platform import CREATION_FLAGS as _CREATION_FLAGS

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)

_GEMINI_AUTH_ENV_KEYS = frozenset(
    {"GEMINI_API_KEY", "GOOGLE_API_KEY", "GOOGLE_CLOUD_PROJECT", "GOOGLE_CLOUD_LOCATION"}
)
_GEMINI_SELECTED_AUTH_TYPES = frozenset(
    {"oauth-personal", "gemini-api-key", "vertex-ai", "compute-default-credentials", "cloud-shell"}
)
_GEMINI_NON_API_KEY_AUTH_TYPES = frozenset(
    {"oauth-personal", "vertex-ai", "compute-default-credentials", "cloud-shell"}
)


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
    """Check Claude Code CLI auth via credentials file, env var, or CLI fallback."""
    claude_dir = Path.home() / ".claude"
    credentials = claude_dir / ".credentials.json"

    # Fast path: credentials file (standard OAuth login).
    if credentials.is_file():
        mtime = datetime.fromtimestamp(credentials.stat().st_mtime, tz=UTC)
        result = AuthResult("claude", AuthStatus.AUTHENTICATED, credentials, mtime)
        logger.debug("Auth check provider=%s status=%s", result.provider, result.status)
        return result

    # ANTHROPIC_API_KEY environment variable.
    if _has_nonempty_env("ANTHROPIC_API_KEY"):
        result = AuthResult("claude", AuthStatus.AUTHENTICATED)
        logger.debug("Auth check provider=%s status=%s (env key)", result.provider, result.status)
        return result

    # Fallback: ask the CLI itself (covers managed keys, OAuth tokens, etc.).
    if _claude_cli_logged_in():
        result = AuthResult("claude", AuthStatus.AUTHENTICATED)
        logger.debug("Auth check provider=%s status=%s (cli)", result.provider, result.status)
        return result

    if claude_dir.is_dir():
        result = AuthResult("claude", AuthStatus.INSTALLED)
        logger.debug("Auth check provider=%s status=%s", result.provider, result.status)
        return result

    result = AuthResult("claude", AuthStatus.NOT_FOUND)
    logger.debug("Auth check provider=%s status=%s", result.provider, result.status)
    return result


def _claude_cli_logged_in() -> bool:
    """Run ``claude auth status`` and return True when the CLI reports logged-in."""
    # Resolve the executable so the npm shim (claude.cmd on Windows) is found; a
    # bare "claude" raises FileNotFoundError there, so OAuth auth is missed (#149).
    claude_cli = shutil.which("claude") or "claude"
    try:
        proc = subprocess.run(
            [claude_cli, "auth", "status"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            creationflags=_CREATION_FLAGS,
        )
        data = json.loads(proc.stdout)
        return data.get("loggedIn") is True
    except (
        OSError,
        subprocess.TimeoutExpired,
        json.JSONDecodeError,
        TypeError,
        ValueError,
    ):
        return False


def check_codex_auth() -> AuthResult:
    """Check Codex CLI auth via ``$CODEX_HOME/auth.json``, env var, or install markers."""
    try:
        default_home = str(Path.home() / ".codex")
    except (RuntimeError, OSError):
        logger.debug("Codex auth: home directory unresolvable; treating as NOT_FOUND")
        return AuthResult("codex", AuthStatus.NOT_FOUND)
    codex_home = Path(os.environ.get("CODEX_HOME", default_home))
    auth_file = codex_home / "auth.json"

    # Fast path: auth.json credential file.
    if auth_file.is_file():
        mtime = datetime.fromtimestamp(auth_file.stat().st_mtime, tz=UTC)
        result = AuthResult("codex", AuthStatus.AUTHENTICATED, auth_file, mtime)
        logger.debug("Auth check provider=%s status=%s", result.provider, result.status)
        return result

    # OPENAI_API_KEY environment variable.
    if _has_nonempty_env("OPENAI_API_KEY"):
        result = AuthResult("codex", AuthStatus.AUTHENTICATED)
        logger.debug("Auth check provider=%s status=%s (env key)", result.provider, result.status)
        return result

    # Installation indicators: version.json or config.toml.
    if (codex_home / "version.json").is_file() or (codex_home / "config.toml").is_file():
        result = AuthResult("codex", AuthStatus.INSTALLED)
        logger.debug("Auth check provider=%s status=%s", result.provider, result.status)
        return result

    result = AuthResult("codex", AuthStatus.NOT_FOUND)
    logger.debug("Auth check provider=%s status=%s", result.provider, result.status)
    return result


def check_gemini_auth() -> AuthResult:
    """Check Gemini CLI auth via OAuth cache, env/.env keys, and Gemini settings."""
    try:
        find_gemini_cli()
    except FileNotFoundError:
        result = AuthResult("gemini", AuthStatus.NOT_FOUND)
        logger.debug("Auth check provider=%s status=%s", result.provider, result.status)
        return result

    gemini_home = _gemini_home_dir()

    oauth_file = gemini_home / "oauth_creds.json"
    if _is_nonempty_file(oauth_file):
        mtime = datetime.fromtimestamp(oauth_file.stat().st_mtime, tz=UTC)
        result = AuthResult("gemini", AuthStatus.AUTHENTICATED, oauth_file, mtime)
        logger.debug("Auth check provider=%s status=%s", result.provider, result.status)
        return result

    auth_file, auth_age = _gemini_key_auth_source(gemini_home)
    if auth_file is not None or auth_age is not None or _gemini_has_env_auth():
        result = AuthResult("gemini", AuthStatus.AUTHENTICATED, auth_file, auth_age)
        logger.debug("Auth check provider=%s status=%s", result.provider, result.status)
        return result

    selected_result = _gemini_selected_type_auth(gemini_home)
    if selected_result is not None:
        result = selected_result
        logger.debug("Auth check provider=%s status=%s", result.provider, result.status)
        return result

    result = AuthResult("gemini", AuthStatus.INSTALLED)
    logger.debug("Auth check provider=%s status=%s", result.provider, result.status)
    return result


def _gemini_home_dir() -> Path:
    base = Path(os.environ.get("GEMINI_CLI_HOME", str(Path.home())))
    return base / ".gemini"


def _has_nonempty_env(name: str) -> bool:
    return bool(_normalize_key_like_value(os.environ.get(name, "")))


def _is_nonempty_file(path: Path) -> bool:
    return path.is_file() and path.stat().st_size > 0


def _discover_gemini_dotenv_keys(gemini_home: Path) -> tuple[set[str], Path | None]:
    keys: set[str] = set()
    source: Path | None = None
    # Gemini CLI checks ~/.gemini/.env first, then ~/.env
    for path in (gemini_home / ".env", gemini_home.parent / ".env"):
        file_keys = _read_dotenv_keys(path)
        if file_keys:
            keys |= file_keys
            if source is None:
                source = path
    return keys, source


def _gemini_has_env_auth() -> bool:
    has_key = _has_nonempty_env("GEMINI_API_KEY") or _has_nonempty_env("GOOGLE_API_KEY")
    has_vertex = _has_nonempty_env("GOOGLE_CLOUD_PROJECT") and _has_nonempty_env(
        "GOOGLE_CLOUD_LOCATION"
    )
    return has_key or has_vertex


def _gemini_key_auth_source(gemini_home: Path) -> tuple[Path | None, datetime | None]:
    """Return file-based auth source for Gemini API-key style auth, if available."""
    dotenv_keys, dotenv_file = _discover_gemini_dotenv_keys(gemini_home)
    has_dotenv_key = "GEMINI_API_KEY" in dotenv_keys or "GOOGLE_API_KEY" in dotenv_keys
    has_dotenv_vertex = {
        "GOOGLE_CLOUD_PROJECT",
        "GOOGLE_CLOUD_LOCATION",
    }.issubset(dotenv_keys)
    if has_dotenv_key or has_dotenv_vertex:
        if dotenv_file is None:
            return None, None
        return dotenv_file, datetime.fromtimestamp(dotenv_file.stat().st_mtime, tz=UTC)

    ductor_key, ductor_config_path = read_ductor_gemini_api_key()
    if ductor_key and ductor_config_path is not None:
        return ductor_config_path, datetime.fromtimestamp(
            ductor_config_path.stat().st_mtime, tz=UTC
        )
    return None, None


def _gemini_selected_type_auth(gemini_home: Path) -> AuthResult | None:
    settings_file = gemini_home / "settings.json"
    selected_type = read_gemini_selected_auth_type(settings_file)
    if selected_type == "oauth-personal":
        accounts_file = gemini_home / "google_accounts.json"
        if _has_active_google_account(accounts_file):
            mtime = datetime.fromtimestamp(accounts_file.stat().st_mtime, tz=UTC)
            return AuthResult("gemini", AuthStatus.AUTHENTICATED, accounts_file, mtime)
        return None
    if selected_type == "gemini-api-key":
        # Treat explicit API-key mode selection as authenticated. The key itself
        # may come from external sources (e.g. shell/profile/secret store) that
        # are not reliably discoverable via filesystem probes.
        mtime = datetime.fromtimestamp(settings_file.stat().st_mtime, tz=UTC)
        return AuthResult("gemini", AuthStatus.AUTHENTICATED, settings_file, mtime)
    if selected_type in _GEMINI_SELECTED_AUTH_TYPES:
        mtime = datetime.fromtimestamp(settings_file.stat().st_mtime, tz=UTC)
        return AuthResult("gemini", AuthStatus.AUTHENTICATED, settings_file, mtime)
    return None


def _read_dotenv_keys(path: Path) -> set[str]:
    if not path.is_file():
        return set()
    found: set[str] = set()
    try:
        for raw_line in path.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if not line or line.startswith("#"):
                continue
            if line.startswith("export "):
                line = line.removeprefix("export ").strip()
            key, separator, value = line.partition("=")
            if separator != "=":
                continue
            key = key.strip()
            if key not in _GEMINI_AUTH_ENV_KEYS:
                continue
            parsed = _normalize_dotenv_value(value)
            if parsed:
                found.add(key)
    except OSError:
        return set()
    return found


def _normalize_dotenv_value(raw: str) -> str:
    value = raw.strip()
    if not value:
        return ""
    if value[0] in {'"', "'"} and value[-1:] == value[0]:
        return _normalize_key_like_value(value[1:-1].strip())
    return _normalize_key_like_value(value.split("#", 1)[0].strip())


def read_gemini_selected_auth_type(settings_file: Path) -> str | None:
    if not settings_file.is_file():
        return None
    try:
        data = json.loads(settings_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return None

    selected: object | None = None
    if isinstance(data, dict):
        security = data.get("security")
        if isinstance(security, dict):
            auth = security.get("auth")
            if isinstance(auth, dict):
                selected = auth.get("selectedType")

    if isinstance(selected, str) and selected:
        return selected
    return None


def read_ductor_gemini_api_key() -> tuple[str | None, Path | None]:
    """Read ``gemini_api_key`` from ``~/.ductor/config/config.json``.

    Returns ``(key, path)`` when configured, otherwise ``(None, None)``.
    """
    config_path = _ductor_config_path()
    if not config_path.is_file():
        return None, None

    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return None, None

    if not isinstance(data, dict):
        return None, None

    raw = data.get("gemini_api_key")
    if isinstance(raw, str):
        key = _normalize_key_like_value(raw)
        if key:
            return key, config_path
    return None, None


def gemini_api_key_mode_selected(settings_file: Path) -> bool:
    """Return True when Gemini config indicates API-key mode (or no explicit mode)."""
    selected_type = read_gemini_selected_auth_type(settings_file)
    if selected_type is None:
        return True
    if selected_type in _GEMINI_NON_API_KEY_AUTH_TYPES:
        return False
    return selected_type == "gemini-api-key"


def gemini_uses_api_key_mode() -> bool:
    """Return True when Gemini settings explicitly use API-key auth mode."""
    settings_file = _gemini_home_dir() / "settings.json"
    return read_gemini_selected_auth_type(settings_file) == "gemini-api-key"


def _ductor_config_path() -> Path:
    from ductor_bot.workspace.paths import resolve_paths

    return resolve_paths().config_path


def _has_active_google_account(accounts_file: Path) -> bool:
    if not accounts_file.is_file():
        return False
    try:
        data = json.loads(accounts_file.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError, TypeError):
        return False
    if not isinstance(data, dict):
        return False
    active = data.get("active")
    return isinstance(active, str) and bool(active.strip())


def _normalize_key_like_value(raw: str) -> str:
    value = raw.strip()
    if not value or value.lower() in NULLISH_TEXT_VALUES:
        return ""
    return value


def _antigravity_cli_logged_in() -> bool:
    # Resolve the executable so the npm shim (agy.cmd on Windows) is found; a
    # bare "agy" raises FileNotFoundError there, so OAuth auth is missed (#149).
    agy_cli = shutil.which("agy") or "agy"
    try:
        result = subprocess.run(
            [agy_cli, "models"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            env=antigravity_process_env(),
            creationflags=_CREATION_FLAGS,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.debug("Antigravity CLI auth probe failed: %s", exc)
        return False

    output = f"{result.stdout}\n{result.stderr}".lower()
    if "sign in" in output or "not logged in" in output or "login" in output:
        return False
    return result.returncode == 0


def check_antigravity_auth() -> AuthResult:
    """Check if Antigravity CLI (agy) is installed and configured."""
    binary = shutil.which("agy")
    if binary is not None and _antigravity_cli_logged_in():
        logger.debug("Auth check provider=antigravity status=AUTHENTICATED (agy models)")
        return AuthResult(provider="antigravity", status=AuthStatus.AUTHENTICATED)

    ccs_settings = Path.home() / ".ccs" / "agy.settings.json"
    if binary is not None or ccs_settings.is_file():
        logger.debug("Auth check provider=antigravity status=INSTALLED")
        return AuthResult(provider="antigravity", status=AuthStatus.INSTALLED)

    logger.debug("Auth check provider=antigravity status=NOT_FOUND")
    return AuthResult(provider="antigravity", status=AuthStatus.NOT_FOUND)


_CHECKERS: dict[str, Callable[[], AuthResult]] = {
    "claude": check_claude_auth,
    "codex": check_codex_auth,
    "gemini": check_gemini_auth,
    "antigravity": check_antigravity_auth,
}


def check_all_auth() -> dict[str, AuthResult]:
    """Check auth for all known providers."""
    return {name: fn() for name, fn in _CHECKERS.items()}
