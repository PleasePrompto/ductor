"""Data models for multi-agent configuration."""

from __future__ import annotations

from pathlib import Path

from pydantic import BaseModel

from ductor_bot.config import (
    AgentConfig,
    ApiConfig,
    CleanupConfig,
    CLIParametersConfig,
    DockerConfig,
    HeartbeatConfig,
    MatrixConfig,
    StreamingConfig,
    WebhookConfig,
)


class SubAgentConfig(BaseModel):
    """Minimal sub-agent definition from agents.json.

    Only ``name`` is strictly required. Telegram agents need ``telegram_token``
    and ``allowed_user_ids``; Matrix agents need ``matrix`` config.
    All other fields are optional and inherit from the main agent config.
    """

    name: str
    transport: str = "telegram"  # "telegram" | "matrix"

    # Telegram credentials (required when transport=telegram)
    telegram_token: str = ""
    allowed_user_ids: list[int] | None = None
    allowed_group_ids: list[int] | None = None

    # Matrix credentials (required when transport=matrix)
    matrix: MatrixConfig | None = None

    # Group behaviour
    group_mention_only: bool | None = None

    # Optional overrides — inherit from main agent if None
    provider: str | None = None
    model: str | None = None
    log_level: str | None = None
    idle_timeout_minutes: int | None = None
    session_age_warning_hours: int | None = None
    daily_reset_hour: int | None = None
    daily_reset_enabled: bool | None = None
    max_budget_usd: float | None = None
    max_turns: int | None = None
    max_session_messages: int | None = None
    permission_mode: str | None = None
    cli_timeout: float | None = None
    reasoning_effort: str | None = None
    file_access: str | None = None
    streaming: StreamingConfig | None = None
    docker: DockerConfig | None = None
    heartbeat: HeartbeatConfig | None = None
    cleanup: CleanupConfig | None = None
    webhooks: WebhookConfig | None = None
    api: ApiConfig | None = None
    cli_parameters: CLIParametersConfig | None = None
    user_timezone: str | None = None


def merge_sub_agent_config(
    main: AgentConfig,
    sub: SubAgentConfig,
    agent_home: Path,
) -> AgentConfig:
    """Create a full AgentConfig by merging main config with sub-agent overrides.

    Merge: main agent defaults → ``agents.json`` explicit overrides (non-None).

    ``switch_model()`` keeps ``agents.json`` up-to-date when the user changes
    model/provider/reasoning_effort in a sub-agent chat, so no extra config
    layer is needed.
    """
    base = main.model_dump()

    # agents.json explicit overrides (non-None fields win)
    overrides = sub.model_dump(exclude_none=True, exclude={"name"})
    base.update(overrides)

    base["ductor_home"] = str(agent_home)
    base["transport"] = sub.transport
    base["transports"] = [sub.transport]
    base["telegram_token"] = sub.telegram_token
    base["allowed_user_ids"] = sub.allowed_user_ids or []
    base["allowed_group_ids"] = sub.allowed_group_ids or []
    if sub.matrix is not None:
        base["matrix"] = sub.matrix.model_dump()

    # Sub-agents don't need the user-facing API server (they use InterAgentBus).
    # Disable it unless the sub-agent explicitly provides an api config.
    if sub.api is None:
        base.setdefault("api", {})["enabled"] = False

    return AgentConfig(**base)
