"""Tests for CLI auth detection."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ductor_bot.cli.auth import (
    AuthResult,
    AuthStatus,
    check_claude_auth,
    check_codex_auth,
    format_age,
)

if TYPE_CHECKING:
    import pytest


def test_auth_status_values() -> None:
    assert AuthStatus.AUTHENTICATED.value == "authenticated"
    assert AuthStatus.INSTALLED.value == "installed"
    assert AuthStatus.NOT_FOUND.value == "not_found"


def test_auth_result_is_authenticated() -> None:
    result = AuthResult(provider="claude", status=AuthStatus.AUTHENTICATED)
    assert result.is_authenticated is True


def test_auth_result_not_authenticated() -> None:
    result = AuthResult(provider="claude", status=AuthStatus.INSTALLED)
    assert result.is_authenticated is False


def test_auth_result_age_human_none() -> None:
    result = AuthResult(provider="claude", status=AuthStatus.NOT_FOUND)
    assert result.age_human == ""


def test_format_age_seconds() -> None:
    from datetime import UTC, datetime, timedelta

    dt = datetime.now(UTC) - timedelta(seconds=30)
    assert format_age(dt) == "30s ago"


def test_format_age_minutes() -> None:
    from datetime import UTC, datetime, timedelta

    dt = datetime.now(UTC) - timedelta(minutes=5)
    assert format_age(dt) == "5m ago"


def test_format_age_hours() -> None:
    from datetime import UTC, datetime, timedelta

    dt = datetime.now(UTC) - timedelta(hours=3)
    assert format_age(dt) == "3h ago"


def test_format_age_days() -> None:
    from datetime import UTC, datetime, timedelta

    dt = datetime.now(UTC) - timedelta(days=2)
    assert format_age(dt) == "2d ago"


def test_check_claude_auth_not_found(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    result = check_claude_auth()
    assert result.status == AuthStatus.NOT_FOUND


def test_check_claude_auth_installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    (tmp_path / ".claude").mkdir()
    result = check_claude_auth()
    assert result.status == AuthStatus.INSTALLED


def test_check_claude_auth_authenticated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    (claude_dir / ".credentials.json").write_text("{}")
    result = check_claude_auth()
    assert result.status == AuthStatus.AUTHENTICATED
    assert result.auth_file is not None


def test_check_codex_auth_not_found(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    result = check_codex_auth()
    assert result.status == AuthStatus.NOT_FOUND


def test_check_codex_auth_authenticated(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    (codex_dir / "auth.json").write_text("{}")
    monkeypatch.setenv("CODEX_HOME", str(codex_dir))
    result = check_codex_auth()
    assert result.status == AuthStatus.AUTHENTICATED
