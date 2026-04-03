"""Tests for multiagent/models.py: SubAgentConfig and merge_sub_agent_config."""

from __future__ import annotations

from pathlib import Path

from ductor_bot.config import AgentConfig, ApiConfig
from ductor_bot.multiagent.models import SubAgentConfig, merge_sub_agent_config


class TestSubAgentConfig:
    """Test SubAgentConfig model validation and defaults."""

    def test_minimal_config(self) -> None:
        cfg = SubAgentConfig(name="sub1", telegram_token="tok:123")
        assert cfg.name == "sub1"
        assert cfg.telegram_token == "tok:123"
        assert cfg.allowed_user_ids is None
        assert cfg.allowed_group_ids is None
        assert cfg.provider is None
        assert cfg.model is None

    def test_with_overrides(self) -> None:
        cfg = SubAgentConfig(
            name="sub1",
            telegram_token="tok:123",
            allowed_user_ids=["100", "200"],
            provider="codex",
            model="gpt-4",
        )
        assert cfg.allowed_user_ids == [100, 200]
        assert cfg.provider == "codex"
        assert cfg.model == "gpt-4"

    def test_model_dump_excludes_none(self) -> None:
        cfg = SubAgentConfig(name="sub1", telegram_token="tok:123")
        dumped = cfg.model_dump(exclude_none=True)
        assert "provider" not in dumped
        assert "model" not in dumped
        assert "allowed_user_ids" not in dumped


class TestMergeSubAgentConfig:
    """Test merge_sub_agent_config behavior."""

    def _main_config(self) -> AgentConfig:
        return AgentConfig(
            provider="claude",
            model="opus",
            allowed_user_ids=["1", "2", "3"],
            ductor_home="/main/home",
            cli_timeout=600,
            telegram_token="main-token",
        )

    def test_inherits_from_main(self) -> None:
        """Sub-agent inherits main config values when not overridden."""
        main = self._main_config()
        sub = SubAgentConfig(name="sub1", telegram_token="sub-token")
        result = merge_sub_agent_config(main, sub, Path("/agents/sub1"))

        assert result.provider == "claude"
        assert result.model == "opus"
        assert result.cli_timeout == 600

    def test_overrides_from_sub(self) -> None:
        """Sub-agent overrides take precedence over main config."""
        main = self._main_config()
        sub = SubAgentConfig(
            name="sub1",
            telegram_token="sub-token",
            provider="codex",
            model="gpt-4",
            cli_timeout=300,
        )
        result = merge_sub_agent_config(main, sub, Path("/agents/sub1"))

        assert result.provider == "codex"
        assert result.model == "gpt-4"
        assert result.cli_timeout == 300

    def test_ductor_home_always_set_to_agent_home(self) -> None:
        """ductor_home is always the agent's home dir, not main's."""
        main = self._main_config()
        sub = SubAgentConfig(name="sub1", telegram_token="sub-token")
        result = merge_sub_agent_config(main, sub, Path("/agents/sub1"))

        assert result.ductor_home == "/agents/sub1"

    def test_telegram_token_always_from_sub(self) -> None:
        """Telegram token always comes from sub-agent definition."""
        main = self._main_config()
        sub = SubAgentConfig(name="sub1", telegram_token="sub-token")
        result = merge_sub_agent_config(main, sub, Path("/agents/sub1"))

        assert result.telegram_token == "sub-token"

    def test_allowed_user_ids_from_sub(self) -> None:
        """Sub-agent's allowed_user_ids override main config."""
        main = self._main_config()
        sub = SubAgentConfig(
            name="sub1",
            telegram_token="sub-token",
            allowed_user_ids=["100", "200"],
        )
        result = merge_sub_agent_config(main, sub, Path("/agents/sub1"))

        assert result.allowed_user_ids == ["100", "200"]

    def test_allowed_user_ids_none_uses_empty_list(self) -> None:
        """When sub-agent has no allowed_user_ids (None), result is empty list."""
        main = self._main_config()
        sub = SubAgentConfig(name="sub1", telegram_token="sub-token")
        result = merge_sub_agent_config(main, sub, Path("/agents/sub1"))

        # allowed_user_ids=None in sub -> fallback to empty list
        assert result.allowed_user_ids == []

    def test_allowed_user_ids_none_does_not_inherit_main(self) -> None:
        """Sub-agent with allowed_user_ids=None should NOT inherit main's users.

        This is intentional: sub-agents need explicit user lists for security.
        """
        main = self._main_config()
        assert main.allowed_user_ids == ["1", "2", "3"]

        sub = SubAgentConfig(name="sub1", telegram_token="sub-token")
        result = merge_sub_agent_config(main, sub, Path("/agents/sub1"))

        # Should be empty, NOT [1, 2, 3] from main
        assert result.allowed_user_ids == []

    def test_allowed_group_ids_from_sub(self) -> None:
        """Sub-agent's allowed_group_ids override main config."""
        main = self._main_config()
        main.allowed_group_ids = ["-1001", "-1002"]
        sub = SubAgentConfig(
            name="sub1",
            telegram_token="sub-token",
            allowed_group_ids=["-2001"],
        )
        result = merge_sub_agent_config(main, sub, Path("/agents/sub1"))

        assert result.allowed_group_ids == ["-2001"]

    def test_allowed_group_ids_none_uses_empty_list(self) -> None:
        """When sub-agent has no allowed_group_ids (None), result is empty list."""
        main = self._main_config()
        main.allowed_group_ids = ["-1001"]
        sub = SubAgentConfig(name="sub1", telegram_token="sub-token")
        result = merge_sub_agent_config(main, sub, Path("/agents/sub1"))

        # Should be empty, NOT [-1001] from main (same pattern as allowed_user_ids)
        assert result.allowed_group_ids == []

    def test_partial_overrides(self) -> None:
        """Only specified fields override, rest inherit from main."""
        main = self._main_config()
        sub = SubAgentConfig(
            name="sub1",
            telegram_token="sub-token",
            model="sonnet",
        )
        result = merge_sub_agent_config(main, sub, Path("/agents/sub1"))

        assert result.model == "sonnet"
        assert result.provider == "claude"  # inherited

    def test_api_disabled_when_not_overridden(self) -> None:
        """Sub-agent without explicit api config gets api.enabled=False."""
        main = self._main_config()
        main.api = ApiConfig(enabled=True, port=8741)
        sub = SubAgentConfig(name="sub1", telegram_token="sub-token")
        result = merge_sub_agent_config(main, sub, Path("/agents/sub1"))

        assert result.api.enabled is False
        assert result.api.port == 8741  # rest inherited

    def test_api_preserved_when_explicitly_overridden(self) -> None:
        """Sub-agent with explicit api config keeps their settings."""
        main = self._main_config()
        main.api = ApiConfig(enabled=True, port=8741)
        sub = SubAgentConfig(
            name="sub1",
            telegram_token="sub-token",
            api=ApiConfig(enabled=True, port=8742),
        )
        result = merge_sub_agent_config(main, sub, Path("/agents/sub1"))

        assert result.api.enabled is True
        assert result.api.port == 8742
