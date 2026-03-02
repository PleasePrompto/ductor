"""Core orchestrator: routes messages through command and conversation flows."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import secrets
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ductor_bot.background import (
    BackgroundObserver,
    BackgroundResult,
    BackgroundSubmit,
    BackgroundTask,
)
from ductor_bot.cleanup import CleanupObserver
from ductor_bot.cli.codex_cache import CodexModelCache
from ductor_bot.cli.codex_cache_observer import CodexCacheObserver
from ductor_bot.cli.gemini_cache_observer import GeminiCacheObserver
from ductor_bot.cli.process_registry import ProcessRegistry
from ductor_bot.cli.service import CLIService, CLIServiceConfig
from ductor_bot.config import (
    _GEMINI_ALIASES,
    CLAUDE_MODELS,
    AgentConfig,
    ModelRegistry,
    get_gemini_models,
    set_gemini_models,
)
from ductor_bot.config_reload import ConfigReloader
from ductor_bot.cron.manager import CronManager
from ductor_bot.cron.observer import CronObserver
from ductor_bot.errors import (
    CLIError,
    CronError,
    SessionError,
    StreamError,
    WebhookError,
    WorkspaceError,
)
from ductor_bot.files.allowed_roots import resolve_allowed_roots
from ductor_bot.heartbeat import HeartbeatObserver
from ductor_bot.infra.docker import DockerManager
from ductor_bot.orchestrator.commands import (
    cmd_cron,
    cmd_diagnose,
    cmd_memory,
    cmd_model,
    cmd_reset,
    cmd_sessions,
    cmd_status,
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
from ductor_bot.orchestrator.hooks import MAINMEMORY_REMINDER, MessageHookRegistry
from ductor_bot.orchestrator.registry import CommandRegistry, OrchestratorResult
from ductor_bot.security import detect_suspicious_patterns
from ductor_bot.session import SessionManager
from ductor_bot.session.named import NamedSession, NamedSessionRegistry
from ductor_bot.webhook.manager import WebhookManager
from ductor_bot.webhook.models import WebhookResult
from ductor_bot.webhook.observer import WebhookObserver
from ductor_bot.workspace.init import (
    inject_runtime_environment,
    watch_rule_files,
)
from ductor_bot.workspace.paths import DuctorPaths, resolve_paths
from ductor_bot.workspace.skill_sync import (
    cleanup_ductor_links,
    sync_bundled_skills,
    sync_skills,
    watch_skill_sync,
)

if TYPE_CHECKING:
    from ductor_bot.cli.auth import AuthResult, AuthStatus
    from ductor_bot.multiagent.bus import AsyncInterAgentResult
    from ductor_bot.multiagent.supervisor import AgentSupervisor

logger = logging.getLogger(__name__)


_TextCallback = Callable[[str], Awaitable[None]]
_SystemStatusCallback = Callable[[str | None], Awaitable[None]]


@dataclass(slots=True)
class NamedSessionRequest:
    """Parameters for submitting a named background session."""

    message_id: int
    thread_id: int | None
    provider_override: str | None = None
    model_override: str | None = None


@dataclass(slots=True)
class _MessageDispatch:
    """Normalized input for one orchestrator message routing pass."""

    chat_id: int
    text: str
    cmd: str
    streaming: bool = False
    on_text_delta: _TextCallback | None = None
    on_tool_activity: _TextCallback | None = None
    on_system_status: _SystemStatusCallback | None = None

    def streaming_callbacks(self) -> StreamingCallbacks:
        """Bundle the streaming callbacks into a StreamingCallbacks instance."""
        return StreamingCallbacks(
            on_text_delta=self.on_text_delta,
            on_tool_activity=self.on_tool_activity,
            on_system_status=self.on_system_status,
        )


def _docker_skill_resync(paths: DuctorPaths) -> None:
    """Re-run skill sync with copies so skills resolve inside Docker."""
    sync_bundled_skills(paths, docker_active=True)
    sync_skills(paths, docker_active=True)


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
        self._models = ModelRegistry()
        self._known_model_ids: frozenset[str] = frozenset()
        self._refresh_known_model_ids()
        self._sessions = SessionManager(paths.sessions_path, config)
        self._named_sessions = NamedSessionRegistry(paths.named_sessions_path)
        self._process_registry = ProcessRegistry()
        self._available_providers: frozenset[str] = frozenset()
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
                agent_name=agent_name,
                interagent_port=interagent_port,
            ),
            models=self._models,
            available_providers=frozenset(),
            process_registry=self._process_registry,
        )
        self._cron_manager = CronManager(jobs_path=paths.cron_jobs_path)
        self._cron_observer: CronObserver | None = None  # Created in create() after cache init
        self._heartbeat = HeartbeatObserver(config)
        self._heartbeat.set_heartbeat_handler(self.handle_heartbeat)
        self._heartbeat.set_busy_check(self._process_registry.has_active)
        stale_max = config.cli_timeout * 2
        self._heartbeat.set_stale_cleanup(lambda: self._process_registry.kill_stale(stale_max))
        self._webhook_manager = WebhookManager(hooks_path=paths.webhooks_path)
        self._webhook_observer: WebhookObserver | None = (
            None  # Created in create() after cache init
        )
        self._bg_observer: BackgroundObserver | None = None
        self._codex_cache: CodexModelCache | None = None
        self._api_stop: Callable[[], Awaitable[None]] | None = None
        self._cleanup_observer = CleanupObserver(config, paths)
        self._codex_cache_observer: CodexCacheObserver | None = None
        self._gemini_cache_observer: GeminiCacheObserver | None = None
        self._rule_sync_task: asyncio.Task[None] | None = None
        self._skill_sync_task: asyncio.Task[None] | None = None
        self._gemini_api_key_mode: bool | None = None
        self._hook_registry = MessageHookRegistry()
        self._hook_registry.register(MAINMEMORY_REMINDER)
        self._supervisor: AgentSupervisor | None = None  # Set by AgentSupervisor after creation
        self._command_registry = CommandRegistry()
        self._register_commands()
        self._config_reloader: ConfigReloader | None = None

    @property
    def paths(self) -> DuctorPaths:
        """Public access to resolved workspace paths."""
        return self._paths

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
        paths = resolve_paths(ductor_home=config.ductor_home)

        # Only set the process-wide env var for the main agent to avoid
        # race conditions in multi-agent mode (sub-agents use per-subprocess env).
        if agent_name == "main":
            os.environ["DUCTOR_HOME"] = str(paths.ductor_home)

        docker_container = ""
        docker_mgr: DockerManager | None = None
        if config.docker.enabled:
            docker_mgr = DockerManager(config.docker, paths)
            container = await docker_mgr.setup()
            if container:
                docker_container = container
            else:
                logger.warning("Docker enabled but setup failed; running on host")

        if docker_container:
            await asyncio.to_thread(_docker_skill_resync, paths)

        await asyncio.to_thread(
            inject_runtime_environment,
            paths,
            docker_container=docker_container,
            agent_name=agent_name,
        )

        orch = cls(config, paths, docker_container=docker_container, agent_name=agent_name)
        orch._docker = docker_mgr

        from ductor_bot.cli.auth import AuthStatus, check_all_auth

        auth_results = await asyncio.to_thread(check_all_auth)
        orch._apply_auth_results(auth_results, auth_status_enum=AuthStatus)

        if not orch._available_providers:
            logger.error("No authenticated providers found! CLI calls will fail.")
        else:
            logger.info("Available providers: %s", ", ".join(sorted(orch._available_providers)))

        await asyncio.to_thread(orch._init_gemini_state)

        safe_codex_cache = await orch._init_model_caches(paths)
        orch._codex_cache = safe_codex_cache
        orch._bg_observer = BackgroundObserver(
            paths, timeout_seconds=config.cli_timeout, cli_service=orch._cli_service
        )
        orch._cron_observer = CronObserver(
            paths,
            orch._cron_manager,
            config=config,
            codex_cache=safe_codex_cache,
        )
        orch._webhook_observer = WebhookObserver(
            paths,
            orch._webhook_manager,
            config=config,
            codex_cache=safe_codex_cache,
        )

        await orch._cron_observer.start()
        await orch._heartbeat.start()
        await orch._webhook_observer.start()
        await orch._cleanup_observer.start()

        # Direct API server (WebSocket, designed for Tailscale)
        if config.api.enabled:
            await orch._start_api_server(config, paths)
        orch._rule_sync_task = asyncio.create_task(watch_rule_files(paths.workspace))
        logger.info("Rule file watcher started (CLAUDE.md <-> AGENTS.md <-> GEMINI.md)")
        orch._skill_sync_task = asyncio.create_task(
            watch_skill_sync(paths, docker_active=bool(docker_container))
        )
        logger.info("Skill sync watcher started")

        orch._config_reloader = ConfigReloader(
            paths.config_path,
            config,
            on_hot_reload=orch._on_config_hot_reload,
            on_restart_needed=lambda fields: logger.warning(
                "Config changed but requires restart: %s", ", ".join(fields)
            ),
        )
        await orch._config_reloader.start()

        return orch

    async def _init_model_caches(self, paths: DuctorPaths) -> CodexModelCache:
        """Start Gemini and Codex cache observers, return Codex cache."""
        # Gemini cache observer
        gemini_cache_path = paths.config_path.parent / "gemini_models.json"

        def _on_gemini_refresh(models: tuple[str, ...]) -> None:
            set_gemini_models(frozenset(models))
            self._refresh_known_model_ids()
            self._gemini_api_key_mode = None  # Invalidate to re-check on next access

        gemini_observer = GeminiCacheObserver(gemini_cache_path, on_refresh=_on_gemini_refresh)
        await gemini_observer.start()
        self._gemini_cache_observer = gemini_observer

        if not get_gemini_models():
            logger.warning("Gemini cache is empty after startup (Gemini may not be installed)")

        # Codex cache observer
        codex_cache_path = paths.config_path.parent / "codex_models.json"
        codex_observer = CodexCacheObserver(codex_cache_path)
        await codex_observer.start()
        self._codex_cache_observer = codex_observer
        codex_cache = codex_observer.get_cache()

        if not codex_cache or not codex_cache.models:
            logger.warning("Codex cache is empty after startup (Codex may not be authenticated)")

        return codex_cache or CodexModelCache("", [])

    def _refresh_known_model_ids(self) -> None:
        """Refresh directive-known model IDs from dynamic provider registries."""
        self._known_model_ids = CLAUDE_MODELS | _GEMINI_ALIASES | get_gemini_models()

    def _apply_auth_results(
        self,
        auth_results: dict[str, AuthResult],
        *,
        auth_status_enum: type[AuthStatus],
    ) -> None:
        """Log provider auth states and update the runtime provider set."""
        authenticated = auth_status_enum.AUTHENTICATED
        installed = auth_status_enum.INSTALLED

        for provider, result in auth_results.items():
            if result.status == authenticated:
                logger.info("Provider [%s]: authenticated", provider)
            elif result.status == installed:
                logger.warning("Provider [%s]: installed but NOT authenticated", provider)
            else:
                logger.info("Provider [%s]: not found", provider)

        self._available_providers = frozenset(
            name for name, res in auth_results.items() if res.is_authenticated
        )
        self._cli_service.update_available_providers(self._available_providers)

    def _init_gemini_state(self) -> None:
        """Cache Gemini API-key mode and trust workspace once at startup."""
        from ductor_bot.cli.auth import gemini_uses_api_key_mode

        self._gemini_api_key_mode = gemini_uses_api_key_mode()
        if "gemini" in self._available_providers:
            from ductor_bot.cli.gemini_utils import trust_workspace

            trust_workspace(self._paths.workspace)

    @property
    def gemini_api_key_mode(self) -> bool:
        """Return cached Gemini API-key mode status."""
        if self._gemini_api_key_mode is None:
            from ductor_bot.cli.auth import gemini_uses_api_key_mode

            self._gemini_api_key_mode = gemini_uses_api_key_mode()
        return self._gemini_api_key_mode

    def _build_provider_info(self) -> list[dict[str, object]]:
        """Build provider metadata for the API auth_ok response.

        Only includes authenticated providers.
        """
        provider_meta: dict[str, tuple[str, str]] = {
            "claude": ("Claude Code", "#F97316"),
            "gemini": ("Gemini", "#8B5CF6"),
            "codex": ("Codex", "#10B981"),
        }
        providers: list[dict[str, object]] = []
        for pid in sorted(self._available_providers):
            name, color = provider_meta.get(pid, (pid.title(), "#A1A1AA"))
            models: list[str]
            if pid == "claude":
                models = sorted(CLAUDE_MODELS)
            elif pid == "gemini":
                gemini = get_gemini_models()
                models = sorted(gemini) if gemini else sorted(_GEMINI_ALIASES)
            elif pid == "codex":
                cache = (
                    self._codex_cache_observer.get_cache() if self._codex_cache_observer else None
                )
                models = [m.id for m in cache.models] if cache and cache.models else []
            else:
                models = []
            providers.append({"id": pid, "name": name, "color": color, "models": models})
        return providers

    async def handle_message(self, chat_id: int, text: str) -> OrchestratorResult:
        """Main entry point: route message to appropriate handler."""
        dispatch = _MessageDispatch(chat_id=chat_id, text=text, cmd=text.strip().lower())
        return await self._handle_message_impl(dispatch)

    async def handle_message_streaming(
        self,
        chat_id: int,
        text: str,
        *,
        on_text_delta: _TextCallback | None = None,
        on_tool_activity: _TextCallback | None = None,
        on_system_status: _SystemStatusCallback | None = None,
    ) -> OrchestratorResult:
        """Main entry point with streaming support."""
        dispatch = _MessageDispatch(
            chat_id=chat_id,
            text=text,
            cmd=text.strip().lower(),
            streaming=True,
            on_text_delta=on_text_delta,
            on_tool_activity=on_tool_activity,
            on_system_status=on_system_status,
        )
        return await self._handle_message_impl(dispatch)

    async def _handle_message_impl(self, dispatch: _MessageDispatch) -> OrchestratorResult:
        self._process_registry.clear_abort(dispatch.chat_id)
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
            dispatch.chat_id,
            dispatch.text,
        )
        if result is not None:
            return result

        await self._ensure_docker()

        directives = parse_directives(dispatch.text, self._known_model_ids)

        # Check if a leading @directive matches a named session
        if directives.raw_directives:
            first_key = next(iter(directives.raw_directives))
            ns = self._named_sessions.get(dispatch.chat_id, first_key)
            if ns is not None:
                session_prompt = directives.cleaned or dispatch.text
                if dispatch.streaming:
                    return await named_session_streaming(
                        self,
                        dispatch.chat_id,
                        first_key,
                        session_prompt,
                        cbs=dispatch.streaming_callbacks(),
                    )
                return await named_session_flow(self, dispatch.chat_id, first_key, session_prompt)

        if directives.is_directive_only and directives.has_model:
            return OrchestratorResult(
                text=f"Next message will use: {directives.model}\n"
                f"(Send a message with @{directives.model} <text> to use it.)",
            )

        prompt_text = directives.cleaned or dispatch.text

        if dispatch.streaming:
            return await normal_streaming(
                self,
                dispatch.chat_id,
                prompt_text,
                model_override=directives.model,
                cbs=dispatch.streaming_callbacks(),
            )

        return await normal(
            self,
            dispatch.chat_id,
            prompt_text,
            model_override=directives.model,
        )

    def _register_commands(self) -> None:
        reg = self._command_registry
        reg.register_async("/new", cmd_reset)
        # /stop is handled entirely by the Middleware abort path (before the lock)
        # and never reaches the orchestrator command registry.
        reg.register_async("/status", cmd_status)
        reg.register_async("/model", cmd_model)
        reg.register_async("/model ", cmd_model)
        reg.register_async("/memory", cmd_memory)
        reg.register_async("/cron", cmd_cron)
        reg.register_async("/diagnose", cmd_diagnose)
        reg.register_async("/upgrade", cmd_upgrade)
        reg.register_async("/sessions", cmd_sessions)

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

    async def reset_session(self, chat_id: int) -> None:
        """Reset the session for a given chat."""
        await self._sessions.reset_session(chat_id)
        logger.info("Session reset")

    async def reset_active_provider_session(self, chat_id: int) -> str:
        """Reset only the active provider session bucket for a given chat."""
        active = await self._sessions.get_active(chat_id)
        if active is not None:
            provider = active.provider
            model = active.model
        else:
            model, provider = self.resolve_runtime_target(self._config.model)

        await self._sessions.reset_provider_session(
            chat_id,
            provider=provider,
            model=model,
        )
        logger.info("Active provider session reset provider=%s", provider)
        return provider

    async def abort(self, chat_id: int) -> int:
        """Kill all active CLI processes and background tasks for chat_id."""
        killed = await self._process_registry.kill_all(chat_id)
        if self._bg_observer:
            killed += await self._bg_observer.cancel_all(chat_id)
        self._named_sessions.end_all(chat_id)
        return killed

    def resolve_runtime_target(self, requested_model: str | None = None) -> tuple[str, str]:
        """Resolve requested model to the effective ``(model, provider)`` pair."""
        model_name = requested_model or self._config.model
        return model_name, self._models.provider_for(model_name)

    def set_cron_result_handler(
        self,
        handler: Callable[[str, str, str], Awaitable[None]],
    ) -> None:
        """Forward cron job results to an external handler (e.g. Telegram)."""
        if self._cron_observer:
            self._cron_observer.set_result_handler(handler)

    def set_heartbeat_handler(
        self,
        handler: Callable[[int, str], Awaitable[None]],
    ) -> None:
        """Forward heartbeat alert messages to an external handler (e.g. Telegram)."""
        self._heartbeat.set_result_handler(handler)

    async def handle_heartbeat(self, chat_id: int) -> str | None:
        """Run a heartbeat turn in the main session. Returns alert text or None."""
        logger.debug("Heartbeat flow starting")
        return await heartbeat_flow(self, chat_id)

    def set_webhook_result_handler(
        self,
        handler: Callable[[WebhookResult], Awaitable[None]],
    ) -> None:
        """Forward webhook results to an external handler (e.g. Telegram)."""
        if self._webhook_observer:
            self._webhook_observer.set_result_handler(handler)

    def set_webhook_wake_handler(
        self,
        handler: Callable[[int, str], Awaitable[str | None]],
    ) -> None:
        """Set the webhook wake handler (provided by the bot layer)."""
        if self._webhook_observer:
            self._webhook_observer.set_wake_handler(handler)

    def set_session_result_handler(
        self,
        handler: Callable[[BackgroundResult], Awaitable[None]],
    ) -> None:
        """Forward background task results to an external handler (e.g. Telegram)."""
        if self._bg_observer:
            self._bg_observer.set_result_handler(handler)

    def submit_background_task(
        self,
        chat_id: int,
        prompt: str,
        message_id: int,
        thread_id: int | None,
    ) -> str:
        """Submit a background task using the current provider/model. Returns task_id."""
        from ductor_bot.cli.param_resolver import resolve_cli_config

        if self._bg_observer is None:
            msg = "Background observer not initialized"
            raise RuntimeError(msg)
        exec_config = resolve_cli_config(self._config, self._codex_cache)
        sub = BackgroundSubmit(
            chat_id=chat_id, prompt=prompt, message_id=message_id, thread_id=thread_id
        )
        return self._bg_observer.submit(sub, exec_config)

    def submit_named_session(
        self,
        chat_id: int,
        prompt: str,
        request: NamedSessionRequest,
    ) -> tuple[str, str]:
        """Submit a new named background session. Returns (task_id, session_name)."""
        from ductor_bot.cli.param_resolver import resolve_cli_config

        if self._bg_observer is None:
            msg = "Background observer not initialized"
            raise RuntimeError(msg)

        model_name, provider_name = self.resolve_runtime_target(self._config.model)
        if request.provider_override:
            provider_name = request.provider_override
            model_name = request.model_override or self.default_model_for_provider(
                request.provider_override
            )

        ns = self._named_sessions.create(chat_id, provider_name, model_name, prompt)
        exec_config = resolve_cli_config(self._config, self._codex_cache)
        sub = BackgroundSubmit(
            chat_id=chat_id,
            prompt=prompt,
            message_id=request.message_id,
            thread_id=request.thread_id,
            session_name=ns.name,
            provider_override=provider_name,
            model_override=model_name,
        )
        task_id = self._bg_observer.submit(sub, exec_config)
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

        if self._bg_observer is None:
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

        self._named_sessions.mark_running(chat_id, session_name, prompt)
        exec_config = resolve_cli_config(self._config, self._codex_cache)
        sub = BackgroundSubmit(
            chat_id=chat_id,
            prompt=prompt,
            message_id=message_id,
            thread_id=thread_id,
            session_name=session_name,
            resume_session_id=ns.session_id,
            provider_override=ns.provider,
            model_override=ns.model,
        )
        return self._bg_observer.submit(sub, exec_config)

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
        if candidate in self._known_model_ids:
            return True
        return bool(self._codex_cache and self._codex_cache.validate_model(candidate))

    def default_model_for_provider(self, provider: str) -> str:
        """Return the default model ID for a provider, or empty string if unknown."""
        if provider == "claude":
            return self._config.model if self._config.provider == "claude" else "sonnet"
        if provider == "codex":
            if self._codex_cache:
                for m in self._codex_cache.models:
                    if m.is_default:
                        return m.id
            return ""
        if provider == "gemini":
            return ""
        return ""

    def resolve_session_directive(self, key: str) -> tuple[str, str] | None:
        """Resolve a ``@key`` directive to ``(provider, model)`` or ``None``.

        Handles three cases:
        - provider name (``@codex``) → (provider, default_model)
        - known model   (``@opus``)  → (inferred_provider, model)
        - unknown                     → None
        """
        if key in ("claude", "codex", "gemini"):
            return key, self.default_model_for_provider(key)
        if self.is_known_model(key):
            provider = self._models.provider_for(key)
            return provider, key
        return None

    def get_named_session(self, chat_id: int, name: str) -> NamedSession | None:
        """Look up a named session."""
        return self._named_sessions.get(chat_id, name)

    def list_named_sessions(self, chat_id: int) -> list[NamedSession]:
        """List active named sessions for a chat."""
        return self._named_sessions.list_active(chat_id)

    def pop_recovered_named_sessions(self, chat_id: int | None = None) -> list[NamedSession]:
        """Pop sessions that were running when the previous process stopped."""
        return self._named_sessions.pop_recovered_running(chat_id)

    def active_background_tasks(self, chat_id: int | None = None) -> list[BackgroundTask]:
        """Return active background tasks, optionally filtered by chat_id."""
        if self._bg_observer is None:
            return []
        return self._bg_observer.active_tasks(chat_id)

    @property
    def active_provider_name(self) -> str:
        """Human-readable name for the active CLI provider."""
        _model, provider = self.resolve_runtime_target(self._config.model)
        if provider == "claude":
            return "Claude Code"
        if provider == "gemini":
            return "Gemini"
        return "Codex"

    def is_chat_busy(self, chat_id: int) -> bool:
        """Check if a chat has active CLI processes."""
        return self._process_registry.has_active(chat_id)

    async def _ensure_docker(self) -> None:
        """Health-check Docker before CLI calls; auto-recover or fall back."""
        if not self._docker:
            return
        container = await self._docker.ensure_running()
        if container:
            self._cli_service.update_docker_container(container)
        elif self._cli_service._config.docker_container:
            logger.warning("Docker recovery failed, falling back to host execution")
            self._cli_service.update_docker_container("")

    async def _start_api_server(
        self,
        config: AgentConfig,
        paths: DuctorPaths,
    ) -> None:
        """Initialize and start the direct WebSocket API server."""
        try:
            from ductor_bot.api.server import ApiServer
        except ImportError:
            logger.warning(
                "API server enabled but PyNaCl is not installed. "
                "Install with: pip install ductor[api]"
            )
            return

        if not config.api.token:
            from ductor_bot.config import update_config_file_async

            token = secrets.token_urlsafe(32)
            config.api.token = token
            await update_config_file_async(
                paths.config_path,
                api={**config.api.model_dump(), "token": token},
            )
            logger.info("Generated API auth token (persisted to config)")

        default_chat_id = config.api.chat_id or (
            config.allowed_user_ids[0] if config.allowed_user_ids else 1
        )
        server = ApiServer(config.api, default_chat_id=default_chat_id)
        server.set_message_handler(self.handle_message_streaming)
        server.set_abort_handler(self.abort)
        server.set_file_context(
            allowed_roots=resolve_allowed_roots(config.file_access, paths.workspace),
            upload_dir=paths.api_files_dir,
            workspace=paths.workspace,
        )
        server.set_provider_info(self._build_provider_info())
        server.set_active_state_getter(lambda: self.resolve_runtime_target(self._config.model))

        try:
            await server.start()
        except OSError:
            logger.exception(
                "Failed to start API server on %s:%d",
                config.api.host,
                config.api.port,
            )
            return

        self._api_stop = server.stop

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
                )
            )

        if "model" in hot:
            self._refresh_known_model_ids()

        logger.info("Hot-reload applied to orchestrator services")

    async def _stop_observers(self) -> None:
        """Stop all background observers and caches."""
        if self._config_reloader:
            await self._config_reloader.stop()
        if self._bg_observer:
            await self._bg_observer.shutdown()
        await self._heartbeat.stop()
        if self._webhook_observer:
            await self._webhook_observer.stop()
        if self._cron_observer:
            await self._cron_observer.stop()
        await self._cleanup_observer.stop()
        if self._codex_cache_observer:
            await self._codex_cache_observer.stop()
            self._codex_cache_observer = None
        if self._gemini_cache_observer:
            await self._gemini_cache_observer.stop()
            self._gemini_cache_observer = None

    # -- Inter-agent communication ------------------------------------------

    def _interagent_chat_id(self) -> int:
        """Return the real Telegram chat_id for inter-agent sessions."""
        if not self._config.allowed_user_ids:
            logger.warning("No allowed_user_ids configured — inter-agent sessions use chat_id=0")
            return 0
        return self._config.allowed_user_ids[0]

    def _get_or_create_interagent_session(
        self,
        sender: str,
        *,
        new_session: bool = False,
    ) -> tuple[NamedSession, bool, str]:
        """Get or create a Named Session for an inter-agent conversation.

        Uses a deterministic name ``ia-{sender}`` so follow-up messages from
        the same sender automatically resume the same session.

        If *new_session* is True, any existing session for this sender is
        ended first so a fresh one is created.

        If the active provider/model has changed since the session was created,
        the old session is ended automatically (the CLI session ID is not
        portable across providers) and a provider-switch notice is returned.

        Returns ``(session, is_new, provider_switch_notice)``.
        """
        chat_id = self._interagent_chat_id()
        session_name = f"ia-{sender}"
        provider_switch_notice = ""

        if new_session and self._named_sessions.end_session(chat_id, session_name):
            logger.info("Inter-agent session reset: %s (sender=%s)", session_name, sender)

        model_name, provider_name = self.resolve_runtime_target(self._config.model)

        ns = self._named_sessions.get(chat_id, session_name)
        if ns is not None and ns.status != "ended":
            # Detect provider/model mismatch → session ID is not portable
            if ns.provider != provider_name:
                old_provider = ns.provider
                self._named_sessions.end_session(chat_id, session_name)
                logger.info(
                    "Inter-agent session %s reset: provider changed %s -> %s",
                    session_name,
                    old_provider,
                    provider_name,
                )
                provider_switch_notice = (
                    f"Agent `{self._cli_service._config.agent_name}` switched "
                    f"provider from `{old_provider}` to `{provider_name}`.\n"
                    f"The previous inter-agent session `{session_name}` is no longer "
                    f"resumable and has been ended.\n"
                    f"A new session `{session_name}` was started with `{provider_name}`."
                )
            else:
                return ns, False, ""

        ns = NamedSession(
            name=session_name,
            chat_id=chat_id,
            provider=provider_name,
            model=model_name,
            session_id="",
            prompt_preview=f"Inter-agent session with {sender}",
            status="running",
            created_at=time.time(),
        )
        self._named_sessions.add(ns)
        logger.info("Inter-agent named session created: %s (sender=%s)", session_name, sender)
        return ns, True, provider_switch_notice

    async def handle_interagent_message(
        self,
        sender: str,
        message: str,
        *,
        new_session: bool = False,
    ) -> tuple[str, str, str]:
        """Process a message from another agent via the InterAgentBus.

        Uses a Named Session per sender so that context is preserved across
        multiple inter-agent interactions.  The session can also be resumed
        manually from Telegram via ``@ia-{sender} <message>``.

        Returns ``(result_text, session_name, provider_switch_notice)``.
        The *provider_switch_notice* is non-empty when a provider change
        caused an automatic session reset — callers should notify the user.
        """
        from ductor_bot.cli.types import AgentRequest

        own_name = self._cli_service._config.agent_name
        chat_id = self._interagent_chat_id()
        ns, _is_new, provider_switch_notice = self._get_or_create_interagent_session(
            sender,
            new_session=new_session,
        )

        prompt = (
            f"[INTER-AGENT MESSAGE from '{sender}' to '{own_name}']\n"
            f"{message}\n"
            f"[END INTER-AGENT MESSAGE]\n\n"
            f"You are agent '{own_name}'. Respond to this inter-agent request "
            f"from '{sender}'. Be direct and concise."
        )

        ns.status = "running"
        request = AgentRequest(
            prompt=prompt,
            chat_id=chat_id,
            process_label=f"interagent:{sender}",
            resume_session=ns.session_id or None,
            timeout_seconds=self._config.cli_timeout,
        )

        try:
            response = await self._cli_service.execute(request)
        except Exception:
            ns.status = "idle"
            logger.exception("Inter-agent message handling failed (from=%s)", sender)
            return (
                f"Error processing inter-agent message from '{sender}'",
                ns.name,
                provider_switch_notice,
            )
        else:
            if response and response.session_id:
                self._named_sessions.update_after_response(
                    chat_id, ns.name, response.session_id, status="idle"
                )
            else:
                ns.status = "idle"
            return (response.result if response else ""), ns.name, provider_switch_notice

    async def handle_async_interagent_result(
        self,
        result: AsyncInterAgentResult,
        *,
        chat_id: int = 0,
    ) -> str:
        """Inject an async inter-agent result into the current active session.

        Called when another agent completes an async request we sent.
        Resumes the *current* active session (not the one that was active when
        the task was dispatched) so the agent has full conversation context.

        The prompt is self-contained: it includes both the original task
        description and the sub-agent's response, so the agent can process
        the result even if the session changed (``/new``, provider switch).

        Caller must hold the per-chat lock to prevent concurrent session access.
        """
        from ductor_bot.cli.types import AgentRequest
        from ductor_bot.orchestrator.flows import _update_session

        own_name = self._cli_service._config.agent_name
        recipient = result.recipient
        task_id = result.task_id

        session_hint = (
            f"\nThe recipient processed this in session `{result.session_name}`. "
            f"The user can continue this session in the recipient's Telegram chat "
            f"via `@{result.session_name} <message>`."
            if result.session_name
            else ""
        )

        task_context = (
            f"\n\nOriginal task you sent to '{recipient}':\n{result.original_message}"
            if result.original_message
            else ""
        )

        prompt = (
            f"[ASYNC INTER-AGENT RESPONSE from '{recipient}' (task {task_id})]\n"
            f"{result.result_text}\n"
            f"[END ASYNC INTER-AGENT RESPONSE]{session_hint}{task_context}\n\n"
            f"You are agent '{own_name}'. Process this response from agent "
            f"'{recipient}' and communicate the relevant results to the user "
            f"in your Telegram chat."
        )

        # Always resume the CURRENT active session (not the original one).
        active = await self._sessions.get_active(chat_id)
        resume_id = active.session_id if active else None

        logger.debug(
            "Injecting async result into main session: task=%s from=%s "
            "resume_session=%s original_msg_len=%d",
            task_id,
            recipient,
            resume_id[:8] if resume_id else "<new>",
            len(result.original_message),
        )

        request = AgentRequest(
            prompt=prompt,
            chat_id=chat_id,
            process_label=f"interagent-async:{recipient}",
            resume_session=resume_id,
            timeout_seconds=self._config.cli_timeout,
        )

        try:
            response = await self._cli_service.execute(request)
        except Exception:
            logger.exception(
                "Async inter-agent result handling failed (from=%s)",
                recipient,
            )
            return f"Error processing async result from '{recipient}'"

        if active and response:
            await _update_session(self, active, response)

        return response.result if response else ""

    async def shutdown(self) -> None:
        """Cleanup on bot shutdown."""
        killed = await self._process_registry.kill_all_active()
        if killed:
            logger.info("Shutdown terminated %d active CLI process(es)", killed)
        if self._api_stop is not None:
            await self._api_stop()
        for task in (self._rule_sync_task, self._skill_sync_task):
            if task and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        await asyncio.to_thread(cleanup_ductor_links, self._paths)
        await self._stop_observers()
        if self._docker:
            await self._docker.teardown()
        logger.info("Orchestrator shutdown")
