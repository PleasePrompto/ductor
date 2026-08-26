"""Tests for path tokens.

These exist because encoding paths directly into callback_data silently
overflowed Telegram's 64-byte cap once a tree got a few levels deep.
"""

from __future__ import annotations

from pathlib import Path

from ductor_bot.files import path_tokens


def setup_function() -> None:
    path_tokens.clear()


def test_token_round_trips(tmp_path: Path) -> None:
    token = path_tokens.token_for(tmp_path)
    assert path_tokens.path_for(token) == tmp_path.resolve()


def test_token_is_stable_across_renders(tmp_path: Path) -> None:
    """Re-rendering a directory must not invalidate buttons already on screen."""
    assert path_tokens.token_for(tmp_path) == path_tokens.token_for(tmp_path)


def test_callback_data_fits_telegram_limit(tmp_path: Path) -> None:
    deep = tmp_path / ("a" * 60) / ("b" * 60) / ("c" * 60) / ("d" * 60)
    deep.mkdir(parents=True)
    payload = f"sf:{path_tokens.token_for(deep)}"
    assert len(payload.encode()) <= 64


def test_unknown_token_returns_none() -> None:
    assert path_tokens.path_for("deadbeef00") is None


def test_registry_is_bounded(tmp_path: Path) -> None:
    """A long-running bot must not accumulate paths without limit."""
    cap = path_tokens._MAX_ENTRIES
    for i in range(cap + 50):
        path_tokens.token_for(tmp_path / str(i))
    assert path_tokens.size() <= cap


def test_recently_used_entries_survive_eviction(tmp_path: Path) -> None:
    keep = tmp_path / "keep-me"
    token = path_tokens.token_for(keep)
    for i in range(path_tokens._MAX_ENTRIES - 1):
        path_tokens.token_for(tmp_path / f"filler{i}")
        if i % 500 == 0:
            path_tokens.path_for(token)  # touch it
    assert path_tokens.path_for(token) == keep.resolve()
