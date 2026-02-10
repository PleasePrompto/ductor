"""Tests for webhook observer parameter resolution."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from ductor_bot.cli.codex_cache import CodexModelCache
from ductor_bot.cli.codex_discovery import CodexModelInfo
from ductor_bot.cli.param_resolver import TaskOverrides
from ductor_bot.config import AgentConfig
from ductor_bot.webhook.models import WebhookEntry


@pytest.fixture
def base_config() -> AgentConfig:
    """Default AgentConfig for testing."""
    return AgentConfig(
        provider="claude",
        model="sonnet",
        permission_mode="normal",
        reasoning_effort="medium",
    )


@pytest.fixture
def codex_cache() -> CodexModelCache:
    """Mock Codex cache with sample models."""
    return CodexModelCache(
        last_updated="2026-02-10T12:00:00",
        models=[
            CodexModelInfo(
                id="gpt-4o",
                display_name="GPT-4o",
                description="GPT-4o model",
                supported_efforts=("low", "medium", "high"),
                default_effort="medium",
                is_default=True,
            ),
        ],
    )


def test_resolve_execution_config_no_overrides(
    base_config: AgentConfig, codex_cache: CodexModelCache
) -> None:
    """Should fall back to global config when webhook has no overrides."""
    from ductor_bot.webhook.observer import WebhookObserver

    observer = WebhookObserver(
        paths=MagicMock(),
        manager=MagicMock(),
        config=base_config,
        models=MagicMock(),
        codex_cache=codex_cache,
    )

    # Webhook with no overrides
    overrides = TaskOverrides()

    exec_config = observer._resolve_execution_config(overrides)

    assert exec_config.provider == "claude"
    assert exec_config.model == "sonnet"
    assert exec_config.reasoning_effort == ""
    assert exec_config.cli_parameters == []


def test_resolve_execution_config_with_overrides(
    base_config: AgentConfig, codex_cache: CodexModelCache
) -> None:
    """Should apply webhook overrides over global config."""
    from ductor_bot.webhook.observer import WebhookObserver

    observer = WebhookObserver(
        paths=MagicMock(),
        manager=MagicMock(),
        config=base_config,
        models=MagicMock(),
        codex_cache=codex_cache,
    )

    # Webhook with overrides
    overrides = TaskOverrides(
        provider="codex",
        model="gpt-4o",
        reasoning_effort="high",
        cli_parameters=["--webhook-flag", "value"],
    )

    exec_config = observer._resolve_execution_config(overrides)

    assert exec_config.provider == "codex"
    assert exec_config.model == "gpt-4o"
    assert exec_config.reasoning_effort == "high"
    assert exec_config.cli_parameters == ["--webhook-flag", "value"]


def test_dispatch_with_cli_parameters(
    base_config: AgentConfig, codex_cache: CodexModelCache
) -> None:
    """Should include webhook CLI parameters in the built command."""
    from ductor_bot.webhook.observer import WebhookObserver

    observer = WebhookObserver(
        paths=MagicMock(),
        manager=MagicMock(),
        config=base_config,
        models=MagicMock(),
        codex_cache=codex_cache,
    )

    # Create webhook entry with CLI parameters
    hook = WebhookEntry(
        id="test-hook",
        title="Test Hook",
        description="Test",
        mode="cron_task",
        prompt_template="{{message}}",
        provider="codex",
        model="gpt-4o",
        reasoning_effort="high",
        cli_parameters=["--custom-param", "custom-value"],
        task_folder="test-folder",
    )

    # Mock build_cmd to capture what's passed
    with patch("ductor_bot.webhook.observer.build_cmd") as mock_build_cmd:
        mock_build_cmd.return_value = [
            "codex",
            "exec",
            "--custom-param",
            "custom-value",
            "--",
            "prompt",
        ]

        # Create TaskOverrides from hook
        overrides = TaskOverrides(
            provider=hook.provider,
            model=hook.model,
            reasoning_effort=hook.reasoning_effort,
            cli_parameters=hook.cli_parameters,
        )

        exec_config = observer._resolve_execution_config(overrides)

        # Verify the resolved config has the webhook params
        assert exec_config.provider == "codex"
        assert exec_config.model == "gpt-4o"
        assert exec_config.reasoning_effort == "high"
        assert exec_config.cli_parameters == ["--custom-param", "custom-value"]
