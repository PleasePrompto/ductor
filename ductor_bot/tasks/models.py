"""Data models for the background task system."""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from typing import Any


@dataclass(slots=True)
class TaskSubmit:
    """Input for creating a background task."""

    chat_id: str
    prompt: str
    message_id: int
    thread_id: int | None
    parent_agent: str
    name: str = ""
    provider_override: str = ""
    model_override: str = ""
    thinking_override: str = ""


@dataclass(slots=True)
class TaskEntry:
    """Persisted task metadata."""

    task_id: str
    chat_id: str
    parent_agent: str
    name: str
    prompt_preview: str
    provider: str
    model: str
    status: str  # "running" | "done" | "failed" | "cancelled" | "waiting"
    session_id: str = ""
    created_at: float = field(default_factory=time.time)
    completed_at: float = 0.0
    elapsed_seconds: float = 0.0
    error: str = ""
    result_preview: str = ""
    question_count: int = 0
    num_turns: int = 0
    last_question: str = ""
    original_prompt: str = ""
    thinking: str = ""
    tasks_dir: str = ""  # Agent's tasks directory (for per-agent folder resolution)
    thread_id: int | None = None  # Forum topic ID (for routing results back to topic)

    def to_dict(self) -> dict[str, object]:
        d: dict[str, object] = {
            "task_id": self.task_id,
            "chat_id": self.chat_id,
            "parent_agent": self.parent_agent,
            "name": self.name,
            "prompt_preview": self.prompt_preview,
            "provider": self.provider,
            "model": self.model,
            "status": self.status,
            "session_id": self.session_id,
            "created_at": self.created_at,
            "completed_at": self.completed_at,
            "elapsed_seconds": self.elapsed_seconds,
            "error": self.error,
            "result_preview": self.result_preview,
            "question_count": self.question_count,
            "num_turns": self.num_turns,
            "last_question": self.last_question,
            "thinking": self.thinking,
            "tasks_dir": self.tasks_dir,
        }
        if self.thread_id is not None:
            d["thread_id"] = self.thread_id
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> TaskEntry:
        return cls(
            task_id=d["task_id"],
            chat_id=d["chat_id"],
            parent_agent=d.get("parent_agent", "main"),
            name=d.get("name", ""),
            prompt_preview=d.get("prompt_preview", ""),
            provider=d.get("provider", ""),
            model=d.get("model", ""),
            status=d.get("status", "running"),
            session_id=d.get("session_id", ""),
            created_at=d.get("created_at", 0.0),
            completed_at=d.get("completed_at", 0.0),
            elapsed_seconds=d.get("elapsed_seconds", 0.0),
            error=d.get("error", ""),
            result_preview=d.get("result_preview", ""),
            question_count=d.get("question_count", 0),
            num_turns=d.get("num_turns", 0),
            last_question=d.get("last_question", ""),
            thinking=d.get("thinking", ""),
            tasks_dir=d.get("tasks_dir", ""),
            thread_id=d.get("thread_id"),
        )


@dataclass(slots=True)
class TaskInFlight:
    """In-memory tracking for a running task."""

    entry: TaskEntry
    asyncio_task: asyncio.Task[None] | None = field(default=None, repr=False)
    has_pending_question: bool = False


@dataclass(slots=True)
class TaskResult:
    """Outcome delivered to parent agent after task completion."""

    task_id: str
    chat_id: str
    parent_agent: str
    name: str
    prompt_preview: str
    result_text: str
    status: str  # "done" | "failed" | "cancelled" | "timeout"
    elapsed_seconds: float
    provider: str
    model: str
    session_id: str = ""
    error: str = ""
    task_folder: str = ""
    original_prompt: str = ""
    thread_id: int | None = None
