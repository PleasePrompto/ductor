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


class _StreamCallbacks:
    """Dispatch stream events to the appropriate callbacks."""

    def __init__(
        self,
        on_text: Callable[[str], Awaitable[None]] | None,
        on_tool: Callable[[str], Awaitable[None]] | None,
        on_status: Callable[[str | None], Awaitable[None]] | None,
    ) -> None:
        self._on_text = on_text
        self._on_tool = on_tool
        self._on_status = on_status

    async def dispatch(self, event: StreamEvent) -> tuple[str, ResultEvent | None]:
        """Handle one event. Returns (accumulated_text_chunk, result_or_none)."""
        if isinstance(event, AssistantTextDelta) and event.text:
            if self._on_text is not None:
                await self._on_text(event.text)
            return event.text, None
        if isinstance(event, ThinkingEvent) and self._on_status is not None:
            await self._on_status("thinking")
        elif isinstance(event, ToolUseEvent) and self._on_tool is not None:
            await self._on_tool(event.tool_name)
        elif isinstance(event, SystemStatusEvent) and self._on_status is not None:
            await self._on_status(event.status)
        elif isinstance(event, CompactBoundaryEvent):
            logger.info(
                "Context compacted (trigger=%s, pre_tokens=%d)",
                event.trigger,
                event.pre_tokens,
            )
            if self._on_status is not None:
                await self._on_status(None)
        elif isinstance(event, ResultEvent):
            return "", event
        return "", None


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
    docker_container: str = ""
    claude_cli_parameters: tuple[str, ...] = ()
    codex_cli_parameters: tuple[str, ...] = ()
    gemini_cli_parameters: tuple[str, ...] = ()

    def cli_parameters_for_provider(self, provider: str) -> list[str]:
        """Return CLI parameters for the given provider."""
        if provider == "codex":
            return list(self.codex_cli_parameters)
        if provider == "gemini":
            return list(self.gemini_cli_parameters)
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

    def update_available_providers(self, providers: frozenset[str]) -> None:
        self._available_providers = providers

    def update_default_model(self, model: str) -> None:
        """Update the default model after /model switch."""
        self._config = replace(self._config, default_model=model)

    def update_reasoning_effort(self, effort: str) -> None:
        """Update the default reasoning effort after wizard selection."""
        self._config = replace(self._config, reasoning_effort=effort)

    def update_docker_container(self, container: str) -> None:
        """Switch Docker container (empty string = host execution)."""
        self._config = replace(self._config, docker_container=container)

    async def execute(self, request: AgentRequest) -> AgentResponse:
        """Execute a CLI call."""
        cli = self._make_cli(request)
        model_name = request.model_override or self._config.default_model
        logger.info(
            "CLI execute starting label=%s model=%s",
            request.process_label,
            model_name,
        )

        t0 = time.monotonic()
        response = await cli.send(
            prompt=request.prompt,
            resume_session=request.resume_session,
            continue_session=request.continue_session,
            timeout_seconds=request.timeout_seconds,
        )
        elapsed_ms = (time.monotonic() - t0) * 1000

        agent_resp = _cli_response_to_agent_response(response)
        self._log_call(request, agent_resp, elapsed_ms)
        return agent_resp

    async def execute_streaming(
        self,
        request: AgentRequest,
        on_text_delta: Callable[[str], Awaitable[None]] | None = None,
        on_tool_activity: Callable[[str], Awaitable[None]] | None = None,
        on_system_status: Callable[[str | None], Awaitable[None]] | None = None,
    ) -> AgentResponse:
        """Execute a streaming CLI call with automatic fallback to non-streaming."""
        cli = self._make_cli(request)
        model_name = request.model_override or self._config.default_model
        logger.info(
            "CLI streaming starting label=%s model=%s",
            request.process_label,
            model_name,
        )

        accumulated_text = ""
        result_event: ResultEvent | None = None
        stream_error = False

        callbacks = _StreamCallbacks(on_text_delta, on_tool_activity, on_system_status)

        try:
            async for event in cli.send_streaming(
                prompt=request.prompt,
                resume_session=request.resume_session,
                continue_session=request.continue_session,
                timeout_seconds=request.timeout_seconds,
            ):
                if self._process_registry.was_aborted(request.chat_id):
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
            )

        logger.info(
            "CLI streaming completed label=%s fallback=%s",
            request.process_label,
            stream_error,
        )
        cli_resp = CLIResponse(
            session_id=result_event.session_id,
            result=result_event.result or accumulated_text,
            is_error=result_event.is_error,
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
    ) -> AgentResponse:
        """Handle failed or incomplete streaming: use accumulated text or retry."""
        was_aborted = self._process_registry.was_aborted(request.chat_id)
        logger.info(
            "Stream fallback: aborted=%s accumulated=%d",
            was_aborted,
            len(accumulated_text),
        )

        if was_aborted:
            return AgentResponse(result="")

        if accumulated_text and not stream_error:
            logger.info(
                "Stream completed without ResultEvent, using %d chars",
                len(accumulated_text),
            )
            return AgentResponse(result=accumulated_text)

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

    def _make_cli(self, request: AgentRequest) -> BaseCLI:
        """Create a BaseCLI instance for the given request."""
        model_name = request.model_override or self._config.default_model

        if request.provider_override:
            model = model_name
            provider = request.provider_override
        elif self._available_providers:
            model, provider = self._models.resolve_for_provider(
                model_name, self._available_providers
            )
        else:
            model = model_name
            provider = self._models.provider_for(model_name)

        return create_cli(
            CLIConfig(
                provider=provider,
                working_dir=self._config.working_dir,
                model=model,
                system_prompt=request.system_prompt,
                append_system_prompt=request.append_system_prompt,
                max_turns=self._config.max_turns,
                max_budget_usd=self._config.max_budget_usd,
                permission_mode=self._config.permission_mode,
                reasoning_effort=self._config.reasoning_effort,
                docker_container=self._config.docker_container,
                process_registry=self._process_registry,
                chat_id=request.chat_id,
                process_label=request.process_label,
                cli_parameters=self._config.cli_parameters_for_provider(provider),
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
        timed_out=resp.timed_out,
        duration_ms=resp.duration_ms,
        stream_fallback=stream_fallback,
    )
