"""Extended auth tests -- covering gaps."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ductor_bot.cli.auth import (
    AuthStatus,
    check_all_auth,
    check_codex_auth,
    format_age,
)

if TYPE_CHECKING:
    import pytest


def test_check_codex_auth_installed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """version.json exists but auth.json does not -> INSTALLED."""
    codex_dir = tmp_path / ".codex"
    codex_dir.mkdir()
    (codex_dir / "version.json").write_text("{}")
    monkeypatch.setenv("CODEX_HOME", str(codex_dir))
    result = check_codex_auth()
    assert result.status == AuthStatus.INSTALLED
    assert result.auth_file is None


def test_check_all_auth_returns_both(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.delenv("CODEX_HOME", raising=False)
    results = check_all_auth()
    assert "claude" in results
    assert "codex" in results


def test_format_age_future_returns_just_now() -> None:
    from datetime import UTC, datetime, timedelta

    future_dt = datetime.now(UTC) + timedelta(seconds=10)
    assert format_age(future_dt) == "just now"
