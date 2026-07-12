"""Native chat stream support for Slack with graceful fallback."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from ductor_bot.cli.stream_events import ToolUseEvent
from ductor_bot.messenger.slack.sender import SlackSendOpts, send_rich
from ductor_bot.text.response_format import normalize_tool_name

logger = logging.getLogger(__name__)

_STREAM_BUFFER_SIZE = 64
_MAX_TOOL_HISTORY = 8
_PLAN_TITLE = "Working on your request"
_ANALYZE_TASK_ID = "analyze"
_ANALYZE_TASK_TITLE = "Understand request"
_TOOLS_TASK_ID = "tools"
_TOOLS_TASK_TITLE = "Use tools if needed"
_RESPONSE_TASK_ID = "respond"
_RESPONSE_TASK_TITLE = "Draft response"
_NO_TOOLS_DETAIL = "No tools needed"
_DETAIL_LIMIT = 280
_SYSTEM_LABELS: dict[str, str] = {
    "thinking": "Thinking",
    "compacting": "Compacting",
    "recovering": "Recovering",
    "timeout_warning": "Timeout approaching",
    "timeout_extended": "Timeout extended",
}


@dataclass(slots=True)
class _PlanTaskState:
    id: str
    title: str
    status: str = "pending"
    details: str = ""


@dataclass(slots=True)
class _ToolActivity:
    label: str
    target: str = ""


class SlackStreamEditor:
    """Render Slack output through the native chat streaming APIs."""

    def __init__(  # noqa: PLR0913
        self,
        client: Any,
        channel_id: str,
        *,
        thread_ts: str,
        recipient_user_id: str | None = None,
        recipient_team_id: str | None = None,
        edit_interval_seconds: float = 1.0,
    ) -> None:
        del edit_interval_seconds
        self._client = client
        self._channel_id = channel_id
        self._thread_ts = thread_ts
        self._recipient_user_id = recipient_user_id
        self._recipient_team_id = recipient_team_id
        self._stream: Any | None = None
        self._native_failed = False
        self._thinking_parts: list[str] = []
        self._answer_parts: list[str] = []
        self._status: str | None = None
        self._thinking_started = False
        self._answer_started = False
        self._tool_history: list[_ToolActivity] = []
        self._used_tools = False
        self._plan_started = False
        self._plan_tasks = {
            _ANALYZE_TASK_ID: _PlanTaskState(_ANALYZE_TASK_ID, _ANALYZE_TASK_TITLE),
            _TOOLS_TASK_ID: _PlanTaskState(_TOOLS_TASK_ID, _TOOLS_TASK_TITLE),
            _RESPONSE_TASK_ID: _PlanTaskState(_RESPONSE_TASK_ID, _RESPONSE_TASK_TITLE),
        }

    async def on_thinking(self, text: str) -> None:
        """Append streamed reasoning text."""
        if not text.strip():
            return
        self._thinking_parts.append(text)
        self._status = "thinking"
        await self._ensure_plan_started(
            phase=_ANALYZE_TASK_ID,
            analysis_detail=self._thinking_detail(text),
        )
        prefix = "💭 *Thinking*\n" if not self._thinking_started else ""
        self._thinking_started = True
        await self._append_markdown(prefix + text)

    async def on_delta(self, text: str) -> None:
        """Append assistant answer text."""
        if not text:
            return
        await self._ensure_plan_started(phase=_RESPONSE_TASK_ID)
        plan_updates: list[dict[str, object]] = []
        analyze_chunk = self._set_task(
            _ANALYZE_TASK_ID,
            status="complete",
            details=self._analysis_detail(),
        )
        if analyze_chunk is not None:
            plan_updates.append(analyze_chunk)
        tools_chunk = self._set_task(
            _TOOLS_TASK_ID,
            status="complete",
            details=self._tool_details() if self._used_tools else _NO_TOOLS_DETAIL,
        )
        if tools_chunk is not None:
            plan_updates.append(tools_chunk)
        response_chunk = self._set_task(_RESPONSE_TASK_ID, status="in_progress")
        if response_chunk is not None:
            plan_updates.append(response_chunk)
        await self._append_chunks(plan_updates)
        self._answer_parts.append(text)
        prefix = ""
        if not self._answer_started:
            prefix = "\n\n" if self._thinking_started else ""
            self._answer_started = True
        await self._append_markdown(prefix + text)

    async def on_tool(self, tool: ToolUseEvent | str) -> None:
        """Show tool activity using Slack's plan UI."""
        activity = self._tool_activity(tool)
        if activity.target:
            self._thinking_parts.append(f"\n[TOOL: {activity.label}: {activity.target}]\n")
        else:
            self._thinking_parts.append(f"\n[TOOL: {activity.label}]\n")
        self._tool_history.append(activity)
        self._tool_history = self._tool_history[-_MAX_TOOL_HISTORY:]
        self._used_tools = True
        await self._ensure_plan_started(phase=_TOOLS_TASK_ID)
        chunks: list[dict[str, object]] = []
        analyze_chunk = self._set_task(
            _ANALYZE_TASK_ID,
            status="complete",
            details=self._analysis_detail(),
        )
        if analyze_chunk is not None:
            chunks.append(analyze_chunk)
        tools_chunk = self._set_task(
            _TOOLS_TASK_ID,
            status="in_progress",
            details=self._tool_details(),
        )
        if tools_chunk is not None:
            chunks.append(tools_chunk)
        await self._append_chunks(chunks)

    async def on_system(self, status: str | None) -> None:
        """Track transient system status."""
        self._status = status
        if status is None or status == "thinking":
            return
        label = _SYSTEM_LABELS.get(status)
        if label:
            await self._append_markdown(f"\n\n_{label}_")

    async def finalize(self, final_text: str | None) -> None:
        """Finalize the stream, falling back to a single rich message if needed."""
        if final_text and not "".join(self._answer_parts).strip():
            self._answer_parts = [final_text]
        if self._native_failed:
            rendered = self._render_fallback(final_text)
            if rendered:
                await send_rich(
                    self._client,
                    self._channel_id,
                    rendered,
                    SlackSendOpts(thread_ts=self._thread_ts),
                )
            return

        stream = await self._ensure_stream()
        stop_text = None
        if final_text and not self._answer_started:
            stop_text = final_text
        stop_chunks = self._final_plan_chunks(final_text)
        await stream.stop(markdown_text=stop_text, chunks=stop_chunks)

    async def _append_markdown(self, text: str) -> None:
        if not text or self._native_failed:
            return
        try:
            stream = await self._ensure_stream()
            await stream.append(markdown_text=text)
        except Exception as exc:
            self._mark_native_failure(exc)

    async def _append_chunks(self, chunks: list[dict[str, object]]) -> None:
        if not chunks or self._native_failed:
            return
        try:
            stream = await self._ensure_stream()
            await stream.append(chunks=chunks)
        except Exception as exc:
            self._mark_native_failure(exc)

    async def _ensure_stream(self) -> Any:
        if self._stream is not None:
            return self._stream
        kwargs: dict[str, object] = {
            "channel": self._channel_id,
            "thread_ts": self._thread_ts,
            "task_display_mode": "plan",
            "buffer_size": _STREAM_BUFFER_SIZE,
        }
        if self._recipient_team_id is not None:
            kwargs["recipient_team_id"] = self._recipient_team_id
        if self._recipient_user_id is not None:
            kwargs["recipient_user_id"] = self._recipient_user_id
        self._stream = await self._client.chat_stream(**kwargs)
        return self._stream

    def _mark_native_failure(self, exc: Exception) -> None:
        if self._native_failed:
            return
        self._native_failed = True
        logger.warning("Slack native stream failed; falling back to plain reply: %r", exc)

    async def _ensure_plan_started(
        self,
        *,
        phase: str,
        analysis_detail: str = "",
    ) -> None:
        if self._plan_started:
            return
        chunks: list[dict[str, object]] = [{"type": "plan_update", "title": _PLAN_TITLE}]
        if phase == _ANALYZE_TASK_ID:
            self._plan_tasks[_ANALYZE_TASK_ID].status = "in_progress"
            self._plan_tasks[_ANALYZE_TASK_ID].details = analysis_detail
        elif phase == _TOOLS_TASK_ID:
            self._plan_tasks[_ANALYZE_TASK_ID].status = "complete"
            self._plan_tasks[_ANALYZE_TASK_ID].details = self._analysis_detail()
            self._plan_tasks[_TOOLS_TASK_ID].status = "in_progress"
            self._plan_tasks[_TOOLS_TASK_ID].details = self._tool_details()
        elif phase == _RESPONSE_TASK_ID:
            self._plan_tasks[_ANALYZE_TASK_ID].status = "complete"
            self._plan_tasks[_ANALYZE_TASK_ID].details = self._analysis_detail()
            self._plan_tasks[_TOOLS_TASK_ID].status = "complete"
            self._plan_tasks[_TOOLS_TASK_ID].details = (
                self._tool_details() if self._used_tools else _NO_TOOLS_DETAIL
            )
            self._plan_tasks[_RESPONSE_TASK_ID].status = "in_progress"
        chunks.extend(
            self._task_chunk(self._plan_tasks[task_id])
            for task_id in (_ANALYZE_TASK_ID, _TOOLS_TASK_ID, _RESPONSE_TASK_ID)
        )
        self._plan_started = True
        await self._append_chunks(chunks)

    def _set_task(
        self,
        task_id: str,
        *,
        status: str | None = None,
        details: str | None = None,
    ) -> dict[str, object] | None:
        task = self._plan_tasks[task_id]
        next_status = status or task.status
        next_details = task.details if details is None else self._limit_detail(details)
        if task.status == next_status and task.details == next_details:
            return None
        task.status = next_status
        task.details = next_details
        return self._task_chunk(task)

    def _task_chunk(self, task: _PlanTaskState) -> dict[str, object]:
        chunk: dict[str, object] = {
            "type": "task_update",
            "id": task.id,
            "title": task.title,
            "status": task.status,
        }
        if task.details:
            chunk["details"] = task.details
        return chunk

    def _analysis_detail(self) -> str:
        for part in self._thinking_parts:
            detail = self._thinking_detail(part)
            if detail:
                return detail
        return "Understanding the request"

    def _thinking_detail(self, text: str) -> str:
        cleaned = " ".join(text.split())
        return self._limit_detail(cleaned)

    def _tool_activity(self, tool: ToolUseEvent | str) -> _ToolActivity:
        label = normalize_tool_name(str(getattr(tool, "tool_name", tool)))
        parameters = getattr(tool, "parameters", None)
        return _ToolActivity(label=label, target=self._tool_target(parameters))

    def _tool_target(self, parameters: dict[str, Any] | None) -> str:
        if not parameters:
            return ""
        return self._limit_detail(
            self._string_param(parameters, ("url", "uri"), transform=self._compact_url)
            or self._string_param(parameters, ("query", "q", "search", "pattern"))
            or self._string_param(
                parameters, ("path", "file_path", "file", "filepath", "directory", "dir")
            )
            or self._string_param(parameters, ("cmd", "command"))
            or self._list_param(parameters, ("urls", "paths"))
        )

    def _string_param(
        self,
        parameters: dict[str, Any],
        keys: tuple[str, ...],
        *,
        transform: Any = None,
    ) -> str:
        for key in keys:
            value = parameters.get(key)
            if isinstance(value, str) and value.strip():
                cleaned = " ".join(value.split())
                return transform(cleaned) if callable(transform) else cleaned
        return ""

    def _list_param(self, parameters: dict[str, Any], keys: tuple[str, ...]) -> str:
        for key in keys:
            value = parameters.get(key)
            if isinstance(value, list) and value:
                first = value[0]
                if isinstance(first, str) and first.strip():
                    suffix = f" (+{len(value) - 1} more)" if len(value) > 1 else ""
                    return f"{first.strip()}{suffix}"
        return ""

    def _compact_url(self, url: str) -> str:
        compact = url.replace("https://", "").replace("http://", "")
        return compact.rstrip("/")

    def _tool_details(self) -> str:
        collapsed: list[tuple[_ToolActivity, int]] = []
        for activity in self._tool_history:
            if collapsed and collapsed[-1][0] == activity:
                previous, count = collapsed[-1]
                collapsed[-1] = (previous, count + 1)
            else:
                collapsed.append((activity, 1))
        lines = [
            self._tool_detail_line(activity, count)
            for activity, count in collapsed[-_MAX_TOOL_HISTORY:]
        ]
        details = "\n".join(lines)
        return self._limit_detail(details)

    def _tool_detail_line(self, activity: _ToolActivity, count: int) -> str:
        label = f"{activity.label} x{count}" if count > 1 else activity.label
        if activity.target:
            return f"- {label}: {activity.target}"
        return f"- {label}"

    def _final_plan_chunks(self, final_text: str | None) -> list[dict[str, object]] | None:
        if not self._plan_started:
            return None
        chunks: list[dict[str, object]] = []
        analyze_chunk = self._set_task(
            _ANALYZE_TASK_ID,
            status="complete",
            details=self._analysis_detail(),
        )
        if analyze_chunk is not None:
            chunks.append(analyze_chunk)
        tools_chunk = self._set_task(
            _TOOLS_TASK_ID,
            status="complete",
            details=self._tool_details() if self._used_tools else _NO_TOOLS_DETAIL,
        )
        if tools_chunk is not None:
            chunks.append(tools_chunk)
        if self._answer_started or final_text:
            response_chunk = self._set_task(_RESPONSE_TASK_ID, status="complete")
            if response_chunk is not None:
                chunks.append(response_chunk)
        return chunks or None

    def _limit_detail(self, text: str) -> str:
        if len(text) <= _DETAIL_LIMIT:
            return text
        return text[: _DETAIL_LIMIT - 1].rstrip() + "…"

    def _render_fallback(self, final_text: str | None) -> str:
        sections: list[str] = []
        thinking = "".join(self._thinking_parts).strip()
        answer = "".join(self._answer_parts).strip() or (final_text or "").strip()

        if thinking:
            sections.append(f"💭 *Thinking*\n{thinking}")
        elif self._status is not None:
            label = _SYSTEM_LABELS.get(self._status or "")
            if label:
                sections.append(f"💭 *{label}*")

        if answer:
            sections.append(answer)

        if not sections:
            sections.append("…")
        return "\n\n".join(sections).strip()
