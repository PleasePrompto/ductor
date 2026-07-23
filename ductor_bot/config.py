"""Application configuration and model registry."""

from __future__ import annotations

import json
import logging
from pathlib import Path
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import BaseModel, Field, field_validator, model_validator

logger = logging.getLogger(__name__)
NULLISH_TEXT_VALUES: frozenset[str] = frozenset({"null", "none"})
DEFAULT_EMPTY_GEMINI_API_KEY: str = "null"

# Intentional bind-all: the API is designed for private-network use (Tailscale).
# Public exposure is gated by ``allow_public`` + a prominent warning at startup.
_BIND_ALL_INTERFACES: str = ".".join(["0"] * 4)

# ``tzdata`` is a declared hard dependency, so ``ZoneInfo("UTC")`` cannot fail.
_SAFE_UTC: ZoneInfo = ZoneInfo("UTC")


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
    show_reasoning_stream: bool = False
    show_tool_progress: bool = True
    show_thinking_indicator: bool = True


class DockerConfig(BaseModel):
    """Settings for Docker-based CLI sandboxing."""

    enabled: bool = False
    image_name: str = "ductor-sandbox"
    container_name: str = "ductor-sandbox"
    auto_build: bool = True
    mount_host_cache: bool = False
    mounts: list[str] = Field(default_factory=list)
    published_ports: list[str] = Field(default_factory=list)
    extras: list[str] = Field(default_factory=list)


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

_DEFAULT_HEARTBEAT_ACK = "HEARTBEAT_OK"

_DEFAULT_FLUSH_PROMPT = (
    "## PRE-COMPACTION MEMORY FLUSH\n"
    "The conversation context is about to be compacted. Before that happens: "
    "review the recent conversation and APPEND any durable facts, decisions, "
    "preferences, or learnings to memory_system/MAINMEMORY.md that are not "
    "already captured there. Do NOT overwrite existing entries. If there is "
    "nothing new worth saving, reply exactly: FLUSH_NOOP"
)

_DEFAULT_MEMORY_REFLECTION_PROMPT = (
    "## MEMORY REFLECTION\n"
    "Review the last several messages in this conversation.\n"
    "Check: were there any new decisions, corrections, error solutions, user "
    "preferences, or important facts that you did NOT yet write to memory?\n"
    "If yes -- update memory_system/MAINMEMORY.md silently.\n"
    "If everything is already recorded -- do nothing."
)

_DEFAULT_COMPACT_PROMPT = (
    "## MEMORY COMPACTION\n"
    "memory_system/MAINMEMORY.md has grown large. Rewrite it as follows:\n"
    "1. Preserve entries from the last {preserve_days} days verbatim.\n"
    "2. For older entries, cluster by topic and replace each cluster with "
    "ONE dense entry that preserves all key facts in fewer lines.\n"
    "3. Target size: roughly {target_lines} lines total.\n"
    "4. Do NOT delete facts -- only compress their expression.\n"
    "If MAINMEMORY.md is already at or below {target_lines} lines, reply "
    "exactly: COMPACT_NOOP"
)


class HeartbeatTarget(BaseModel):
    """A specific chat/topic to send heartbeat checks to.

    All optional fields override the global HeartbeatConfig when set.
    """

    enabled: bool = True
    transport: str = "tg"
    chat_id: int | None = None
    topic_id: int | None = None
    prompt: str | None = None
    ack_token: str | None = None
    interval_minutes: int | None = None
    quiet_start: int | None = None
    quiet_end: int | None = None


class HeartbeatConfig(BaseModel):
    """Settings for the periodic heartbeat system."""

    enabled: bool = False
    interval_minutes: int = 30
    cooldown_minutes: int = 5
    quiet_start: int = 21
    quiet_end: int = 8
    prompt: str = _DEFAULT_HEARTBEAT_PROMPT
    ack_token: str = _DEFAULT_HEARTBEAT_ACK
    group_targets: list[HeartbeatTarget] = Field(
        default_factory=lambda: [
            HeartbeatTarget(
                enabled=False,
                chat_id=None,
                topic_id=None,
                prompt="Replace chat_id with your group ID to enable this target.",
            ),
        ]
    )


class CleanupConfig(BaseModel):
    """Settings for automatic file cleanup of workspace directories."""

    enabled: bool = True
    media_files_days: int = 30
    output_to_user_days: int = 30
    api_files_days: int = 30
    check_hour: int = 3

    def __init__(self, **data: object) -> None:
        # Backwards compat: accept old name ``telegram_files_days``.
        if "telegram_files_days" in data and "media_files_days" not in data:
            data["media_files_days"] = data.pop("telegram_files_days")
        elif "telegram_files_days" in data:
            data.pop("telegram_files_days")
        super().__init__(**data)


class MemoryFlushConfig(BaseModel):
    """Settings for the pre-compaction silent memory flush (#77)."""

    enabled: bool = True
    flush_prompt: str = _DEFAULT_FLUSH_PROMPT
    # ``0`` disables the dedup window (flush fires on every boundary).
    dedup_seconds: int = Field(default=300, ge=0)


class MemoryReflectionConfig(BaseModel):
    """Settings for the periodic memory reflection hook (#65)."""

    enabled: bool = False
    # Must be >= 1 to avoid ``ZeroDivisionError`` in modulo check (hooks.py).
    every_n_messages: int = Field(default=10, ge=1)
    prompt: str = _DEFAULT_MEMORY_REFLECTION_PROMPT


class MemoryCompactionConfig(BaseModel):
    """Settings for LLM-driven memory compaction (#80)."""

    enabled: bool = True
    trigger_lines: int = Field(default=70, ge=1)
    target_lines: int = Field(default=40, ge=1)
    # ``0`` disables the "preserve recent entries verbatim" guard.
    preserve_recency_days: int = Field(default=14, ge=0)
    prompt: str = _DEFAULT_COMPACT_PROMPT

    @model_validator(mode="after")
    def _check_target_le_trigger(self) -> MemoryCompactionConfig:
        """``target_lines`` must not exceed ``trigger_lines`` (compaction would be a no-op)."""
        if self.target_lines > self.trigger_lines:
            raise ValueError(
                f"target_lines ({self.target_lines}) must be <= "
                f"trigger_lines ({self.trigger_lines})"
            )
        return self


class ImageConfig(BaseModel):
    """Settings for incoming image processing."""

    max_dimension: int = 2000
    output_format: str = "webp"
    quality: int = 85


class CLIParametersConfig(BaseModel):
    """CLI parameters for main agent."""

    claude: list[str] = Field(default_factory=list)
    codex: list[str] = Field(default_factory=list)
    gemini: list[str] = Field(default_factory=list)
    antigravity: list[str] = Field(default_factory=list)
    grok: list[str] = Field(default_factory=list)


class MatrixConfig(BaseModel):
    """Matrix homeserver connection settings."""

    homeserver: str = ""  # https://matrix.myserver.com
    user_id: str = ""  # @ductor:myserver.com
    password: str = ""  # for initial login
    access_token: str = ""  # persisted after first login
    device_id: str = ""  # persisted after first login
    allowed_rooms: list[str] = Field(default_factory=list)  # ["!abc:server", "#room:server"]
    allowed_users: list[str] = Field(default_factory=list)  # ["@user:server"]
    store_path: str = "matrix_store"  # relative to ductor_home


class SlackConfig(BaseModel):
    """Slack Socket Mode settings."""

    bot_token: str = ""
    app_token: str = ""
    allowed_channels: list[str] = Field(default_factory=list)
    allowed_users: list[str] = Field(default_factory=list)


class TasksConfig(BaseModel):
    """Settings for background task delegation."""

    enabled: bool = True
    max_parallel: int = 5
    timeout_seconds: float = 3600.0
    finished_retention_hours: int = 168
    finished_keep_last: int = 100


class CronDeliveryRetryConfig(BaseModel):
    """Retry delivery of preserved cron results without rerunning the agent."""

    enabled: bool = False
    interval_seconds: int = Field(default=300, ge=1)
    max_attempts: int = Field(default=12, ge=1)


class CronPreflightConfig(BaseModel):
    """Task-local deterministic gate that can skip a cron agent run."""

    enabled: bool = False
    timeout_seconds: float = Field(default=15.0, gt=0)
    skip_marker: str = "HEARTBEAT_OK"


class TimeoutConfig(BaseModel):
    """Per-execution-path timeout settings."""

    normal: float = 600.0
    background: float = 1800.0
    subagent: float = 3600.0
    warning_intervals: list[float] = Field(default_factory=lambda: [60.0, 10.0])
    extend_on_activity: bool = True
    activity_extension: float = 120.0
    max_extensions: int = 3


class WebhookConfig(BaseModel):
    """Settings for the webhook HTTP server."""

    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 8742
    token: str = ""
    max_body_bytes: int = 262144
    rate_limit_per_minute: int = 30


class TranscriptionConfig(BaseModel):
    """External transcription hooks for audio + video (#66).

    Empty strings preserve the built-in strategies in the bundled tool
    scripts (OpenAI Whisper API → local whisper CLI → whisper.cpp).
    When a value is set, the bot exports it via
    ``DUCTOR_TRANSCRIBE_COMMAND`` / ``DUCTOR_VIDEO_TRANSCRIBE_COMMAND``
    and the tool script invokes the external command first (falling
    back to the built-ins on failure).
    """

    audio_command: str = ""
    video_command: str = ""


class NotificationTarget(BaseModel):
    """A chat/topic to route startup or upgrade notifications to (#64).

    ``topic_id`` is Telegram-specific (forum-topic thread). Matrix ignores it.
    """

    enabled: bool = True
    chat_id: int | None = None
    topic_id: int | None = None


class NotificationsConfig(BaseModel):
    """Opt-in routing for lifecycle notifications (#64).

    Empty lists preserve the previous fan-out-to-all behaviour. When
    ``startup_targets`` has at least one enabled target with a valid
    ``chat_id``, startup notices go to those targets only; same for
    ``upgrade_targets`` and new-version notices.
    """

    startup_targets: list[NotificationTarget] = Field(default_factory=list)
    upgrade_targets: list[NotificationTarget] = Field(default_factory=list)


class SceneConfig(BaseModel):
    """Settings for scene indicators and technical footer."""

    seen_reaction: bool = False
    # #63: when True, the user's message gets a stage-based reaction that
    # updates as the agent works (thinking/tool/system) and clears on
    # completion. Overrides ``seen_reaction`` so they do not fight over
    # the same emoji slot. Default on — gives users immediate visual
    # feedback that the agent is working.
    status_reaction: bool = True
    technical_footer: bool = False


class ApiConfig(BaseModel):
    """Settings for the direct WebSocket API server.

    Designed for use over Tailscale or other private networks.
    When ``allow_public`` is False and Tailscale is not detected,
    the server still starts but logs a prominent warning.

    ``chat_id`` controls which session the API client uses.
    ``0`` means "use the first ``allowed_user_ids`` entry".
    """

    enabled: bool = False
    host: str = _BIND_ALL_INTERFACES
    port: int = 8741
    token: str = ""
    chat_id: int = 0
    allow_public: bool = False


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
    from ductor_bot.infra.json_store import atomic_json_save

    data: dict[str, object] = json.loads(config_path.read_text(encoding="utf-8"))
    if all(data.get(key) == value for key, value in updates.items()):
        logger.debug("Skipped config update with unchanged values: %s", ", ".join(updates))
        return
    data.update(updates)
    atomic_json_save(config_path, data)
    # Keys only — values can carry secrets (api.token, gemini_api_key, ...).
    logger.info("Persisted config update: %s", ", ".join(updates))


async def update_config_file_async(config_path: Path, **updates: object) -> None:
    """Async wrapper: update config.json without blocking the event loop."""
    import asyncio

    await asyncio.to_thread(update_config_file, config_path, **updates)


class SkillSyncProviders(BaseModel):
    """Per-provider cross-tool skill sync toggles (#141)."""

    claude: bool = True
    codex: bool = True
    gemini: bool = True
    grok: bool = True


class SkillsConfig(BaseModel):
    """Cross-tool skill sync configuration.

    ``sync_enabled`` is the global switch; ``sync`` allows opting out of
    individual provider skill directories (e.g. ``~/.codex/skills``) while
    keeping the shared workflow for the others.
    """

    sync_enabled: bool = True
    sync: SkillSyncProviders = Field(default_factory=SkillSyncProviders)


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
    cli_timeout: float = 1800.0
    reasoning_effort: str = "medium"
    file_access: str = "all"
    append_system_prompt_files: list[str] = Field(default_factory=list)
    # Per-topic project roots: topic name | "<topic_id>" | "<chat_id>:<topic_id>" -> path
    project_roots: dict[str, str] = Field(default_factory=dict)
    gemini_api_key: str | None = None
    streaming: StreamingConfig = Field(default_factory=StreamingConfig)
    docker: DockerConfig = Field(default_factory=DockerConfig)
    heartbeat: HeartbeatConfig = Field(default_factory=HeartbeatConfig)
    cleanup: CleanupConfig = Field(default_factory=CleanupConfig)
    memory_flush: MemoryFlushConfig = Field(default_factory=MemoryFlushConfig)
    memory_reflection: MemoryReflectionConfig = Field(default_factory=MemoryReflectionConfig)
    memory_compaction: MemoryCompactionConfig = Field(default_factory=MemoryCompactionConfig)
    webhooks: WebhookConfig = Field(default_factory=WebhookConfig)
    api: ApiConfig = Field(default_factory=ApiConfig)
    cli_parameters: CLIParametersConfig = Field(default_factory=CLIParametersConfig)
    image: ImageConfig = Field(default_factory=ImageConfig)
    timeouts: TimeoutConfig = Field(default_factory=TimeoutConfig)
    tasks: TasksConfig = Field(default_factory=TasksConfig)
    cron_delivery_retry: CronDeliveryRetryConfig = Field(default_factory=CronDeliveryRetryConfig)
    cron_preflight: CronPreflightConfig = Field(default_factory=CronPreflightConfig)
    scene: SceneConfig = Field(default_factory=SceneConfig)
    notifications: NotificationsConfig = Field(default_factory=NotificationsConfig)
    transcription: TranscriptionConfig = Field(default_factory=TranscriptionConfig)
    skills: SkillsConfig = Field(default_factory=SkillsConfig)
    user_timezone: str = ""
    language: str = "en"
    update_check: bool = True
    group_mention_only: bool = False
    interagent_port: int = 8799
    transport: str = "telegram"  # "telegram" | "matrix" | "slack"
    transports: list[str] = Field(default_factory=list)
    telegram_token: str = ""
    allowed_user_ids: list[int] = Field(default_factory=list)
    allowed_group_ids: list[int] = Field(default_factory=list)
    allowed_channel_ids: list[int] = Field(default_factory=list)
    matrix: MatrixConfig = Field(default_factory=MatrixConfig)
    slack: SlackConfig = Field(default_factory=SlackConfig)

    @field_validator("gemini_api_key", mode="before")
    @classmethod
    def _normalize_gemini_api_key(cls, value: object) -> object:
        """Normalize null-like string values to ``None`` for optional key config."""
        if not isinstance(value, str):
            return value
        normalized = value.strip()
        if not normalized or normalized.lower() in NULLISH_TEXT_VALUES:
            return None
        return normalized

    @model_validator(mode="after")
    def _sync_cli_timeout_to_timeouts(self) -> AgentConfig:
        """Sync legacy ``cli_timeout`` to ``timeouts.normal`` for backward compat.

        When ``cli_timeout`` differs from the default 600.0 and ``timeouts.normal``
        is still at its default, propagate ``cli_timeout`` into ``timeouts.normal``.
        """
        if self.cli_timeout != 600.0 and self.timeouts.normal == 600.0:
            self.timeouts.normal = self.cli_timeout
        return self

    @model_validator(mode="after")
    def _normalize_transports(self) -> AgentConfig:
        """Normalize ``transports`` and ``transport`` for backward compat.

        - Empty ``transports`` → populated from ``transport`` (single-transport).
        - Non-empty ``transports`` → ``transport`` set to first entry (primary).
        """
        if not self.transports:
            self.transports = [self.transport]
        else:
            self.transport = self.transports[0]
        return self

    @property
    def is_multi_transport(self) -> bool:
        """True when more than one transport is configured."""
        return len(self.transports) > 1


def resolve_timeout(config: AgentConfig, path: str) -> float:
    """Resolve timeout for execution path: 'normal', 'background', 'subagent'."""
    mapping = {
        "normal": config.timeouts.normal,
        "background": config.timeouts.background,
        "subagent": config.timeouts.subagent,
    }
    return mapping.get(path, config.cli_timeout)


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

    # Try host system timezone via environment or OS-specific detection.
    import os
    import sys

    tz_env = os.environ.get("TZ", "").strip()
    if tz_env:
        try:
            return ZoneInfo(tz_env)
        except (ZoneInfoNotFoundError, KeyError):
            pass

    detected = _detect_host_timezone() if sys.platform == "win32" else _detect_posix_timezone()
    return detected or _SAFE_UTC


def _detect_host_timezone() -> ZoneInfo | None:
    """Detect timezone on Windows via datetime."""
    import datetime

    local_tz = datetime.datetime.now(datetime.UTC).astimezone().tzinfo
    if local_tz is None:
        return None
    tz_name = getattr(local_tz, "key", None) or str(local_tz)
    try:
        return ZoneInfo(tz_name)
    except (ZoneInfoNotFoundError, KeyError):
        return None


def _detect_posix_timezone() -> ZoneInfo | None:
    """Detect timezone on POSIX via /etc/localtime symlink."""
    localtime = Path("/etc/localtime")
    if not localtime.is_symlink():
        return None
    target = str(localtime.resolve())
    marker = "/zoneinfo/"
    idx = target.find(marker)
    if idx == -1:
        return None
    candidate = target[idx + len(marker) :]
    try:
        return ZoneInfo(candidate)
    except (ZoneInfoNotFoundError, KeyError):
        return None


# `[1m]` suffix unlocks Claude Code's 1M-context beta on sonnet + opus.
# The Claude CLI strips the suffix before dispatch and sets the beta header
# internally (see https://code.claude.com/docs/en/model-config). Haiku has
# no 1M variant upstream, so it is intentionally omitted.
CLAUDE_MODELS_ORDERED: tuple[str, ...] = (
    "haiku",
    "sonnet",
    "sonnet[1m]",
    "opus",
    "opus[1m]",
    # Claude Code >= 2.1.172 resolves the "fable" alias to the latest Fable
    # model (same auto-tracking as the opus/sonnet aliases).
    "fable",
)
CLAUDE_MODELS: frozenset[str] = frozenset(CLAUDE_MODELS_ORDERED)

# Reasoning-effort levels the Claude CLI accepts via ``--effort``. ``max`` is
# Claude-specific (Codex tops out at ``xhigh``).
CLAUDE_SUPPORTED_EFFORTS: tuple[str, ...] = ("low", "medium", "high", "xhigh", "max")

# Codex effort levels used to validate when the live Codex model cache is
# unavailable, so ``max`` is rejected for Codex regardless of cache state.
CODEX_SUPPORTED_EFFORTS_FALLBACK: tuple[str, ...] = ("low", "medium", "high", "xhigh")

# "auto" is a Gemini-specific alias (Gemini CLI auto-selects the best model).
_GEMINI_ALIASES: frozenset[str] = frozenset({"auto", "pro", "flash", "flash-lite"})

# Antigravity currently exposes a provider-level default through its CLI bridge.
ANTIGRAVITY_MODELS_ORDERED: tuple[str, ...] = ("antigravity-default",)
ANTIGRAVITY_MODELS: frozenset[str] = frozenset(ANTIGRAVITY_MODELS_ORDERED)

# Grok Build models (xAI Grok CLI). Fallback when discovery is unavailable.
GROK_MODELS_ORDERED: tuple[str, ...] = (
    "grok-4.5",
    "grok-composer-2.5-fast",
)
GROK_MODELS: frozenset[str] = frozenset(GROK_MODELS_ORDERED)
# Canonical Grok headless levels (plus max alias of xhigh). See grok --help.
GROK_SUPPORTED_EFFORTS: tuple[str, ...] = (
    "none",
    "minimal",
    "low",
    "medium",
    "high",
    "xhigh",
    "max",
)

_runtime_gemini: list[frozenset[str]] = [frozenset()]
_runtime_antigravity: list[frozenset[str]] = [frozenset()]
_runtime_grok: list[frozenset[str]] = [frozenset()]
_runtime_grok_ordered: list[tuple[str, ...]] = [()]


class ModelRegistry:
    """Provider resolution for models.

    Claude models (haiku, sonnet, opus) are hardcoded.
    Gemini / Antigravity / Grok models refresh from CLI discovery when available.
    Codex models are discovered dynamically at runtime.
    """

    @staticmethod
    def provider_for(model_id: str) -> str:
        """Return the provider for a model ID.

        Claude Code accepts both the short aliases in ``CLAUDE_MODELS`` and
        full model IDs (``claude-opus-4-7``), so any ``claude-`` prefix
        routes to Claude.
        """
        if model_id in CLAUDE_MODELS or model_id.startswith("claude-"):
            return "claude"
        if (
            model_id in _GEMINI_ALIASES
            or model_id in _runtime_gemini[0]
            or model_id.startswith(("gemini-", "auto-gemini-"))
        ):
            return "gemini"
        if (
            model_id in ANTIGRAVITY_MODELS
            or model_id in _runtime_antigravity[0]
            or model_id.startswith("antigravity-")
        ):
            return "antigravity"
        if model_id in GROK_MODELS or model_id in _runtime_grok[0] or model_id.startswith("grok-"):
            return "grok"
        return "codex"


def get_gemini_models() -> frozenset[str]:
    """Return dynamically discovered Gemini models (may be empty)."""
    return _runtime_gemini[0]


def set_gemini_models(models: frozenset[str]) -> None:
    """Set runtime Gemini models discovered from local Gemini CLI files.

    Refuses to overwrite with an empty set to prevent cache wipe.
    """
    if not models:
        return
    _runtime_gemini[0] = models


def reset_gemini_models() -> None:
    """Clear runtime Gemini models. For test teardown only."""
    _runtime_gemini[0] = frozenset()


def get_antigravity_models() -> frozenset[str]:
    """Return dynamically discovered Antigravity models (may be empty)."""
    return _runtime_antigravity[0]


def set_antigravity_models(models: frozenset[str]) -> None:
    """Set runtime Antigravity models discovered from ``agy models``.

    Refuses to overwrite with an empty set to prevent cache wipe.
    """
    if not models:
        return
    _runtime_antigravity[0] = models


def reset_antigravity_models() -> None:
    """Clear runtime Antigravity models. For test teardown only."""
    _runtime_antigravity[0] = frozenset()


def get_grok_models() -> frozenset[str]:
    """Return dynamically discovered Grok models (may be empty)."""
    return _runtime_grok[0]


def get_grok_models_ordered() -> tuple[str, ...]:
    """Return Grok models in discovery order, or the hardcoded fallback list."""
    ordered = _runtime_grok_ordered[0]
    if ordered:
        return ordered
    return GROK_MODELS_ORDERED


def set_grok_models(models: tuple[str, ...] | frozenset[str] | list[str]) -> None:
    """Set runtime Grok models discovered from ``grok models``.

    Refuses to overwrite with an empty set to prevent cache wipe.
    Preserves discovery order when a sequence is provided.
    """
    if not models:
        return
    if isinstance(models, frozenset):
        ordered = tuple(sorted(models))
    else:
        ordered = tuple(dict.fromkeys(models))  # dedupe, keep order
    if not ordered:
        return
    _runtime_grok_ordered[0] = ordered
    _runtime_grok[0] = frozenset(ordered)


def reset_grok_models() -> None:
    """Clear runtime Grok models. For test teardown only."""
    _runtime_grok[0] = frozenset()
    _runtime_grok_ordered[0] = ()
