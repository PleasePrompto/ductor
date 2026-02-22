"""Integration tests for provider-isolated SessionManager behavior."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from ductor_bot.config import AgentConfig
from ductor_bot.session.manager import SessionData, SessionManager


def _make_manager(tmp_path: Path, **overrides: Any) -> SessionManager:
    cfg = AgentConfig(**overrides)
    return SessionManager(sessions_path=tmp_path / "sessions.json", config=cfg)


async def _simulate_cli_response(
    mgr: SessionManager,
    session: SessionData,
    cli_session_id: str,
    *,
    cost_usd: float = 0.0,
    tokens: int = 0,
) -> None:
    session.session_id = cli_session_id
    await mgr.update_session(session, cost_usd=cost_usd, tokens=tokens)


async def test_provider_switch_preserves_other_session(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)

    claude, _ = await mgr.resolve_session(chat_id=1, provider="claude", model="opus")
    await _simulate_cli_response(mgr, claude, "claude-sid")

    codex, is_new = await mgr.resolve_session(chat_id=1, provider="codex", model="gpt-5.2-codex")

    assert is_new is True
    assert codex.session_id == ""
    assert codex.provider_sessions["claude"].session_id == "claude-sid"


async def test_switch_back_resumes_original_session(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)

    claude, _ = await mgr.resolve_session(chat_id=1, provider="claude", model="opus")
    await _simulate_cli_response(mgr, claude, "claude-sid")

    _codex, _ = await mgr.resolve_session(chat_id=1, provider="codex", model="gpt-5.2-codex")
    resumed, is_new = await mgr.resolve_session(chat_id=1, provider="claude", model="opus")

    assert is_new is False
    assert resumed.session_id == "claude-sid"


async def test_provider_switch_preserves_other_metrics(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)

    claude, _ = await mgr.resolve_session(chat_id=1, provider="claude", model="opus")
    await _simulate_cli_response(mgr, claude, "claude-sid", cost_usd=0.2, tokens=400)

    codex, _ = await mgr.resolve_session(chat_id=1, provider="codex", model="gpt-5.2-codex")
    await _simulate_cli_response(mgr, codex, "codex-sid", cost_usd=0.1, tokens=50)

    switched_back, _ = await mgr.resolve_session(chat_id=1, provider="claude", model="opus")
    assert switched_back.message_count == 1
    assert switched_back.total_cost_usd == pytest.approx(0.2)
    assert switched_back.total_tokens == 400

    switched_back.provider = "codex"
    assert switched_back.message_count == 1
    assert switched_back.total_cost_usd == pytest.approx(0.1)
    assert switched_back.total_tokens == 50


async def test_reset_session_clears_all_providers(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)

    claude, _ = await mgr.resolve_session(chat_id=1, provider="claude", model="opus")
    await _simulate_cli_response(mgr, claude, "claude-sid")
    codex, _ = await mgr.resolve_session(chat_id=1, provider="codex", model="gpt-5.2-codex")
    await _simulate_cli_response(mgr, codex, "codex-sid")

    reset = await mgr.reset_session(chat_id=1, provider="claude", model="opus")

    assert reset.provider_sessions == {}
    assert reset.session_id == ""
    assert reset.message_count == 0


async def test_reset_provider_session_clears_only_one(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)

    claude, _ = await mgr.resolve_session(chat_id=1, provider="claude", model="opus")
    await _simulate_cli_response(mgr, claude, "claude-sid")

    codex, _ = await mgr.resolve_session(chat_id=1, provider="codex", model="gpt-5.2-codex")
    await _simulate_cli_response(mgr, codex, "codex-sid")

    reset = await mgr.reset_provider_session(chat_id=1, provider="codex", model="gpt-5.2-codex")

    assert reset.provider == "codex"
    assert reset.model == "gpt-5.2-codex"
    assert reset.session_id == ""
    assert reset.message_count == 0
    assert reset.provider_sessions["claude"].session_id == "claude-sid"
    assert "codex" not in reset.provider_sessions


async def test_resolve_session_no_existing_codex_is_new(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)

    claude, _ = await mgr.resolve_session(chat_id=1, provider="claude", model="opus")
    await _simulate_cli_response(mgr, claude, "claude-sid")

    codex, is_new = await mgr.resolve_session(chat_id=1, provider="codex", model="gpt-5.2-codex")

    assert is_new is True
    assert codex.session_id == ""


async def test_metrics_increment_only_active_provider(tmp_path: Path) -> None:
    mgr = _make_manager(tmp_path)

    claude, _ = await mgr.resolve_session(chat_id=1, provider="claude", model="opus")
    await _simulate_cli_response(mgr, claude, "claude-sid", cost_usd=0.2, tokens=100)

    codex, _ = await mgr.resolve_session(chat_id=1, provider="codex", model="gpt-5.2-codex")
    await _simulate_cli_response(mgr, codex, "codex-sid", cost_usd=0.1, tokens=50)
    await mgr.update_session(codex, cost_usd=0.05, tokens=30)

    active = await mgr.get_active(1)
    assert active is not None
    assert active.provider_sessions["claude"].message_count == 1
    assert active.provider_sessions["claude"].total_cost_usd == pytest.approx(0.2)
    assert active.provider_sessions["claude"].total_tokens == 100

    assert active.provider_sessions["codex"].message_count == 2
    assert active.provider_sessions["codex"].total_cost_usd == pytest.approx(0.15)
    assert active.provider_sessions["codex"].total_tokens == 80


async def test_persistence_round_trip_multi_provider(tmp_path: Path) -> None:
    path = tmp_path / "sessions.json"
    cfg = AgentConfig()

    mgr1 = SessionManager(sessions_path=path, config=cfg)
    claude, _ = await mgr1.resolve_session(chat_id=1, provider="claude", model="opus")
    await _simulate_cli_response(mgr1, claude, "claude-sid", cost_usd=0.2, tokens=100)

    codex, _ = await mgr1.resolve_session(chat_id=1, provider="codex", model="gpt-5.2-codex")
    await _simulate_cli_response(mgr1, codex, "codex-sid", cost_usd=0.1, tokens=50)

    mgr2 = SessionManager(sessions_path=path, config=cfg)

    claude_loaded, claude_is_new = await mgr2.resolve_session(
        chat_id=1,
        provider="claude",
        model="opus",
    )
    assert claude_is_new is False
    assert claude_loaded.session_id == "claude-sid"
    assert claude_loaded.total_cost_usd == pytest.approx(0.2)

    codex_loaded, codex_is_new = await mgr2.resolve_session(
        chat_id=1,
        provider="codex",
        model="gpt-5.2-codex",
    )
    assert codex_is_new is False
    assert codex_loaded.session_id == "codex-sid"
    assert codex_loaded.total_cost_usd == pytest.approx(0.1)
