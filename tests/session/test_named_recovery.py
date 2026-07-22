"""Tests for NamedSession last_prompt, mark_running, and recovery tracking."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from ductor_bot.session.key import SessionKey
from ductor_bot.session.named import NamedSessionRegistry


def _make_registry(tmp_path: Path) -> NamedSessionRegistry:
    return NamedSessionRegistry(tmp_path / "named_sessions.json")


class TestLastPrompt:
    def test_created_session_has_empty_last_prompt(self, tmp_path: Path) -> None:
        reg = _make_registry(tmp_path)
        ns = reg.create(
            chat_id=1,
            provider="claude",
            model="opus",
            prompt_preview="hello",
            key=SessionKey.for_transport("sl", 1, 77),
        )
        assert ns.last_prompt == ""
        assert ns.transport == "sl"
        assert ns.topic_id == 77

    def test_mark_running_stores_prompt(self, tmp_path: Path) -> None:
        reg = _make_registry(tmp_path)
        ns = reg.create(chat_id=1, provider="claude", model="opus", prompt_preview="hello")
        reg.mark_running(1, ns.name, "full prompt text here", transport="sl", topic_id=42)
        updated = reg.get(1, ns.name)
        assert updated is not None
        assert updated.status == "running"
        assert updated.last_prompt == "full prompt text here"
        assert updated.transport == "sl"
        assert updated.topic_id == 42

    def test_mark_running_truncates_at_4000(self, tmp_path: Path) -> None:
        reg = _make_registry(tmp_path)
        ns = reg.create(chat_id=1, provider="claude", model="opus", prompt_preview="hi")
        long_prompt = "x" * 5000
        reg.mark_running(1, ns.name, long_prompt)
        updated = reg.get(1, ns.name)
        assert updated is not None
        assert len(updated.last_prompt) == 4000

    def test_mark_running_nonexistent_is_noop(self, tmp_path: Path) -> None:
        reg = _make_registry(tmp_path)
        reg.mark_running(1, "nonexistent", "prompt")  # should not raise


class TestRecoveredRunning:
    def _persist_running_session(
        self,
        tmp_path: Path,
        *,
        name: str = "boldowl",
        chat_id: int = 42,
        last_prompt: str = "do stuff",
        topic_id: int | None = 77,
        transport: str = "sl",
    ) -> Path:
        """Write a JSON file with a running session for reload testing."""
        import json

        path = tmp_path / "named_sessions.json"
        data = {
            "sessions": [
                {
                    "name": name,
                    "chat_id": chat_id,
                    "topic_id": topic_id,
                    "provider": "claude",
                    "model": "opus",
                    "session_id": "sid-123",
                    "prompt_preview": "do stuff",
                    "status": "running",
                    "created_at": time.time(),
                    "message_count": 3,
                    "last_prompt": last_prompt,
                    "transport": transport,
                },
            ],
        }
        path.write_text(json.dumps(data))
        return path

    def test_running_session_downgraded_to_idle(self, tmp_path: Path) -> None:
        path = self._persist_running_session(tmp_path)
        reg = NamedSessionRegistry(path)
        ns = reg.get(42, "boldowl")
        assert ns is not None
        assert ns.status == "idle"

    def test_recovered_running_populated(self, tmp_path: Path) -> None:
        path = self._persist_running_session(tmp_path)
        reg = NamedSessionRegistry(path)
        recovered = reg.pop_recovered_running()
        assert len(recovered) == 1
        assert recovered[0].name == "boldowl"
        assert recovered[0].status == "idle"
        assert recovered[0].last_prompt == "do stuff"
        assert recovered[0].transport == "sl"
        assert recovered[0].topic_id == 77

    def test_pop_clears_recovered(self, tmp_path: Path) -> None:
        path = self._persist_running_session(tmp_path)
        reg = NamedSessionRegistry(path)
        first = reg.pop_recovered_running()
        assert len(first) == 1
        second = reg.pop_recovered_running()
        assert len(second) == 0

    def test_pop_filtered_by_chat_id(self, tmp_path: Path) -> None:
        path = self._persist_running_session(tmp_path, chat_id=42)
        reg = NamedSessionRegistry(path)
        assert len(reg.pop_recovered_running(chat_id=99)) == 0
        assert len(reg.pop_recovered_running(chat_id=42)) == 1

    def test_pop_filtered_by_transport(self, tmp_path: Path) -> None:
        path = self._persist_running_session(tmp_path, transport="sl")
        reg = NamedSessionRegistry(path)
        assert len(reg.pop_recovered_running(transport="tg")) == 0
        assert len(reg.pop_recovered_running(transport="sl")) == 1

    @pytest.mark.parametrize("name", ["ia-sub1", "ia.main.t3892.xf47692", "ia.main.t10.x255106fd"])
    def test_ia_sessions_excluded(self, tmp_path: Path, name: str) -> None:
        path = self._persist_running_session(tmp_path, name=name)
        reg = NamedSessionRegistry(path)
        assert len(reg.pop_recovered_running()) == 0

    def test_last_prompt_round_trip(self, tmp_path: Path) -> None:
        """last_prompt survives persist -> reload."""
        reg = _make_registry(tmp_path)
        ns = reg.create(chat_id=1, provider="claude", model="opus", prompt_preview="hello")
        reg.mark_running(1, ns.name, "my prompt")

        reg2 = NamedSessionRegistry(tmp_path / "named_sessions.json")
        recovered = reg2.pop_recovered_running()
        assert len(recovered) == 1
        assert recovered[0].last_prompt == "my prompt"


class TestReasoningEffort:
    def test_create_stores_effort(self, tmp_path: Path) -> None:
        reg = _make_registry(tmp_path)
        ns = reg.create(
            chat_id=1,
            provider="claude",
            model="opus",
            prompt_preview="hi",
            reasoning_effort="high",
        )
        assert ns.reasoning_effort == "high"

    def test_effort_survives_reload(self, tmp_path: Path) -> None:
        reg = _make_registry(tmp_path)
        reg.create(
            chat_id=1,
            provider="claude",
            model="opus",
            prompt_preview="hi",
            reasoning_effort="high",
        )
        reg2 = _make_registry(tmp_path)  # reload from disk
        loaded = next(iter(reg2._sessions.values()))
        assert loaded.reasoning_effort == "high"
