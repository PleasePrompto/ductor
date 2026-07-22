"""Central authority for CLI parameter and model resolution."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from ductor_bot.errors import DuctorError

if TYPE_CHECKING:
    from ductor_bot.cli.codex_cache import CodexModelCache
    from ductor_bot.config import AgentConfig

from ductor_bot.config import (
    _GEMINI_ALIASES,
    CLAUDE_MODELS,
    CLAUDE_SUPPORTED_EFFORTS,
    GROK_MODELS,
    GROK_SUPPORTED_EFFORTS,
    get_gemini_models,
)

_TASK_PROVIDERS: frozenset[str] = frozenset({"claude", "codex", "gemini", "grok"})


def _looks_like_gemini_model(model: str) -> bool:
    return model.startswith(("gemini-", "auto-gemini-"))


def _validate_gemini_model(model: str) -> None:
    gemini_models = get_gemini_models()
    if model in _GEMINI_ALIASES:
        return
    if gemini_models and model not in gemini_models:
        msg = f"Invalid Gemini model: {model}. Must be one of {sorted(gemini_models)}"
        raise DuctorError(msg)
    if not gemini_models and not _looks_like_gemini_model(model):
        msg = (
            f"Invalid Gemini model: {model}. Must use a Gemini model ID "
            "(e.g. gemini-2.5-pro) or Gemini alias."
        )
        raise DuctorError(msg)


def _validate_task_provider(provider: str) -> None:
    if provider in _TASK_PROVIDERS:
        return

    supported = ", ".join(sorted(_TASK_PROVIDERS))
    msg = f"Unsupported task provider: {provider}. Supported providers: {supported}"
    raise DuctorError(msg)


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


def _static_effort(
    requested: str,
    supported: tuple[str, ...],
    provider_label: str,
    model: str,
    *,
    explicit: bool,
) -> str:
    """Validate an effort against a static support list (Claude / Grok)."""
    if requested in supported:
        return requested
    if explicit:
        msg = (
            f"Invalid reasoning effort '{requested}' for {provider_label} model {model}. "
            f"Supported: {', '.join(supported)}"
        )
        raise DuctorError(msg)
    return ""


def _resolve_reasoning_effort(
    provider: str,
    model: str,
    overrides: TaskOverrides,
    base_config: AgentConfig,
    codex_cache: CodexModelCache | None,
) -> str:
    """Resolve the reasoning effort delivered to the CLI.

    Codex and Claude carry the effort (override or global default); other
    providers drop it. An explicit, unsupported override raises DuctorError.
    """
    requested_effort = overrides.reasoning_effort or base_config.reasoning_effort
    if not requested_effort or requested_effort == "default":
        return ""

    explicit = overrides.reasoning_effort is not None
    if provider == "claude":
        return _static_effort(
            requested_effort, CLAUDE_SUPPORTED_EFFORTS, "Claude", model, explicit=explicit
        )
    if provider == "grok":
        return _static_effort(
            requested_effort, GROK_SUPPORTED_EFFORTS, "Grok", model, explicit=explicit
        )

    if provider == "codex" and codex_cache:
        model_info = codex_cache.get_model(model)
        if (
            model_info
            and model_info.supported_efforts
            and requested_effort in model_info.supported_efforts
        ):
            return requested_effort
        if overrides.reasoning_effort is not None:
            supported_display = ", ".join(model_info.supported_efforts) if model_info else "none"
            msg = (
                f"Invalid reasoning effort '{requested_effort}' for Codex model {model}. "
                f"Supported: {supported_display}"
            )
            raise DuctorError(msg)
    return ""


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
    _validate_task_provider(provider)

    # 2. Resolve model
    model = overrides.model or base_config.model

    # 3. Validate model
    if provider == "claude":
        if model not in CLAUDE_MODELS:
            msg = f"Invalid Claude model: {model}. Must be one of {sorted(CLAUDE_MODELS)}"
            raise DuctorError(msg)
    elif provider == "gemini":
        _validate_gemini_model(model)
    elif provider == "grok":
        from ductor_bot.config import get_grok_models

        known = GROK_MODELS | get_grok_models()
        if model not in known and not model.startswith("grok-"):
            msg = (
                f"Invalid Grok model: {model}. Must be one of {sorted(known)} or a grok-* model ID"
            )
            raise DuctorError(msg)
    else:  # codex
        if codex_cache is None:
            msg = "Codex cache is required for Codex model validation"
            raise DuctorError(msg)
        if not codex_cache.validate_model(model):
            msg = f"Invalid Codex model: {model}"
            raise DuctorError(msg)

    # 4. Resolve reasoning effort (Codex, Claude, Grok carry it; others drop it).
    reasoning_effort = _resolve_reasoning_effort(
        provider, model, overrides, base_config, codex_cache
    )

    # 5. Merge CLI parameters: base per-provider bucket first, task overrides second.
    #    argparse-style resolution — last flag wins at the CLI level.
    #    `base_config.cli_parameters` is a CLIParametersConfig BaseModel with
    #    per-provider fields. Mirrors the foreground pattern in orchestrator/core.py.
    #    getattr+None fallback keeps this forward-compatible if a new provider is
    #    added without a matching bucket.
    base_params = getattr(base_config.cli_parameters, provider, None) or []
    cli_parameters = [*base_params, *overrides.cli_parameters]

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
