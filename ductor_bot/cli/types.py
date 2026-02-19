"""Shared types for the CLI layer."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel, Field


class CLIResponse(BaseModel):
    """Response from a CLI call -- provider-agnostic."""

    session_id: str | None = None
    result: str = ""
    is_error: bool = False
    returncode: int | None = None
    stderr: str = ""
    timed_out: bool = False
    duration_ms: float | None = None
    duration_api_ms: float | None = None
    num_turns: int | None = None
    total_cost_usd: float | None = None
    usage: dict[str, Any] = Field(default_factory=dict)
    model_usage: dict[str, Any] = Field(default_factory=dict)

    @property
    def input_tokens(self) -> int:
        """Total input tokens (includes cache reads/writes)."""
        return int(self.usage.get("input_tokens", 0))

    @property
    def output_tokens(self) -> int:
        """Total output tokens."""
        return int(self.usage.get("output_tokens", 0))

    @property
    def total_tokens(self) -> int:
        """Combined input + output tokens for context tracking."""
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True, slots=True)
class AgentRequest:
    """Immutable specification for a CLI call."""

    prompt: str
    system_prompt: str | None = None
    append_system_prompt: str | None = None
    model_override: str | None = None
    provider_override: str | None = None
    chat_id: int = 0
    process_label: str = "main"
    resume_session: str | None = None
    continue_session: bool = False
    timeout_seconds: float | None = None


@dataclass(frozen=True, slots=True)
class AgentResponse:
    """Immutable result from a CLI call."""

    result: str
    returncode: int | None = None
    session_id: str | None = None
    is_error: bool = False
    cost_usd: float = 0.0
    total_tokens: int = 0
    input_tokens: int = 0
    timed_out: bool = False
    duration_ms: float | None = None
    stream_fallback: bool = False
