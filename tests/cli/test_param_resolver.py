"""Tests for CLI parameter and model resolution."""

from __future__ import annotations

import pytest

from ductor_bot.cli.codex_cache import CodexModelCache
from ductor_bot.cli.codex_discovery import CodexModelInfo
from ductor_bot.cli.param_resolver import (
    TaskOverrides,
    resolve_cli_config,
)
from ductor_bot.config import AgentConfig
from ductor_bot.errors import DuctorError


@pytest.fixture
def base_config() -> AgentConfig:
    """Default AgentConfig for testing."""
    return AgentConfig(
        provider="claude",
        model="sonnet",
        ductor_home="~/ductor",
        permission_mode="normal",
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
            CodexModelInfo(
                id="gpt-4o-mini",
                display_name="GPT-4o Mini",
                description="GPT-4o Mini model (no reasoning)",
                supported_efforts=(),
                default_effort="",
                is_default=False,
            ),
        ],
    )


def test_resolve_global_only(base_config: AgentConfig, codex_cache: CodexModelCache) -> None:
    """Should resolve using only global config when no overrides."""
    result = resolve_cli_config(base_config, codex_cache)

    assert result.provider == "claude"
    assert result.model == "sonnet"
    assert result.reasoning_effort == ""
    assert result.cli_parameters == []
    assert result.permission_mode == "normal"
    assert result.working_dir == "~/ductor"
    assert result.file_access == "all"


def test_resolve_with_task_overrides(
    base_config: AgentConfig, codex_cache: CodexModelCache
) -> None:
    """Should apply task overrides over global config."""
    overrides = TaskOverrides(
        provider="codex",
        model="gpt-4o-mini",
        reasoning_effort="low",
    )

    result = resolve_cli_config(base_config, codex_cache, task_overrides=overrides)

    assert result.provider == "codex"
    assert result.model == "gpt-4o-mini"
    # gpt-4o-mini doesn't support reasoning, should be empty
    assert result.reasoning_effort == ""
    assert result.cli_parameters == []


def test_resolve_merge_parameters(base_config: AgentConfig, codex_cache: CodexModelCache) -> None:
    """Should use task-specific CLI parameters."""
    overrides = TaskOverrides(
        cli_parameters=["--task-param", "task-value"],
    )

    result = resolve_cli_config(base_config, codex_cache, task_overrides=overrides)

    # Should contain task params (no global provider-specific params in flat config)
    assert result.cli_parameters == ["--task-param", "task-value"]


def test_resolve_invalid_claude_model(
    base_config: AgentConfig, codex_cache: CodexModelCache
) -> None:
    """Should raise error for invalid Claude model."""
    overrides = TaskOverrides(model="invalid-model")

    with pytest.raises(DuctorError, match="Invalid Claude model"):
        resolve_cli_config(base_config, codex_cache, task_overrides=overrides)


def test_resolve_invalid_codex_model(
    base_config: AgentConfig, codex_cache: CodexModelCache
) -> None:
    """Should raise error for invalid Codex model."""
    overrides = TaskOverrides(
        provider="codex",
        model="nonexistent-model",
    )

    with pytest.raises(DuctorError, match="Invalid Codex model"):
        resolve_cli_config(base_config, codex_cache, task_overrides=overrides)


def test_resolve_codex_reasoning_effort(
    base_config: AgentConfig, codex_cache: CodexModelCache
) -> None:
    """Should validate and apply reasoning effort for Codex models."""
    overrides = TaskOverrides(
        provider="codex",
        model="gpt-4o",
        reasoning_effort="high",
    )

    result = resolve_cli_config(base_config, codex_cache, task_overrides=overrides)

    assert result.provider == "codex"
    assert result.model == "gpt-4o"
    assert result.reasoning_effort == "high"


def test_resolve_codex_effort_fallback(
    base_config: AgentConfig, codex_cache: CodexModelCache
) -> None:
    """Should fall back to empty reasoning effort for non-reasoning models."""
    overrides = TaskOverrides(
        provider="codex",
        model="gpt-4o-mini",
        reasoning_effort="high",  # Attempt to set, but model doesn't support
    )

    result = resolve_cli_config(base_config, codex_cache, task_overrides=overrides)

    assert result.model == "gpt-4o-mini"
    assert result.reasoning_effort == ""


def test_resolve_claude_ignores_reasoning(
    base_config: AgentConfig, codex_cache: CodexModelCache
) -> None:
    """Should ignore reasoning_effort for Claude provider."""
    overrides = TaskOverrides(
        reasoning_effort="high",  # Should be ignored for Claude
    )

    result = resolve_cli_config(base_config, codex_cache, task_overrides=overrides)

    assert result.provider == "claude"
    assert result.reasoning_effort == ""
