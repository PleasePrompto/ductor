"""Webhook data models and template rendering."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

_TEMPLATE_RE = re.compile(r"\{\{(\w+)\}\}")


@dataclass
class WebhookEntry:
    """A registered webhook endpoint definition."""

    id: str
    title: str
    description: str
    mode: str  # "wake" | "cron_task"
    prompt_template: str
    enabled: bool = True
    task_folder: str | None = None
    auth_mode: str = "bearer"  # "bearer" | "hmac"
    token: str = ""  # per-hook bearer token (auto-generated on creation)
    hmac_secret: str = ""  # external service's HMAC signing secret
    hmac_header: str = ""  # header name for HMAC signature (e.g. "X-Hub-Signature-256")
    hmac_algorithm: str = "sha256"  # sha256 | sha1 | sha512
    hmac_encoding: str = "hex"  # hex | base64
    hmac_sig_prefix: str = "sha256="  # prefix to strip from header value before comparison
    hmac_sig_regex: str = ""  # regex to extract signature (group 1), overrides sig_prefix
    hmac_payload_prefix_regex: str = ""  # regex on header value, group 1 prepended to body with "."
    created_at: str = ""
    trigger_count: int = 0
    last_triggered_at: str | None = None
    last_error: str | None = None

    # Per-webhook execution overrides
    provider: str | None = None
    model: str | None = None
    reasoning_effort: str | None = None
    cli_parameters: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.created_at:
            self.created_at = datetime.now(UTC).isoformat()

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "mode": self.mode,
            "prompt_template": self.prompt_template,
            "enabled": self.enabled,
            "task_folder": self.task_folder,
            "auth_mode": self.auth_mode,
            "token": self.token,
            "hmac_secret": self.hmac_secret,
            "hmac_header": self.hmac_header,
            "hmac_algorithm": self.hmac_algorithm,
            "hmac_encoding": self.hmac_encoding,
            "hmac_sig_prefix": self.hmac_sig_prefix,
            "hmac_sig_regex": self.hmac_sig_regex,
            "hmac_payload_prefix_regex": self.hmac_payload_prefix_regex,
            "created_at": self.created_at,
            "trigger_count": self.trigger_count,
            "last_triggered_at": self.last_triggered_at,
            "last_error": self.last_error,
            "provider": self.provider,
            "model": self.model,
            "reasoning_effort": self.reasoning_effort,
            "cli_parameters": self.cli_parameters,
        }

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> WebhookEntry:
        return cls(
            id=data["id"],
            title=data["title"],
            description=data.get("description", ""),
            mode=data["mode"],
            prompt_template=data["prompt_template"],
            enabled=data.get("enabled", True),
            task_folder=data.get("task_folder"),
            auth_mode=data.get("auth_mode", "bearer"),
            token=data.get("token", ""),
            hmac_secret=data.get("hmac_secret", ""),
            hmac_header=data.get("hmac_header", ""),
            hmac_algorithm=data.get("hmac_algorithm", "sha256"),
            hmac_encoding=data.get("hmac_encoding", "hex"),
            hmac_sig_prefix=data.get("hmac_sig_prefix", "sha256="),
            hmac_sig_regex=data.get("hmac_sig_regex", ""),
            hmac_payload_prefix_regex=data.get("hmac_payload_prefix_regex", ""),
            created_at=data.get("created_at", ""),
            trigger_count=data.get("trigger_count", 0),
            last_triggered_at=data.get("last_triggered_at"),
            last_error=data.get("last_error"),
            provider=data.get("provider"),
            model=data.get("model"),
            reasoning_effort=data.get("reasoning_effort"),
            cli_parameters=data.get("cli_parameters", []),
        )


@dataclass(frozen=True)
class WebhookResult:
    """Immutable result of a webhook dispatch."""

    hook_id: str
    hook_title: str
    mode: str
    result_text: str
    status: str  # "success" | "error:..."


def render_template(template: str, payload: dict[str, Any]) -> str:
    """Replace ``{{field}}`` placeholders with values from *payload*.

    Missing keys render as ``{{?field}}`` so they are visible but non-fatal.
    """

    def _replace(match: re.Match[str]) -> str:
        key = match.group(1)
        value = payload.get(key)
        if value is None:
            return f"{{{{?{key}}}}}"
        return str(value)

    return _TEMPLATE_RE.sub(_replace, template)
