"""Application configuration and model registry."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class StreamingConfig(BaseModel):
    """Settings for streaming response output."""

    enabled: bool = True
    min_chars: int = 200
    max_chars: int = 4000
    idle_ms: int = 800
    edit_interval_seconds: float = 2.0
    max_edit_failures: int = 3
    append_mode: bool = False
    sentence_break: bool = True


class DockerConfig(BaseModel):
    """Settings for Docker-based CLI sandboxing."""

    enabled: bool = False
    image_name: str = "ductor-sandbox"
    container_name: str = "ductor-sandbox"
    auto_build: bool = True


_DEFAULT_HEARTBEAT_PROMPT = (
    "You are running as a background heartbeat check. Review the current workspace context:\n"
    "- Read memory_system/MAINMEMORY.md for user interests and personality\n"
    "- Check cron_tasks/ for active projects\n"
    "- Think about what might be useful, interesting, or fun for the user\n"
    "\n"
    "If you have a creative idea, suggestion, interesting fact, or something the user might enjoy:\n"
    "Reply with your message directly.\n"
    "\n"
    "If nothing needs attention right now:\n"
    "Reply exactly: HEARTBEAT_OK"
)


class HeartbeatConfig(BaseModel):
    """Settings for the periodic heartbeat system."""

    enabled: bool = False
    interval_minutes: int = 30
    cooldown_minutes: int = 5
    quiet_start: int = 21
    quiet_end: int = 8
    prompt: str = _DEFAULT_HEARTBEAT_PROMPT
    ack_token: str = "HEARTBEAT_OK"  # noqa: S105


class CleanupConfig(BaseModel):
    """Settings for automatic file cleanup of workspace directories."""

    enabled: bool = True
    telegram_files_days: int = 30
    output_to_user_days: int = 30
    check_hour: int = 3


class CLIParametersConfig(BaseModel):
    """CLI parameters for main agent."""

    claude: list[str] = Field(default_factory=list)
    codex: list[str] = Field(default_factory=list)


class WebhookConfig(BaseModel):
    """Settings for the webhook HTTP server."""

    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8742
    token: str = ""
    max_body_bytes: int = 262144
    rate_limit_per_minute: int = 30


def deep_merge_config(
    user: dict[str, object],
    defaults: dict[str, object],
) -> tuple[dict[str, object], bool]:
    """Recursively merge *defaults* into *user*, preserving user values.

    Returns ``(merged_dict, changed)`` where *changed* is True when new keys were added.
    """
    result: dict[str, object] = dict(user)
    changed = False
    new_keys = 0
    for key, default_val in defaults.items():
        if key not in result:
            result[key] = default_val
            changed = True
            new_keys += 1
        elif isinstance(default_val, dict) and isinstance(result[key], dict):
            sub_merged, sub_changed = deep_merge_config(
                result[key],  # type: ignore[arg-type]
                default_val,
            )
            result[key] = sub_merged
            changed = changed or sub_changed
    if new_keys:
        logger.info("Config deep-merge: %d new keys added", new_keys)
    return result, changed


def update_config_file(config_path: Path, **updates: object) -> None:
    """Update specific keys in config.json without overwriting other user settings."""
    data: dict[str, object] = json.loads(config_path.read_text(encoding="utf-8"))
    data.update(updates)
    config_path.write_text(
        json.dumps(data, indent=2, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )
    logger.info("Persisted config update: %s", ", ".join(f"{k}={v}" for k, v in updates.items()))


async def update_config_file_async(config_path: Path, **updates: object) -> None:
    """Async wrapper: update config.json without blocking the event loop."""
    import asyncio

    await asyncio.to_thread(update_config_file, config_path, **updates)


class AgentConfig(BaseModel):
    """Top-level configuration loaded from config.json."""

    log_level: str = "INFO"
    provider: str = "claude"
    model: str = "opus"
    ductor_home: str = "~/.ductor"
    idle_timeout_minutes: int = 1440
    session_age_warning_hours: int = 12
    daily_reset_hour: int = 4
    daily_reset_enabled: bool = False
    max_budget_usd: float | None = None
    max_turns: int | None = None
    max_session_messages: int | None = None
    permission_mode: str = "bypassPermissions"
    cli_timeout: float = 600.0
    reasoning_effort: str = "medium"
    file_access: str = "all"
    streaming: StreamingConfig = Field(default_factory=StreamingConfig)
    docker: DockerConfig = Field(default_factory=DockerConfig)
    heartbeat: HeartbeatConfig = Field(default_factory=HeartbeatConfig)
    cleanup: CleanupConfig = Field(default_factory=CleanupConfig)
    webhooks: WebhookConfig = Field(default_factory=WebhookConfig)
    cli_parameters: CLIParametersConfig = Field(default_factory=CLIParametersConfig)
    user_timezone: str = ""
    telegram_token: str = ""
    allowed_user_ids: list[int] = Field(default_factory=list)


def resolve_user_timezone(configured: str = "") -> ZoneInfo:
    """Resolve timezone: config value -> host system -> UTC.

    Returns a ``ZoneInfo`` instance. Invalid or empty *configured* values
    fall through to the host OS timezone, then to UTC as last resort.
    """
    trimmed = configured.strip()
    if trimmed:
        try:
            return ZoneInfo(trimmed)
        except (ZoneInfoNotFoundError, KeyError):
            logger.warning("Invalid user_timezone '%s', falling back to host/UTC", trimmed)

    # Try host system timezone via /etc/localtime symlink (Linux).
    import os

    tz_env = os.environ.get("TZ", "").strip()
    if tz_env:
        try:
            return ZoneInfo(tz_env)
        except (ZoneInfoNotFoundError, KeyError):
            pass

    localtime = Path("/etc/localtime")
    if localtime.is_symlink():
        target = str(localtime.resolve())
        # /usr/share/zoneinfo/Europe/Berlin -> Europe/Berlin
        marker = "/zoneinfo/"
        idx = target.find(marker)
        if idx != -1:
            candidate = target[idx + len(marker) :]
            try:
                return ZoneInfo(candidate)
            except (ZoneInfoNotFoundError, KeyError):
                pass

    return ZoneInfo("UTC")


_CLAUDE_MODELS: frozenset[str] = frozenset({"haiku", "sonnet", "opus"})

_MODEL_EQUIVALENCE: dict[str, str] = {
    "opus": "gpt-5.2-codex",
    "sonnet": "gpt-5.1-codex-mini",
    "haiku": "gpt-5.1-codex-mini",
    "gpt-5.2-codex": "opus",
    "gpt-5.1-codex-max": "opus",
    "gpt-5.1-codex-mini": "sonnet",
    "gpt-5.2": "opus",
    "gpt-5.3-codex": "opus",
}


class ModelRegistry:
    """Provider resolution for models.

    Claude models (haiku, sonnet, opus) are hardcoded.
    Codex models are discovered dynamically at runtime.
    """

    @staticmethod
    def provider_for(model_id: str) -> str:
        """Return the provider for a model ID."""
        if model_id in _CLAUDE_MODELS:
            return "claude"
        return "codex"

    def resolve_for_provider(
        self,
        model_name: str,
        available_providers: frozenset[str],
    ) -> tuple[str, str]:
        """Resolve *model_name* to ``(model_id, provider)`` with fallback."""
        native_provider = self.provider_for(model_name)

        if native_provider in available_providers:
            return model_name, native_provider

        equivalent = _MODEL_EQUIVALENCE.get(model_name)
        if equivalent:
            eq_provider = self.provider_for(equivalent)
            if eq_provider in available_providers:
                logger.info(
                    "Model fallback: %s (%s) -> %s (%s)",
                    model_name,
                    native_provider,
                    equivalent,
                    eq_provider,
                )
                return equivalent, eq_provider

        fallback_provider = next(iter(available_providers), None)
        if fallback_provider:
            fallback_model = "opus" if fallback_provider == "claude" else model_name
            logger.warning(
                "No equivalent for '%s', falling back to %s (%s)",
                model_name,
                fallback_model,
                fallback_provider,
            )
            return fallback_model, fallback_provider

        msg = f"No available provider for model '{model_name}'"
        raise ValueError(msg)
