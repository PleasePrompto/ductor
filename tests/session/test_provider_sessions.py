"""Unit tests for provider-isolated SessionData properties and migration."""

from __future__ import annotations

from dataclasses import asdict

from ductor_bot.session.manager import ProviderSessionData, SessionData


def test_session_id_property_reads_current_provider() -> None:
    session = SessionData(
        chat_id=1,
        provider="claude",
        provider_sessions={
            "claude": ProviderSessionData(session_id="claude-sid"),
            "codex": ProviderSessionData(session_id="codex-sid"),
        },
    )
    assert session.session_id == "claude-sid"

    session.provider = "codex"
    assert session.session_id == "codex-sid"


def test_session_id_returns_empty_for_unknown_provider() -> None:
    session = SessionData(
        chat_id=1,
        provider="codex",
        provider_sessions={"claude": ProviderSessionData(session_id="claude-sid")},
    )
    assert session.session_id == ""


def test_session_id_setter_writes_to_current_provider() -> None:
    session = SessionData(chat_id=1, provider="codex", provider_sessions={})

    session.session_id = "codex-sid"

    assert session.provider_sessions["codex"].session_id == "codex-sid"


def test_message_count_property_per_provider() -> None:
    session = SessionData(
        chat_id=1,
        provider="claude",
        provider_sessions={
            "claude": ProviderSessionData(message_count=2),
            "codex": ProviderSessionData(message_count=7),
        },
    )
    assert session.message_count == 2

    session.provider = "codex"
    assert session.message_count == 7


def test_cost_property_per_provider() -> None:
    session = SessionData(
        chat_id=1,
        provider="claude",
        provider_sessions={
            "claude": ProviderSessionData(total_cost_usd=0.12),
            "codex": ProviderSessionData(total_cost_usd=0.34),
        },
    )
    assert session.total_cost_usd == 0.12

    session.provider = "codex"
    assert session.total_cost_usd == 0.34


def test_tokens_property_per_provider() -> None:
    session = SessionData(
        chat_id=1,
        provider="claude",
        provider_sessions={
            "claude": ProviderSessionData(total_tokens=123),
            "codex": ProviderSessionData(total_tokens=456),
        },
    )
    assert session.total_tokens == 123

    session.provider = "codex"
    assert session.total_tokens == 456


def test_clear_all_sessions() -> None:
    session = SessionData(
        chat_id=1,
        provider="claude",
        provider_sessions={
            "claude": ProviderSessionData(session_id="claude-sid"),
            "codex": ProviderSessionData(session_id="codex-sid"),
        },
    )

    session.clear_all_sessions()

    assert session.provider_sessions == {}
    assert session.session_id == ""


def test_clear_provider_session() -> None:
    session = SessionData(
        chat_id=1,
        provider="claude",
        provider_sessions={
            "claude": ProviderSessionData(session_id="claude-sid"),
            "codex": ProviderSessionData(session_id="codex-sid"),
        },
    )

    session.clear_provider_session("claude")

    assert "claude" not in session.provider_sessions
    assert session.provider_sessions["codex"].session_id == "codex-sid"


def test_migration_from_old_json_format() -> None:
    session = SessionData(
        chat_id=1,
        provider="claude",
        session_id="legacy-sid",
        message_count=4,
        total_cost_usd=0.2,
        total_tokens=900,
    )

    provider_data = session.provider_sessions["claude"]
    assert provider_data.session_id == "legacy-sid"
    assert provider_data.message_count == 4
    assert provider_data.total_cost_usd == 0.2
    assert provider_data.total_tokens == 900


def test_migration_empty_session_id() -> None:
    session = SessionData(
        chat_id=1,
        provider="claude",
        session_id="",
        message_count=0,
        total_cost_usd=0.0,
        total_tokens=0,
    )

    assert "claude" in session.provider_sessions
    assert session.provider_sessions["claude"].session_id == ""


def test_asdict_contains_provider_sessions_not_session_id() -> None:
    session = SessionData(
        chat_id=1,
        provider="claude",
        provider_sessions={"claude": ProviderSessionData(session_id="sid")},
    )

    serialized = asdict(session)

    assert "provider_sessions" in serialized
    assert "session_id" not in serialized
    assert serialized["provider_sessions"]["claude"]["session_id"] == "sid"
