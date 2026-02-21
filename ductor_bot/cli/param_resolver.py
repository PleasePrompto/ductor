"""Central authority for CLI parameter and model resolution."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ductor_bot.errors import DuctorError

if TYPE_CHECKING:
    from ductor_bot.cli.codex_cache import CodexModelCache
    from ductor_bot.config import AgentConfig

_CLAUDE_MODELS: frozenset[str] = frozenset({"haiku", "sonnet", "opus"})


@dataclass(frozen=True)
class TaskOverrides:
    """Per-task configuration overrides from CronJob or WebhookEntry."""

    provider: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    cli_parameters: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class TaskExecutionConfig:
    """Resolved configuration for a single CLI execution."""

    provider: str
    model: str
    reasoning_effort: str
    cli_parameters: list[str]
    permission_mode: str
    working_dir: str
    file_access: str


def resolve_cli_config(
    base_config: AgentConfig,
    codex_cache: CodexModelCache | None,
    *,
    task_overrides: TaskOverrides | None = None,
) -> TaskExecutionConfig:
    """Merge global config with task overrides, validate, return execution config.

    Logic:
    1. Resolve provider (task override → global config)
    2. Resolve model (task override → global config)
    3. Validate model against cache (Claude hardcoded, Codex from cache)
    4. Resolve reasoning effort (Codex only, validate against model's supported efforts)
    5. Merge CLI parameters (global + task-specific)
    6. Return immutable TaskExecutionConfig

    Args:
        base_config: Global agent configuration
        codex_cache: Codex model cache (optional, required for Codex validation)
        task_overrides: Task-specific overrides (optional)

    Returns:
        TaskExecutionConfig with resolved and validated settings

    Raises:
        DuctorError: If model validation fails
    """
    overrides = task_overrides or TaskOverrides()

    # 1. Resolve provider
    provider = overrides.provider or base_config.provider

    # 2. Resolve model
    model = overrides.model or base_config.model

    # 3. Validate model
    if provider == "claude":
        if model not in _CLAUDE_MODELS:
            msg = f"Invalid Claude model: {model}. Must be one of {sorted(_CLAUDE_MODELS)}"
            raise DuctorError(msg)
    elif provider == "gemini":
        # Gemini models are currently validated by prefix or hardcoded in GeminiCLI
        pass
    else:  # codex
        if codex_cache is None:
            msg = "Codex cache is required for Codex model validation"
            raise DuctorError(msg)
        if not codex_cache.validate_model(model):
            msg = f"Invalid Codex model: {model}"
            raise DuctorError(msg)

    # 4. Resolve reasoning effort (Codex only)
    reasoning_effort = ""
    if provider == "codex":
        requested_effort = overrides.reasoning_effort or base_config.reasoning_effort

        # Check if model supports reasoning and if effort is valid
        if codex_cache and requested_effort:
            model_info = codex_cache.get_model(model)
            if (
                model_info
                and model_info.supported_efforts
                and requested_effort in model_info.supported_efforts
            ):
                reasoning_effort = requested_effort
            # Otherwise, fall back to empty (invalid effort or model doesn't support reasoning)

    # 5. Merge CLI parameters (currently no provider-specific params in flat config)
    cli_parameters = [*overrides.cli_parameters]

    # 6. Return immutable config
    return TaskExecutionConfig(
        provider=provider,
        model=model,
        reasoning_effort=reasoning_effort,
        cli_parameters=cli_parameters,
        permission_mode=base_config.permission_mode,
        working_dir=base_config.ductor_home,
        file_access=base_config.file_access,
    )
