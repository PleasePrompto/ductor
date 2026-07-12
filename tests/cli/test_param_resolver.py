"""Tests for CLI parameter and model resolution."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ductor_bot.cli.codex_cache import CodexModelCache
from ductor_bot.cli.codex_discovery import CodexModelInfo
from ductor_bot.cli.param_resolver import (
    TaskOverrides,
    resolve_cli_config,
)
from ductor_bot.config import (
    AgentConfig,
    CLIParametersConfig,
    reset_gemini_models,
    set_gemini_models,
)
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


@pytest.fixture(autouse=True)
def _reset_gemini_models() -> None:
    reset_gemini_models()


def test_resolve_global_only(base_config: AgentConfig, codex_cache: CodexModelCache) -> None:
    """Should resolve using only global config when no overrides."""
    result = resolve_cli_config(base_config, codex_cache)

    assert result.provider == "claude"
    assert result.model == "sonnet"
    assert result.reasoning_effort == "medium"  # claude carries the global default
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
        model="gpt-4o",
        reasoning_effort="low",
    )

    result = resolve_cli_config(base_config, codex_cache, task_overrides=overrides)

    assert result.provider == "codex"
    assert result.model == "gpt-4o"
    assert result.reasoning_effort == "low"
    assert result.cli_parameters == []


def test_resolve_merge_parameters(base_config: AgentConfig, codex_cache: CodexModelCache) -> None:
    """Should append task-specific CLI parameters after global provider parameters."""
    base_config.cli_parameters = CLIParametersConfig(claude=["--global-param", "global-value"])
    overrides = TaskOverrides(
        cli_parameters=["--task-param", "task-value"],
    )

    result = resolve_cli_config(base_config, codex_cache, task_overrides=overrides)

    assert result.cli_parameters == [
        "--global-param",
        "global-value",
        "--task-param",
        "task-value",
    ]


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


def test_resolve_rejects_unsupported_task_provider(
    base_config: AgentConfig, codex_cache: CodexModelCache
) -> None:
    """Cron/webhook one-shot execution supports only explicit task providers."""
    overrides = TaskOverrides(
        provider="antigravity",
        model="antigravity-default",
    )

    with pytest.raises(DuctorError, match="Unsupported task provider: antigravity"):
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
    """Explicit invalid reasoning override must fail loudly."""
    overrides = TaskOverrides(
        provider="codex",
        model="gpt-4o-mini",
        reasoning_effort="high",  # Attempt to set, but model doesn't support
    )

    with pytest.raises(DuctorError, match="Invalid reasoning effort"):
        resolve_cli_config(base_config, codex_cache, task_overrides=overrides)


def test_resolve_claude_carries_reasoning(
    base_config: AgentConfig, codex_cache: CodexModelCache
) -> None:
    """Claude carries reasoning_effort (delivered to the CLI via --effort)."""
    overrides = TaskOverrides(
        reasoning_effort="high",
    )

    result = resolve_cli_config(base_config, codex_cache, task_overrides=overrides)

    assert result.provider == "claude"
    assert result.reasoning_effort == "high"


def test_resolve_claude_rejects_invalid_reasoning(
    base_config: AgentConfig, codex_cache: CodexModelCache
) -> None:
    """An explicit unsupported effort for Claude raises (max is valid, foo is not)."""
    from ductor_bot.errors import DuctorError

    ok = resolve_cli_config(
        base_config, codex_cache, task_overrides=TaskOverrides(reasoning_effort="max")
    )
    assert ok.reasoning_effort == "max"  # claude-specific top level

    import pytest

    with pytest.raises(DuctorError):
        resolve_cli_config(
            base_config, codex_cache, task_overrides=TaskOverrides(reasoning_effort="foo")
        )


def test_resolve_gemini_model_from_discovery(
    base_config: AgentConfig, codex_cache: CodexModelCache
) -> None:
    set_gemini_models(frozenset({"gemini-2.5-pro"}))
    overrides = TaskOverrides(provider="gemini", model="gemini-2.5-pro")

    result = resolve_cli_config(base_config, codex_cache, task_overrides=overrides)

    assert result.provider == "gemini"
    assert result.model == "gemini-2.5-pro"


def test_resolve_gemini_invalid_against_discovered_models(
    base_config: AgentConfig, codex_cache: CodexModelCache
) -> None:
    set_gemini_models(frozenset({"gemini-2.5-pro"}))
    overrides = TaskOverrides(provider="gemini", model="gemini-3-pro-preview")

    with pytest.raises(DuctorError, match="Invalid Gemini model"):
        resolve_cli_config(base_config, codex_cache, task_overrides=overrides)


def test_resolve_gemini_fallback_prefix_when_no_discovery(
    base_config: AgentConfig, codex_cache: CodexModelCache
) -> None:
    overrides = TaskOverrides(provider="gemini", model="gemini-foo")

    result = resolve_cli_config(base_config, codex_cache, task_overrides=overrides)

    assert result.provider == "gemini"
    assert result.model == "gemini-foo"


def test_resolve_merges_base_claude_params(
    base_config: AgentConfig, codex_cache: CodexModelCache
) -> None:
    """Base claude bucket MUST concat with overrides, in order: base first, override second."""
    merged = base_config.model_copy(
        update={
            "cli_parameters": CLIParametersConfig(claude=["--mcp-config", "brave.json"]),
        }
    )
    overrides = TaskOverrides(cli_parameters=["--debug"])

    result = resolve_cli_config(merged, codex_cache, task_overrides=overrides)

    assert result.provider == "claude"
    assert result.cli_parameters == ["--mcp-config", "brave.json", "--debug"]


def test_resolve_picks_correct_provider_bucket(
    base_config: AgentConfig, codex_cache: CodexModelCache
) -> None:
    """Per-provider bucket selection MUST NOT leak claude/gemini flags into codex spawn."""
    merged = base_config.model_copy(
        update={
            "cli_parameters": CLIParametersConfig(
                claude=["--claude-only"],
                codex=["--codex-only"],
                gemini=["--gemini-only"],
            ),
        }
    )
    overrides = TaskOverrides(provider="codex", model="gpt-4o")

    result = resolve_cli_config(merged, codex_cache, task_overrides=overrides)

    assert result.provider == "codex"
    assert result.cli_parameters == ["--codex-only"]


def test_resolve_gemini_bucket_with_overrides(
    base_config: AgentConfig, codex_cache: CodexModelCache
) -> None:
    """Gemini bucket + override concat; claude bucket MUST NOT leak when provider=gemini."""
    set_gemini_models(frozenset({"gemini-2.5-pro"}))
    merged = base_config.model_copy(
        update={
            "cli_parameters": CLIParametersConfig(
                claude=["IGNORED"],
                gemini=["--thinking-budget", "4096"],
            ),
        }
    )
    overrides = TaskOverrides(
        provider="gemini",
        model="gemini-2.5-pro",
        cli_parameters=["--show-thoughts"],
    )

    result = resolve_cli_config(merged, codex_cache, task_overrides=overrides)

    assert result.provider == "gemini"
    assert result.cli_parameters == ["--thinking-budget", "4096", "--show-thoughts"]


def test_resolve_missing_bucket_falls_back_to_empty(
    base_config: AgentConfig, codex_cache: CodexModelCache
) -> None:
    """Forward-compat: future provider addition without matching bucket must not AttributeError."""
    # Simulate a future provider scenario by swapping cli_parameters for a stub that
    # lacks the resolved provider's attribute. AgentConfig.model_construct bypasses
    # pydantic validation so the stub is accepted as-is; the getattr+None fallback
    # in resolve_cli_config must keep this from raising.
    stub = SimpleNamespace(codex=["x"], gemini=["y"])  # no 'claude' field
    broken = AgentConfig.model_construct(**{**base_config.model_dump(), "cli_parameters": stub})

    result = resolve_cli_config(broken, codex_cache)

    assert result.provider == "claude"
    assert result.cli_parameters == []
