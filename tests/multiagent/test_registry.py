"""Tests for multiagent/registry.py: AgentRegistry load."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ductor_bot.multiagent.registry import AgentRegistry


@pytest.fixture
def agents_path(tmp_path: Path) -> Path:
    return tmp_path / "agents.json"


class TestRegistryLoad:
    """Test AgentRegistry.load() behavior."""

    def test_missing_file_returns_empty(self, agents_path: Path) -> None:
        reg = AgentRegistry(agents_path)
        assert reg.load() == []

    def test_valid_json_array(self, agents_path: Path) -> None:
        data = [
            {"name": "sub1", "telegram_token": "tok:1"},
            {"name": "sub2", "telegram_token": "tok:2", "provider": "codex"},
        ]
        agents_path.write_text(json.dumps(data))
        reg = AgentRegistry(agents_path)
        agents = reg.load()
        assert len(agents) == 2
        assert agents[0].name == "sub1"
        assert agents[1].provider == "codex"

    def test_corrupt_json_returns_empty(self, agents_path: Path) -> None:
        agents_path.write_text("{not valid json")
        reg = AgentRegistry(agents_path)
        assert reg.load() == []

    def test_non_array_json_returns_empty(self, agents_path: Path) -> None:
        agents_path.write_text('{"name": "sub1"}')
        reg = AgentRegistry(agents_path)
        assert reg.load() == []

    def test_empty_array_returns_empty(self, agents_path: Path) -> None:
        agents_path.write_text("[]")
        reg = AgentRegistry(agents_path)
        assert reg.load() == []

    def test_invalid_entry_is_skipped(self, agents_path: Path) -> None:
        data = [
            {"name": "sub1", "telegram_token": "tok:1"},
            {"invalid": "missing name and token"},
            {"name": "sub3", "telegram_token": "tok:3"},
        ]
        agents_path.write_text(json.dumps(data))
        reg = AgentRegistry(agents_path)
        agents = reg.load()
        assert len(agents) == 2
        assert agents[0].name == "sub1"
        assert agents[1].name == "sub3"
