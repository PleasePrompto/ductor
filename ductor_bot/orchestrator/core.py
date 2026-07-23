"""Core orchestrator: routes messages through command and conversation flows."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ductor_bot.background import (
    BackgroundSubmit,
    BackgroundTask,
)
from ductor_bot.cli.process_registry import ProcessRegistry
from ductor_bot.cli.service import CLIService, CLIServiceConfig
from ductor_bot.cli.stream_events import ToolUseEvent
from ductor_bot.cli.types import AgentRequest
from ductor_bot.config import AgentConfig
from ductor_bot.cron.manager import CronManager
from ductor_bot.errors import (
    CLIError,
    CronError,
    SessionError,
    StreamError,
    WebhookError,
    WorkspaceError,
)
from ductor_bot.infra.docker import DockerManager
from ductor_bot.infra.inflight import InflightTracker
from ductor_bot.orchestrator.commands import (
    cmd_cron,
    cmd_diagnose,
    cmd_effort,
    cmd_memory,
    cmd_model,
    cmd_reset,
    cmd_reset_current,
    cmd_sessions,
    cmd_status,
    cmd_tasks,
    cmd_upgrade,
)
from ductor_bot.orchestrator.directives import parse_directives
from ductor_bot.orchestrator.flows import (
    StreamingCallbacks,
    heartbeat_flow,
    named_session_flow,
    named_session_streaming,
    normal,
    normal_streaming,
)
from ductor_bot.orchestrator.hooks import (
    DELEGATION_BRIEF,
    DELEGATION_REMINDER,
    MAINMEMORY_REMINDER,
    MessageHookRegistry,
    build_memory_reflection_hook,
)
from ductor_bot.orchestrator.memory_flush import MemoryFlusher
from ductor_bot.orchestrator.observers import ObserverManager
from ductor_bot.orchestrator.providers import ProviderManager
from ductor_bot.orchestrator.registry import CommandRegistry, OrchestratorResult
from ductor_bot.security import detect_suspicious_patterns
from ductor_bot.session import SessionKey, SessionManager
from ductor_bot.session.manager import SessionData
from ductor_bot.session.named import NamedSessionRegistry
from ductor_bot.webhook.manager import WebhookManager
from ductor_bot.workspace.paths import DuctorPaths
from ductor_bot.workspace.project_roots import resolve_project_root

if TYPE_CHECKING:
    from ductor_bot.background import BackgroundObserver
    from ductor_bot.bus.bus import MessageBus
    from ductor_bot.bus.lock_pool import LockPool
    from ductor_bot.config import ModelRegistry
    from ductor_bot.multiagent.supervisor import AgentSupervisor
    from ductor_bot.session.named import NamedSession
    from ductor_bot.tasks.hub import TaskHub

logger = logging.getLogger(__name__)


_TextCallback = Callable[[str], Awaitable[None]]
_ToolCallback = Callable[[ToolUseEvent], Awaitable[None]]
_SystemStatusCallback = Callable[[str | None], Awaitable[None]]
_ReasoningCallback = Callable[[str], Awaitable[None]]


@dataclass(slots=True)
class NamedSessionRequest:
    """Parameters for submitting a named background session."""

    message_id: int
    thread_id: int | None
    transport: str = "tg"
    provider_override: str | None = None
    model_override: str | None = None
    reasoning_effort_override: str | None = None


@dataclass(slots=True)
class _MessageDispatch:
    """Normalized input for one orchestrator message routing pass."""

    key: SessionKey
    text: str
    cmd: str
    streaming: bool = False
    on_text_delta: _TextCallback | None = None
    on_thinking_delta: _TextCallback | None = None
    on_tool_activity: _ToolCallback | None = None
    on_system_status: _SystemStatusCallback | None = None
    on_reasoning_delta: _ReasoningCallback | None = None

    def streaming_callbacks(self) -> StreamingCallbacks:
        """Bundle the streaming callbacks into a StreamingCallbacks instance."""
        return StreamingCallbacks(
            on_text_delta=self.on_text_delta,
            on_thinking_delta=self.on_thinking_delta,
            on_tool_activity=self.on_tool_activity,
            on_system_status=self.on_system_status,
            on_reasoning_delta=self.on_reasoning_delta,
        )


class Orchestrator:
    """Routes messages through command dispatch and conversation flows."""

    def __init__(
        self,
        config: AgentConfig,
        paths: DuctorPaths,
        *,
        docker_container: str = "",
        agent_name: str = "main",
        interagent_port: int = 8799,
    ) -> None:
        self._config = config
        self._paths: DuctorPaths = paths
        self._docker: DockerManager | None = None
        self._providers = ProviderManager(config)
        self._sessions = SessionManager(paths.sessions_path, config)
        self._named_sessions = NamedSessionRegistry(paths.named_sessions_path)
        self._process_registry = ProcessRegistry()
        self._lock_pool: LockPool | None = None
        self._cli_service = CLIService(
            config=CLIServiceConfig(
                working_dir=str(paths.workspace),
                default_model=config.model,
                provider=config.provider,
                max_turns=config.max_turns,
                max_budget_usd=config.max_budget_usd,
                permission_mode=config.permission_mode,
                reasoning_effort=config.reasoning_effort,
                gemini_api_key=config.gemini_api_key,
                docker_container=docker_container,
                claude_cli_parameters=tuple(config.cli_parameters.claude),
                codex_cli_parameters=tuple(config.cli_parameters.codex),
                gemini_cli_parameters=tuple(config.cli_parameters.gemini),
                antigravity_cli_parameters=tuple(config.cli_parameters.antigravity),
                grok_cli_parameters=tuple(config.cli_parameters.grok),
                agent_name=agent_name,
                interagent_port=interagent_port,
                transcribe_command=config.transcription.audio_command,
                video_transcribe_command=config.transcription.video_command,
            ),
            models=self._providers.models,
            available_providers=frozenset(),
            process_registry=self._process_registry,
        )
        self._cli_service.set_working_dir_resolver(self._resolve_request_working_dir)
        self._cron_manager = CronManager(jobs_path=paths.cron_jobs_path)
        self._webhook_manager = WebhookManager(hooks_path=paths.webhooks_path)
        self._observers = ObserverManager(config, paths)

        async def _heartbeat_handler(
            chat_id: int,
            topic_id: int | None = None,
            prompt: str | None = None,
            ack_token: str | None = None,
            transport: str = "tg",
        ) -> str | None:
            return await self.handle_heartbeat(
                SessionKey.for_transport(transport, chat_id, topic_id),
                prompt=prompt,
                ack_token=ack_token,
            )

        self._observers.heartbeat.set_heartbeat_handler(_heartbeat_handler)
        self._observers.heartbeat.set_busy_check(self.is_chat_busy)
        stale_max = config.cli_timeout * 2
        self._observers.heartbeat.set_stale_cleanup(
            lambda: self._process_registry.kill_stale(stale_max)
        )
        self._api_stop: Callable[[], Awaitable[None]] | None = None
        self._inflight_tracker = InflightTracker(paths.inflight_turns_path)
        self._memory_flusher: MemoryFlusher | None = (
            MemoryFlusher(
                config.memory_flush,
                self._cli_service,
                config.memory_compaction,
                paths,
            )
            if config.memory_flush.enabled
            else None
        )
        self._hook_registry = MessageHookRegistry()
        self._hook_registry.register(MAINMEMORY_REMINDER)
        self._hook_registry.register(DELEGATION_BRIEF)
        self._hook_registry.register(DELEGATION_REMINDER)
        if config.memory_reflection.enabled:
            self._hook_registry.register(build_memory_reflection_hook(config.memory_reflection))
        self._supervisor: AgentSupervisor | None = None  # Set by AgentSupervisor after creation
        self._task_hub: TaskHub | None = None  # Set by supervisor or __main__.py
        self._command_registry = CommandRegistry()
        self._register_commands()

    def _resolve_request_working_dir(self, request: AgentRequest) -> str | None:
        """Map a CLI request to a per-topic project root, if configured.

        Returns ``None`` (keep the default workspace) for named sessions —
        their resume consistency depends on a stable working dir — and when
        no ``project_roots`` entry matches the request's topic.
        """
        if request.process_label.startswith("ns:"):
            return None  # named sessions stay in workspace (resume consistency)
        return resolve_project_root(
            self._config.project_roots,
            chat_id=request.chat_id,
            topic_id=request.topic_id,
            topic_name=self._sessions.resolve_topic_name(request.chat_id, request.topic_id),
        )

    @property
    def paths(self) -> DuctorPaths:
        """Public access to resolved workspace paths."""
        return self._paths

    @property
    def task_hub(self) -> TaskHub | None:
        """Public access to the task hub (None when tasks are disabled)."""
        return self._task_hub

    @property
    def config(self) -> AgentConfig:
        """Public access to the agent config."""
        return self._config

    @property
    def inflight_tracker(self) -> InflightTracker:
        """Public access to the inflight turn tracker."""
        return self._inflight_tracker

    @property
    def named_sessions(self) -> NamedSessionRegistry:
        """Public access to the named session registry."""
        return self._named_sessions

    @property
    def available_providers(self) -> frozenset[str]:
        """Public access to the set of authenticated providers."""
        return self._providers.available_providers

    @property
    def cli_service(self) -> CLIService:
        """Public access to the CLI service."""
        return self._cli_service

    @property
    def process_registry(self) -> ProcessRegistry:
        """Public access to the process registry."""
        return self._process_registry

    @property
    def bg_observer(self) -> BackgroundObserver | None:
        """Public access to the background observer."""
        return self._observers.background

    @property
    def supervisor(self) -> AgentSupervisor | None:
        """Public access to the agent supervisor."""
        return self._supervisor

    @supervisor.setter
    def supervisor(self, value: AgentSupervisor | None) -> None:
        self._supervisor = value

    def set_task_hub(self, hub: TaskHub) -> None:
        """Inject the task hub (called by supervisor or startup wiring)."""
        self._task_hub = hub
        hub.start_maintenance()

    @classmethod
    async def create(
        cls,
        config: AgentConfig,
        *,
        agent_name: str = "main",
    ) -> Orchestrator:
        """Async factory: build Orchestrator.

        Workspace must already be initialized by the caller (``__main__.load_config``).
        """
        from ductor_bot.orchestrator.lifecycle import create_orchestrator

        return await create_orchestrator(config, agent_name=agent_name)

    @property
    def models(self) -> ModelRegistry:
        """Public access to the model registry (delegates to ProviderManager)."""
        return self._providers.models

    @property
    def gemini_api_key_mode(self) -> bool:
        """Return cached Gemini API-key mode status."""
        return self._providers.gemini_api_key_mode

    def refresh_gemini_api_key_mode(self) -> bool:
        """Force a fresh read of Gemini auth settings (bypasses the cache)."""
        return self._providers.refresh_gemini_api_key_mode()

    @property
    def active_provider_name(self) -> str:
        """Human-readable name for the active CLI provider."""
        return self._providers.active_provider_name

    async def handle_message(self, key: SessionKey, text: str) -> OrchestratorResult:
        """Main entry point: route message to appropriate handler."""
        dispatch = _MessageDispatch(key=key, text=text, cmd=text.strip().lower())
        return await self._handle_message_impl(dispatch)

    async def handle_message_streaming(  # noqa: PLR0913
        self,
        key: SessionKey,
        text: str,
        *,
        on_text_delta: _TextCallback | None = None,
        on_thinking_delta: _TextCallback | None = None,
        on_tool_activity: _ToolCallback | None = None,
        on_system_status: _SystemStatusCallback | None = None,
        on_reasoning_delta: _ReasoningCallback | None = None,
    ) -> OrchestratorResult:
        """Main entry point with streaming support."""
        dispatch = _MessageDispatch(
            key=key,
            text=text,
            cmd=text.strip().lower(),
            streaming=True,
            on_text_delta=on_text_delta,
            on_thinking_delta=on_thinking_delta,
            on_tool_activity=on_tool_activity,
            on_system_status=on_system_status,
            on_reasoning_delta=on_reasoning_delta,
        )
        return await self._handle_message_impl(dispatch)

    async def _handle_message_impl(self, dispatch: _MessageDispatch) -> OrchestratorResult:
        self._process_registry.clear_abort(dispatch.key.chat_id)
        self._process_registry.clear_topic_abort(dispatch.key.chat_id, dispatch.key.topic_id)
        logger.info("Message received text=%s", dispatch.cmd[:80])

        patterns = detect_suspicious_patterns(dispatch.text)
        if patterns:
            logger.warning("Suspicious input patterns: %s", ", ".join(patterns))

        try:
            return await self._route_message(dispatch)
        except asyncio.CancelledError:
            raise
        except (CLIError, StreamError, SessionError, CronError, WebhookError, WorkspaceError):
            logger.exception("Domain error in handle_message")
            return OrchestratorResult(text="An internal error occurred. Please try again.")
        except (OSError, RuntimeError, ValueError, TypeError, KeyError):
            logger.exception("Unexpected error in handle_message")
            return OrchestratorResult(text="An internal error occurred. Please try again.")

    async def _route_message(self, dispatch: _MessageDispatch) -> OrchestratorResult:
        result = await self._command_registry.dispatch(
            dispatch.cmd,
            self,
            dispatch.key,
            dispatch.text,
        )
        if result is not None:
            return result

        await self._ensure_docker()

        # _known_model_ids only covers Claude + Gemini IDs (refreshed on Gemini
        # cache updates).  Codex model IDs live in a separate dynamic cache, so
        # merge them in here before parsing so that directives like @gpt-5.4
        # route to the Codex provider instead of falling through to the default.
        known_ids = self._providers._known_model_ids
        codex_cache = self._providers._codex_cache_fn() if self._providers._codex_cache_fn else None
        if codex_cache is not None:
            known_ids = known_ids | frozenset(m.id for m in codex_cache.models)
        directives = parse_directives(dispatch.text, known_ids)

        # Check if a leading @directive matches a named session
        if directives.raw_directives:
            first_key = next(iter(directives.raw_directives))
            ns = self._named_sessions.get(dispatch.key.chat_id, first_key)
            if ns is not None:
                session_prompt = directives.cleaned or dispatch.text
                if dispatch.streaming:
                    return await named_session_streaming(
                        self,
                        dispatch.key,
                        first_key,
                        session_prompt,
                        cbs=dispatch.streaming_callbacks(),
                    )
                return await named_session_flow(self, dispatch.key, first_key, session_prompt)

        if directives.is_directive_only and directives.has_model:
            return OrchestratorResult(
                text=f"Next message will use: {directives.model}\n"
                f"(Send a message with @{directives.model} <text> to use it.)",
            )

        prompt_text = directives.cleaned or dispatch.text

        if dispatch.streaming:
            return await normal_streaming(
                self,
                dispatch.key,
                prompt_text,
                model_override=directives.model,
                cbs=dispatch.streaming_callbacks(),
            )

        return await normal(
            self,
            dispatch.key,
            prompt_text,
            model_override=directives.model,
        )

    def _register_commands(self) -> None:
        reg = self._command_registry
        reg.register_async("/new", cmd_reset)
        reg.register_async("/reset", cmd_reset_current)
        reg.register_async("/reset ", cmd_reset_current)
        # /stop is handled entirely by the Middleware abort path (before the lock)
        # and never reaches the orchestrator command registry.
        reg.register_async("/status", cmd_status)
        reg.register_async("/model", cmd_model)
        reg.register_async("/model ", cmd_model)
        reg.register_async("/effort", cmd_effort)
        reg.register_async("/memory", cmd_memory)
        reg.register_async("/cron", cmd_cron)
        reg.register_async("/diagnose", cmd_diagnose)
        reg.register_async("/upgrade", cmd_upgrade)
        reg.register_async("/sessions", cmd_sessions)
        reg.register_async("/tasks", cmd_tasks)

    def register_multiagent_commands(self) -> None:
        """Register /agents, /agent_start, /agent_stop, /agent_restart commands.

        Called by the AgentSupervisor after setting ``_supervisor``.
        """
        from ductor_bot.multiagent.commands import (
            cmd_agent_restart,
            cmd_agent_start,
            cmd_agent_stop,
            cmd_agents,
        )

        reg = self._command_registry
        reg.register_async("/agents", cmd_agents)
        reg.register_async("/agent_start", cmd_agent_start)
        reg.register_async("/agent_start ", cmd_agent_start)
        reg.register_async("/agent_stop", cmd_agent_stop)
        reg.register_async("/agent_stop ", cmd_agent_stop)
        reg.register_async("/agent_restart", cmd_agent_restart)
        reg.register_async("/agent_restart ", cmd_agent_restart)
        logger.info("Multi-agent commands registered")

    async def reset_current_provider_session(self, key: SessionKey) -> str:
        """Reset the bucket the user is currently on, keeping that provider active.

        Unlike ``reset_active_provider_session`` (``/new`` -> config default), this
        clears the *currently active* provider's bucket and stays on it.
        """
        active = await self._sessions.get_active(key)
        if active is None:
            model, provider = self.resolve_runtime_target(self._config.model)
        else:
            model, provider = active.model, active.provider
        await self._sessions.reset_provider_session(key, provider=provider, model=model)
        logger.info("Current provider session reset provider=%s model=%s", provider, model)
        return provider

    async def reset_active_provider_session(self, key: SessionKey) -> str:
        """Reset the active provider bucket to the config-default model.

        ``/new`` acts as a "factory reset" -- the bucket cleared is the one
        tied to ``config.model`` (resolved via provider mapping), not the
        bucket the user last switched to via ``/model``. This matches the
        behaviour users expect from a reset command (issue #82).
        """
        model, provider = self.resolve_runtime_target(self._config.model)
        await self._sessions.reset_provider_session(
            key,
            provider=provider,
            model=model,
        )
        logger.info("Active provider session reset provider=%s model=%s", provider, model)
        return provider

    async def abort(self, chat_id: int, topic_id: int | None = None) -> int:
        """Kill active CLI processes for *chat_id* (optionally scoped to *topic_id*).

        When ``topic_id`` is provided (``/stop`` from a specific topic),
        only the foreground CLI processes registered under that
        ``(chat_id, topic_id)`` pair are killed. Background tasks and
        named sessions are left alone — they are not topic-tagged in
        the current model and have their own management surfaces
        (``/tasks``, ``/sessions``) so /stop should not double up.

        When ``topic_id`` is ``None`` (legacy callers / ``/stop_all``)
        the chat-wide sweep runs as before: every process for the chat
        plus every background task and named session.
        """
        if topic_id is not None:
            return await self._process_registry.kill_by_chat_topic(chat_id, topic_id)
        killed = await self._process_registry.kill_all(chat_id)
        if self._observers.background:
            killed += await self._observers.background.cancel_all(chat_id)
        self._named_sessions.end_all(chat_id)
        return killed

    def interrupt(self, chat_id: int) -> int:
        """Send SIGINT to active CLI processes for *chat_id*.

        Unlike :meth:`abort` this does not kill or unregister the processes.
        It sends a soft interrupt so the CLI can cancel the current tool
        execution (equivalent to pressing ESC in the terminal).
        """
        return self._process_registry.interrupt_all(chat_id)

    async def abort_all(self) -> int:
        """Kill all active CLI processes across all chats on this agent."""
        return await self._process_registry.kill_all_active()

    def resolve_runtime_target(self, requested_model: str | None = None) -> tuple[str, str]:
        """Resolve requested model to the effective ``(model, provider)`` pair."""
        return self._providers.resolve_runtime_target(requested_model)

    def wire_observers_to_bus(
        self,
        bus: MessageBus,
        *,
        wake_handler: Callable[[int, str], Awaitable[str | None]] | None = None,
    ) -> None:
        """Wire all observer result callbacks to the message bus."""
        self._observers.wire_to_bus(bus, wake_handler=wake_handler)
        bus.set_injector(self)
        self._lock_pool = bus.lock_pool
        # Share the bus lock pool with MemoryFlusher so silent flush / compact
        # turns serialize against concurrent user turns on the same SessionKey.
        if self._memory_flusher is not None:
            self._memory_flusher.set_lock_pool(bus.lock_pool)

    async def handle_heartbeat(
        self,
        key: SessionKey,
        *,
        prompt: str | None = None,
        ack_token: str | None = None,
    ) -> str | None:
        """Run a heartbeat turn in the main session. Returns alert text or None."""
        logger.debug("Heartbeat flow starting")
        if self._lock_pool is not None:
            lock = self._lock_pool.get(key.lock_key)
            async with lock:
                return await heartbeat_flow(self, key, prompt=prompt, ack_token=ack_token)
        return await heartbeat_flow(self, key, prompt=prompt, ack_token=ack_token)

    def submit_named_session(
        self,
        chat_id: int,
        prompt: str,
        request: NamedSessionRequest,
    ) -> tuple[str, str]:
        """Submit a new named background session. Returns (task_id, session_name)."""
        from ductor_bot.cli.param_resolver import resolve_cli_config

        if self._observers.background is None:
            msg = "Background observer not initialized"
            raise RuntimeError(msg)

        model_name, provider_name = self.resolve_runtime_target(self._config.model)
        if request.provider_override:
            provider_name = request.provider_override
            model_name = request.model_override or self.default_model_for_provider(
                request.provider_override
            )

        # Effective effort at creation: explicit override or the global default
        # (mirrors model_name from self._config.model). Re-validate against the
        # session's resolved provider and reset to a safe default when the
        # carried-over effort is unsupported (e.g. global Claude ``max`` -> a
        # @codex session) so an invalid value never reaches the CLI. Mirrors the
        # provider-switch reset in model_selector.switch_model.
        from ductor_bot.orchestrator.selectors.model_selector import _validate_reasoning_effort

        effort = request.reasoning_effort_override or self._config.reasoning_effort
        if effort and _validate_reasoning_effort(self, model_name, effort) is not None:
            effort = "medium"
        ns = self._named_sessions.create(
            chat_id,
            provider_name,
            model_name,
            prompt,
            reasoning_effort=effort,
            key=SessionKey.for_transport(request.transport, chat_id, request.thread_id),
        )
        exec_config = resolve_cli_config(self._config, self._observers.codex_cache)
        sub = BackgroundSubmit(
            chat_id=chat_id,
            prompt=prompt,
            message_id=request.message_id,
            thread_id=request.thread_id,
            transport=request.transport,
            session_name=ns.name,
            provider_override=provider_name,
            model_override=model_name,
            reasoning_effort_override=effort,
        )
        task_id = self._observers.background.submit(sub, exec_config)
        return task_id, ns.name

    def submit_named_followup_bg(
        self,
        chat_id: int,
        session_name: str,
        prompt: str,
        message_id: int,
        thread_id: int | None,
    ) -> str:
        """Submit a background follow-up to an existing named session. Returns task_id."""
        from ductor_bot.cli.param_resolver import resolve_cli_config

        if self._observers.background is None:
            msg = "Background observer not initialized"
            raise RuntimeError(msg)

        ns = self._named_sessions.get(chat_id, session_name)
        if ns is None:
            msg = f"Session '{session_name}' not found"
            raise ValueError(msg)
        if ns.status == "ended":
            msg = f"Session '{session_name}' has ended"
            raise ValueError(msg)
        if ns.status == "running":
            msg = f"Session '{session_name}' is still processing"
            raise ValueError(msg)

        self._named_sessions.mark_running(
            chat_id,
            session_name,
            prompt,
            transport=ns.transport,
            topic_id=thread_id,
        )
        exec_config = resolve_cli_config(self._config, self._observers.codex_cache)
        # Follow-up keeps the session's provider/model; defensively re-validate
        # the carried-over effort against ns.model (resets a stale/unsupported
        # value to a safe default before it reaches the CLI).
        from ductor_bot.orchestrator.selectors.model_selector import _validate_reasoning_effort

        followup_effort = ns.reasoning_effort
        if (
            followup_effort
            and _validate_reasoning_effort(self, ns.model, followup_effort) is not None
        ):
            followup_effort = "medium"
        sub = BackgroundSubmit(
            chat_id=chat_id,
            prompt=prompt,
            message_id=message_id,
            thread_id=thread_id if thread_id is not None else ns.topic_id,
            transport=ns.transport,
            session_name=session_name,
            resume_session_id=ns.session_id,
            provider_override=ns.provider,
            model_override=ns.model,
            reasoning_effort_override=followup_effort,
        )
        return self._observers.background.submit(sub, exec_config)

    async def end_named_session(self, chat_id: int, name: str) -> bool:
        """Kill process and end a named session."""
        ns = self._named_sessions.get(chat_id, name)
        if ns is None:
            return False
        await self._process_registry.kill_by_label(chat_id, f"ns:{name}")
        self._process_registry.clear_label_abort(chat_id, f"ns:{name}")
        return self._named_sessions.end_session(chat_id, name)

    def is_known_model(self, candidate: str) -> bool:
        """Return True if *candidate* is a recognized model ID for any provider."""
        return self._providers.is_known_model(candidate)

    def default_model_for_provider(self, provider: str) -> str:
        """Return the default model ID for a provider, or empty string if unknown."""
        return self._providers.default_model_for_provider(provider)

    def resolve_session_directive(self, key: str) -> tuple[str, str] | None:
        """Resolve a ``@key`` directive to ``(provider, model)`` or ``None``."""
        return self._providers.resolve_session_directive(key)

    def get_named_session(self, chat_id: int, name: str) -> NamedSession | None:
        """Look up a named session."""
        return self._named_sessions.get(chat_id, name)

    def list_named_sessions(self, chat_id: int) -> list[NamedSession]:
        """List active named sessions for a chat."""
        return self._named_sessions.list_active(chat_id)

    async def list_topic_sessions(self, chat_id: int) -> list[SessionData]:
        """Return fresh topic sessions for *chat_id*."""
        all_sessions = await self._sessions.list_active_for_chat(chat_id)
        return [s for s in all_sessions if s.topic_id is not None]

    def active_background_tasks(self, chat_id: int | None = None) -> list[BackgroundTask]:
        """Return active background tasks, optionally filtered by chat_id."""
        if self._observers.background is None:
            return []
        return self._observers.background.active_tasks(chat_id)

    def is_chat_busy(self, chat_id: int, topic_id: int | None = None) -> bool:
        """Check if a chat has active CLI processes."""
        if self._process_registry.has_active(chat_id, topic_id):
            return True
        if self._lock_pool is None:
            return False
        if topic_id is not None:
            return self._lock_pool.is_locked((chat_id, topic_id))
        return self._lock_pool.any_locked_for_chat(chat_id)

    async def _ensure_docker(self) -> None:
        """Health-check Docker before CLI calls; auto-recover or fall back."""
        from ductor_bot.orchestrator.lifecycle import ensure_docker

        await ensure_docker(self)

    def set_config_hot_reload_handler(
        self,
        handler: Callable[[AgentConfig, dict[str, object]], None],
    ) -> None:
        """Register an external hot-reload callback (e.g. TelegramBot auth update)."""
        self._config_hot_reload_handler = handler

    def _on_config_hot_reload(self, config: AgentConfig, hot: dict[str, object]) -> None:
        """Apply hot-reloaded config fields to dependent services."""
        if any(
            k in hot
            for k in (
                "model",
                "provider",
                "max_turns",
                "max_budget_usd",
                "permission_mode",
                "reasoning_effort",
                "cli_parameters",
            )
        ):
            self._cli_service.update_config(
                CLIServiceConfig(
                    working_dir=str(self._paths.workspace),
                    default_model=config.model,
                    provider=config.provider,
                    max_turns=config.max_turns,
                    max_budget_usd=config.max_budget_usd,
                    permission_mode=config.permission_mode,
                    reasoning_effort=config.reasoning_effort,
                    gemini_api_key=config.gemini_api_key,
                    docker_container=self._cli_service._config.docker_container,
                    claude_cli_parameters=tuple(config.cli_parameters.claude),
                    codex_cli_parameters=tuple(config.cli_parameters.codex),
                    gemini_cli_parameters=tuple(config.cli_parameters.gemini),
                    antigravity_cli_parameters=tuple(config.cli_parameters.antigravity),
                    transcribe_command=config.transcription.audio_command,
                    video_transcribe_command=config.transcription.video_command,
                )
            )

        if "model" in hot:
            self._providers.refresh_known_model_ids()

        if "language" in hot:
            from ductor_bot.i18n import init as init_i18n

            init_i18n(config.language)

        if "heartbeat" in hot:
            hb = self._observers.heartbeat
            if config.heartbeat.enabled and not hb.running:
                task = asyncio.create_task(hb.start())
                task.add_done_callback(lambda _: None)
                logger.info("Heartbeat observer started via hot-reload")
            elif not config.heartbeat.enabled and hb.running:
                task = asyncio.create_task(hb.stop())
                task.add_done_callback(lambda _: None)
                logger.info("Heartbeat observer stopped via hot-reload")

        handler = getattr(self, "_config_hot_reload_handler", None)
        if handler is not None:
            handler(config, hot)

        logger.info("Hot-reload applied to orchestrator services")

    # -- Inter-agent communication ------------------------------------------

    async def handle_interagent_message(
        self,
        sender: str,
        message: str,
        *,
        new_session: bool = False,
        source_chat_id: int = 0,
        source_topic_id: int | None = None,
    ) -> tuple[str, str, str]:
        """Process a message from another agent via the InterAgentBus."""
        from ductor_bot.orchestrator.injection import (
            handle_interagent_message as _handle_ia,
        )

        return await _handle_ia(
            self,
            sender,
            message,
            new_session=new_session,
            source_chat_id=source_chat_id,
            source_topic_id=source_topic_id,
        )

    async def inject_prompt(
        self,
        prompt: str,
        chat_id: int,
        label: str,
        *,
        topic_id: int | None = None,
        transport: str = "tg",
    ) -> str:
        """Execute *prompt* in the active session (fulfils ``SessionInjector`` protocol)."""
        from ductor_bot.orchestrator.injection import _inject_prompt

        return await _inject_prompt(
            self, prompt, chat_id, label, topic_id=topic_id, transport=transport
        )

    async def shutdown(self) -> None:
        """Cleanup on bot shutdown."""
        from ductor_bot.orchestrator.lifecycle import shutdown

        await shutdown(self)
