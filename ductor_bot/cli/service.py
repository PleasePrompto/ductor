"""CLIService: unified gateway for ALL CLI calls in the project.

No retry/backoff, no circuit breaker, no dead letters.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING

from ductor_bot.cli.base import CLIConfig
from ductor_bot.cli.factory import create_cli
from ductor_bot.cli.stream_events import (
    AssistantTextDelta,
    CompactBoundaryEvent,
    ResultEvent,
    StreamEvent,
    SystemInitEvent,
    SystemStatusEvent,
    ThinkingEvent,
    ToolUseEvent,
)
from ductor_bot.cli.types import AgentRequest, AgentResponse, CLIResponse

if TYPE_CHECKING:
    from ductor_bot.cli.base import BaseCLI
    from ductor_bot.cli.process_registry import ProcessRegistry
    from ductor_bot.config import ModelRegistry

logger = logging.getLogger(__name__)

_ToolCallback = Callable[[ToolUseEvent], Awaitable[None]]


class _StreamCallbacks:
    """Dispatch stream events to the appropriate callbacks."""

    def __init__(  # noqa: PLR0913
        self,
        on_text: Callable[[str], Awaitable[None]] | None,
        on_thinking: Callable[[str], Awaitable[None]] | None,
        on_tool: _ToolCallback | None,
        on_status: Callable[[str | None], Awaitable[None]] | None,
        on_reasoning: Callable[[str], Awaitable[None]] | None = None,
        on_compact_boundary: Callable[[], Awaitable[None]] | None = None,
    ) -> None:
        self._on_text = on_text
        self._on_thinking = on_thinking
        self._on_tool = on_tool
        self._on_status = on_status
        self._on_reasoning = on_reasoning
        self._on_compact_boundary = on_compact_boundary
        self.init_session_id: str | None = None

    async def dispatch(self, event: StreamEvent) -> tuple[str, ResultEvent | None]:  # noqa: C901
        """Handle one event. Returns (accumulated_text_chunk, result_or_none)."""
        if isinstance(event, SystemInitEvent) and event.session_id:
            self.init_session_id = event.session_id
            return "", None
        if isinstance(event, AssistantTextDelta) and event.text:
            if self._on_text is not None:
                await self._on_text(event.text)
            return event.text, None
        if isinstance(event, ThinkingEvent):
            if event.text and self._on_thinking is not None:
                await self._on_thinking(event.text)
            if self._on_reasoning is not None and event.text:
                await self._on_reasoning(event.text)
            elif self._on_status is not None:
                await self._on_status("thinking")
        elif isinstance(event, ToolUseEvent) and self._on_tool is not None:
            await self._on_tool(event)
        elif isinstance(event, SystemStatusEvent) and self._on_status is not None:
            await self._on_status(event.status)
        elif isinstance(event, CompactBoundaryEvent):
            await self._handle_compact_boundary(event)
        elif isinstance(event, ResultEvent):
            return "", event
        return "", None

    async def _handle_compact_boundary(self, event: CompactBoundaryEvent) -> None:
        """Log the boundary and fan out to the configured callbacks."""
        logger.info(
            "Context compacted (trigger=%s, pre_tokens=%d)",
            event.trigger,
            event.pre_tokens,
        )
        if self._on_compact_boundary is not None:
            await self._on_compact_boundary()
        if self._on_status is not None:
            await self._on_status(None)


@dataclass(frozen=True, slots=True)
class CLIServiceConfig:
    """Static wiring that CLIService needs from the orchestrator."""

    working_dir: str
    default_model: str
    provider: str
    max_turns: int | None
    max_budget_usd: float | None
    permission_mode: str
    reasoning_effort: str = "medium"
    gemini_api_key: str | None = None
    docker_container: str = ""
    claude_cli_parameters: tuple[str, ...] = ()
    codex_cli_parameters: tuple[str, ...] = ()
    gemini_cli_parameters: tuple[str, ...] = ()
    antigravity_cli_parameters: tuple[str, ...] = ()
    grok_cli_parameters: tuple[str, ...] = ()
    agent_name: str = "main"
    interagent_port: int = 8799
    # External transcription hooks (#66) — empty strings keep built-in strategies.
    transcribe_command: str = ""
    video_transcribe_command: str = ""

    def cli_parameters_for_provider(self, provider: str) -> list[str]:
        """Return CLI parameters for the given provider."""
        if provider == "codex":
            return list(self.codex_cli_parameters)
        if provider == "gemini":
            return list(self.gemini_cli_parameters)
        if provider == "antigravity":
            return list(self.antigravity_cli_parameters)
        if provider == "grok":
            return list(self.grok_cli_parameters)
        return list(self.claude_cli_parameters)


class CLIService:
    """Single gateway for every CLI call in the project."""

    def __init__(
        self,
        *,
        config: CLIServiceConfig,
        models: ModelRegistry,
        available_providers: frozenset[str],
        process_registry: ProcessRegistry,
    ) -> None:
        self._config = config
        self._models = models
        self._available_providers = available_providers
        self._process_registry = process_registry
        self._working_dir_resolver: Callable[[AgentRequest], str | None] | None = None

    def set_working_dir_resolver(self, resolver: Callable[[AgentRequest], str | None]) -> None:
        """Register a callback that maps a request to a per-request working dir.

        The resolver returns an absolute path to use as the CLI working
        directory, or ``None`` to keep the configured default workspace.
        """
        self._working_dir_resolver = resolver

    @property
    def docker_enabled(self) -> bool:
        """True when CLI calls run inside a Docker container."""
        return bool(self._config.docker_container)

    def update_available_providers(self, providers: frozenset[str]) -> None:
        self._available_providers = providers

    def update_default_model(self, model: str) -> None:
        """Update the default model after /model switch."""
        self._config = replace(self._config, default_model=model)

    def update_reasoning_effort(self, effort: str) -> None:
        """Update the default reasoning effort after wizard selection."""
        self._config = replace(self._config, reasoning_effort=effort)

    def update_config(self, config: CLIServiceConfig) -> None:
        """Replace the full service config (used by config hot-reload)."""
        self._config = config

    def update_docker_container(self, container: str) -> None:
        """Switch Docker container (empty string = host execution)."""
        self._config = replace(self._config, docker_container=container)

    def _resolve_model(self, request: AgentRequest) -> str:
        """Resolve the effective model for logging and metadata."""
        if request.provider_override:
            return request.model_override or f"<{request.provider_override} default>"
        return request.model_override or self._config.default_model

    async def execute(self, request: AgentRequest) -> AgentResponse:
        """Execute a CLI call."""
        cli = self._make_cli(request)
        logger.info(
            "CLI execute starting label=%s model=%s",
            request.process_label,
            self._resolve_model(request),
        )

        t0 = time.monotonic()
        response = await cli.send(
            prompt=request.prompt,
            resume_session=request.resume_session,
            continue_session=request.continue_session,
            timeout_seconds=request.timeout_seconds,
            timeout_controller=request.timeout_controller,
        )
        elapsed_ms = (time.monotonic() - t0) * 1000

        agent_resp = _cli_response_to_agent_response(response)
        self._log_call(request, agent_resp, elapsed_ms)
        return agent_resp

    async def execute_streaming(  # noqa: PLR0913
        self,
        request: AgentRequest,
        on_text_delta: Callable[[str], Awaitable[None]] | None = None,
        on_thinking_delta: Callable[[str], Awaitable[None]] | None = None,
        on_tool_activity: _ToolCallback | None = None,
        on_system_status: Callable[[str | None], Awaitable[None]] | None = None,
        on_reasoning_delta: Callable[[str], Awaitable[None]] | None = None,
        on_compact_boundary: Callable[[], Awaitable[None]] | None = None,
    ) -> AgentResponse:
        """Execute a streaming CLI call with automatic fallback to non-streaming."""
        cli = self._make_cli(request)
        logger.info(
            "CLI streaming starting label=%s model=%s",
            request.process_label,
            self._resolve_model(request),
        )

        accumulated_text = ""
        result_event: ResultEvent | None = None
        stream_error = False

        callbacks = _StreamCallbacks(
            on_text_delta,
            on_thinking_delta,
            on_tool_activity,
            on_system_status,
            on_reasoning_delta,
            on_compact_boundary,
        )

        try:
            async for event in cli.send_streaming(
                prompt=request.prompt,
                resume_session=request.resume_session,
                continue_session=request.continue_session,
                timeout_seconds=request.timeout_seconds,
                timeout_controller=request.timeout_controller,
            ):
                if self._process_registry.was_aborted(
                    request.chat_id
                ) or self._process_registry.was_aborted_topic(request.chat_id, request.topic_id):
                    logger.info("Streaming aborted mid-stream chat=%d", request.chat_id)
                    break
                text, result = await callbacks.dispatch(event)
                accumulated_text += text
                if result is not None:
                    result_event = result
        except asyncio.CancelledError:
            raise
        except (OSError, RuntimeError, ValueError, UnicodeDecodeError):
            logger.exception(
                "Stream error label=%s, falling back",
                request.process_label,
            )
            stream_error = True

        if stream_error or result_event is None:
            return await self._handle_stream_fallback(
                request,
                accumulated_text,
                stream_error=stream_error,
                init_session_id=callbacks.init_session_id,
            )

        # Carry forward session_id from SystemInitEvent when the ResultEvent
        # lacks one (e.g. timeout kill before final event).
        if not result_event.session_id and callbacks.init_session_id:
            result_event.session_id = callbacks.init_session_id

        # Detect timeout marker from executor.
        timed_out = (result_event.result or "").startswith("__TIMEOUT__")

        logger.info(
            "CLI streaming completed label=%s fallback=%s timed_out=%s",
            request.process_label,
            stream_error,
            timed_out,
        )
        cli_resp = CLIResponse(
            session_id=result_event.session_id,
            result="" if timed_out else (result_event.result or accumulated_text),
            is_error=result_event.is_error,
            timed_out=timed_out,
            returncode=result_event.returncode,
            duration_ms=result_event.duration_ms,
            duration_api_ms=result_event.duration_api_ms,
            total_cost_usd=result_event.total_cost_usd,
            usage=result_event.usage,
            model_usage=result_event.model_usage,
            num_turns=result_event.num_turns,
        )
        return _cli_response_to_agent_response(cli_resp)

    async def _handle_stream_fallback(
        self,
        request: AgentRequest,
        accumulated_text: str,
        *,
        stream_error: bool,
        init_session_id: str | None = None,
    ) -> AgentResponse:
        """Handle failed or incomplete streaming: use accumulated text or retry."""
        was_aborted = self._process_registry.was_aborted(
            request.chat_id
        ) or self._process_registry.was_aborted_topic(request.chat_id, request.topic_id)
        logger.info(
            "Stream fallback: aborted=%s accumulated=%d init_sid=%s",
            was_aborted,
            len(accumulated_text),
            (init_session_id or "?")[:8],
        )

        if was_aborted:
            return AgentResponse(result="")

        if accumulated_text and not stream_error:
            logger.info(
                "Stream completed without ResultEvent, using %d chars",
                len(accumulated_text),
            )
            return AgentResponse(result=accumulated_text, session_id=init_session_id)

        logger.warning(
            "Streaming failed error=%s accumulated=%d chars, retrying non-streaming",
            stream_error,
            len(accumulated_text),
        )
        resp = await self.execute(request)
        return AgentResponse(
            result=resp.result,
            returncode=resp.returncode,
            session_id=resp.session_id,
            is_error=resp.is_error,
            cost_usd=resp.cost_usd,
            total_tokens=resp.total_tokens,
            input_tokens=resp.input_tokens,
            timed_out=resp.timed_out,
            duration_ms=resp.duration_ms,
            stream_fallback=True,
        )

    def resolve_provider(self, request: AgentRequest) -> tuple[str, str]:
        """Return ``(provider, model)`` that would be used for *request*."""
        if request.provider_override:
            return request.provider_override, request.model_override or ""
        model = request.model_override or self._config.default_model
        return self._models.provider_for(model), model

    def _make_cli(self, request: AgentRequest) -> BaseCLI:
        """Create a BaseCLI instance for the given request."""
        provider, model = self.resolve_provider(request)
        # Per-turn effort: request override wins, else the service default
        # (mirrors model_override or default_model).
        effort = request.effort_override or self._config.reasoning_effort

        # Per-request working dir override (project_roots). Skipped in Docker
        # mode: docker_wrap maps cwd into the container via relative_to() and
        # would fail on a path outside the workspace.
        working_dir = self._config.working_dir
        append_prompt = request.append_system_prompt
        if self._working_dir_resolver is not None and not self._config.docker_container:
            override = self._working_dir_resolver(request)
            if override is not None:
                working_dir = override
                # Applied here at the single choke point so every execution
                # path (normal turns, memory flush/compact, heartbeat) keeps
                # addressing the bot memory by absolute path — a relative
                # memory_system/ reference would otherwise land inside the
                # user's project repo.
                note = (
                    f"[ductor] Project cwd override active. The shared bot workspace "
                    f"(tools/, memory_system/) is at {self._config.working_dir}. Bot memory "
                    f"lives at {self._config.working_dir}/memory_system/MAINMEMORY.md — always "
                    f"address it by this absolute path, never via a relative path."
                )
                append_prompt = f"{append_prompt}\n\n{note}" if append_prompt else note

        return create_cli(
            CLIConfig(
                provider=provider,
                working_dir=working_dir,
                model=model,
                system_prompt=request.system_prompt,
                append_system_prompt=append_prompt,
                max_turns=self._config.max_turns,
                max_budget_usd=self._config.max_budget_usd,
                permission_mode=self._config.permission_mode,
                reasoning_effort=effort,
                gemini_api_key=self._config.gemini_api_key,
                docker_container=self._config.docker_container,
                process_registry=self._process_registry,
                chat_id=request.chat_id,
                topic_id=request.topic_id,
                transport=request.transport,
                process_label=request.process_label,
                cli_parameters=self._config.cli_parameters_for_provider(provider),
                agent_name=self._config.agent_name,
                interagent_port=self._config.interagent_port,
                transcribe_command=self._config.transcribe_command,
                video_transcribe_command=self._config.video_transcribe_command,
            )
        )

    def _log_call(self, request: AgentRequest, response: AgentResponse, elapsed_ms: float) -> None:
        status = "error" if response.is_error else "ok"
        logger.info(
            "CLI %s [%s] cost=$%.4f tokens=%d duration_ms=%.0f",
            request.process_label,
            status,
            response.cost_usd,
            response.total_tokens,
            elapsed_ms,
        )


def _cli_response_to_agent_response(
    resp: CLIResponse,
    *,
    stream_fallback: bool = False,
) -> AgentResponse:
    """Convert internal CLIResponse to public AgentResponse."""
    return AgentResponse(
        result=resp.result,
        returncode=resp.returncode,
        session_id=resp.session_id,
        is_error=resp.is_error,
        cost_usd=resp.total_cost_usd or 0.0,
        total_tokens=resp.total_tokens,
        input_tokens=resp.input_tokens,
        num_turns=resp.num_turns or 0,
        timed_out=resp.timed_out,
        duration_ms=resp.duration_ms,
        stream_fallback=stream_fallback,
    )
