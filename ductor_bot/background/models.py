"""Data models for background task tracking."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field


@dataclass(slots=True)
class BackgroundSubmit:
    """Input for submitting a background task."""

    chat_id: int
    prompt: str
    message_id: int
    thread_id: int | None
    transport: str = "tg"
    session_name: str = ""
    resume_session_id: str = ""
    provider_override: str = ""
    model_override: str = ""
    reasoning_effort_override: str = ""


@dataclass(slots=True)
class BackgroundTask:
    """In-flight background task metadata."""

    task_id: str
    chat_id: int
    prompt: str
    message_id: int
    thread_id: int | None
    transport: str
    provider: str
    model: str
    reasoning_effort: str
    submitted_at: float
    asyncio_task: asyncio.Task[None] | None = field(default=None, repr=False)
    session_name: str = ""
    resume_session_id: str = ""


@dataclass(slots=True)
class BackgroundResult:
    """Outcome delivered after a background task completes."""

    task_id: str
    chat_id: int
    message_id: int
    thread_id: int | None
    transport: str
    prompt_preview: str
    result_text: str
    status: str
    elapsed_seconds: float
    provider: str
    model: str
    session_name: str = ""
    session_id: str = ""
