"""Core orchestrator: routes messages through command and conversation flows."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
from collections.abc import Awaitable, Callable

from ductor_bot.cleanup import CleanupObserver
from ductor_bot.cli.process_registry import ProcessRegistry
from ductor_bot.cli.service import CLIService, CLIServiceConfig
from ductor_bot.config import _CLAUDE_MODELS, AgentConfig, ModelRegistry
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
from ductor_bot.heartbeat import HeartbeatObserver
from ductor_bot.infra.docker import DockerManager
from ductor_bot.orchestrator.commands import (
    cmd_cron,
    cmd_diagnose,
    cmd_memory,
    cmd_model,
    cmd_reset,
    cmd_status,
    cmd_stop,
    cmd_upgrade,
)
from ductor_bot.orchestrator.directives import parse_directives
from ductor_bot.orchestrator.flows import (
    heartbeat_flow,
    normal,
    normal_streaming,
)
from ductor_bot.orchestrator.hooks import MAINMEMORY_REMINDER, MessageHookRegistry
from ductor_bot.orchestrator.registry import CommandRegistry, OrchestratorResult
from ductor_bot.security import detect_suspicious_patterns
from ductor_bot.session import SessionManager
from ductor_bot.webhook.manager import WebhookManager
from ductor_bot.webhook.models import WebhookResult
from ductor_bot.webhook.observer import WebhookObserver
from ductor_bot.workspace.init import (
    init_workspace,
    inject_runtime_environment,
    watch_rule_files,
)
from ductor_bot.workspace.paths import DuctorPaths, resolve_paths

logger = logging.getLogger(__name__)


class Orchestrator:
    """Routes messages through command dispatch and conversation flows."""

    def __init__(
        self,
        config: AgentConfig,
        paths: DuctorPaths,
        *,
        docker_container: str = "",
    ) -> None:
        self._config = config
        self._paths: DuctorPaths = paths
        self._docker: DockerManager | None = None
        self._models = ModelRegistry()
        self._known_model_ids: frozenset[str] = _CLAUDE_MODELS
        self._sessions = SessionManager(paths.sessions_path, config)
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
                docker_container=docker_container,
            ),
            models=self._models,
            available_providers=frozenset(),
            process_registry=self._process_registry,
        )
        self._cron_manager = CronManager(jobs_path=paths.cron_jobs_path)
        self._cron_observer = CronObserver(
            paths,
            self._cron_manager,
            config=config,
            models=self._models,
        )
        self._heartbeat = HeartbeatObserver(config)
        self._heartbeat.set_heartbeat_handler(self.handle_heartbeat)
        self._heartbeat.set_busy_check(self._process_registry.has_active)
        stale_max = config.cli_timeout * 2
        self._heartbeat.set_stale_cleanup(lambda: self._process_registry.kill_stale(stale_max))
        self._webhook_manager = WebhookManager(hooks_path=paths.webhooks_path)
        self._webhook_observer = WebhookObserver(
            paths,
            self._webhook_manager,
            config=config,
            models=self._models,
        )
        self._cleanup_observer = CleanupObserver(config, paths)
        self._rule_sync_task: asyncio.Task[None] | None = None
        self._hook_registry = MessageHookRegistry()
        self._hook_registry.register(MAINMEMORY_REMINDER)
        self._command_registry = CommandRegistry()
        self._register_commands()

    @property
    def paths(self) -> DuctorPaths:
        """Public access to resolved workspace paths."""
        return self._paths

    @classmethod
    async def create(cls, config: AgentConfig) -> Orchestrator:
        """Async factory: initialize workspace, build Orchestrator."""
        paths = resolve_paths(ductor_home=config.ductor_home)
        await asyncio.to_thread(init_workspace, paths)

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

        await asyncio.to_thread(
            inject_runtime_environment, paths, docker_container=docker_container
        )

        orch = cls(config, paths, docker_container=docker_container)
        orch._docker = docker_mgr

        from ductor_bot.cli.auth import AuthStatus, check_all_auth

        auth_results = await asyncio.to_thread(check_all_auth)
        for provider, result in auth_results.items():
            if result.status == AuthStatus.AUTHENTICATED:
                logger.info("Provider [%s]: authenticated", provider)
            elif result.status == AuthStatus.INSTALLED:
                logger.warning("Provider [%s]: installed but NOT authenticated", provider)
            else:
                logger.info("Provider [%s]: not found", provider)

        orch._available_providers = frozenset(
            name for name, res in auth_results.items() if res.is_authenticated
        )
        orch._cli_service.update_available_providers(orch._available_providers)

        if not orch._available_providers:
            logger.error("No authenticated providers found! CLI calls will fail.")
        else:
            logger.info("Available providers: %s", ", ".join(sorted(orch._available_providers)))

        await orch._cron_observer.start()
        await orch._heartbeat.start()
        await orch._webhook_observer.start()
        await orch._cleanup_observer.start()
        orch._rule_sync_task = asyncio.create_task(watch_rule_files(paths.workspace))
        logger.info("Rule file watcher started (CLAUDE.md <-> AGENTS.md)")

        return orch

    async def handle_message(self, chat_id: int, text: str) -> OrchestratorResult:
        """Main entry point: route message to appropriate handler."""
        return await self._handle_message_impl(chat_id, text)

    async def handle_message_streaming(
        self,
        chat_id: int,
        text: str,
        *,
        on_text_delta: Callable[[str], Awaitable[None]] | None = None,
        on_tool_activity: Callable[[str], Awaitable[None]] | None = None,
        on_system_status: Callable[[str | None], Awaitable[None]] | None = None,
    ) -> OrchestratorResult:
        """Main entry point with streaming support."""
        return await self._handle_message_impl(
            chat_id,
            text,
            streaming=True,
            on_text_delta=on_text_delta,
            on_tool_activity=on_tool_activity,
            on_system_status=on_system_status,
        )

    async def _handle_message_impl(  # noqa: PLR0913
        self,
        chat_id: int,
        text: str,
        *,
        streaming: bool = False,
        on_text_delta: Callable[[str], Awaitable[None]] | None = None,
        on_tool_activity: Callable[[str], Awaitable[None]] | None = None,
        on_system_status: Callable[[str | None], Awaitable[None]] | None = None,
    ) -> OrchestratorResult:
        self._process_registry.clear_abort(chat_id)
        cmd = text.strip().lower()
        logger.info("Message received text=%s", cmd[:80])

        patterns = detect_suspicious_patterns(text)
        if patterns:
            logger.warning("Suspicious input patterns: %s", ", ".join(patterns))

        try:
            return await self._route_message(
                chat_id,
                text,
                cmd,
                streaming=streaming,
                on_text_delta=on_text_delta,
                on_tool_activity=on_tool_activity,
                on_system_status=on_system_status,
            )
        except asyncio.CancelledError:
            raise
        except (CLIError, StreamError, SessionError, CronError, WebhookError, WorkspaceError):
            logger.exception("Domain error in handle_message")
            return OrchestratorResult(text="An internal error occurred. Please try again.")
        except (OSError, RuntimeError, ValueError, TypeError, KeyError):
            logger.exception("Unexpected error in handle_message")
            return OrchestratorResult(text="An internal error occurred. Please try again.")

    async def _route_message(  # noqa: PLR0913
        self,
        chat_id: int,
        text: str,
        cmd: str,
        *,
        streaming: bool,
        on_text_delta: Callable[[str], Awaitable[None]] | None,
        on_tool_activity: Callable[[str], Awaitable[None]] | None,
        on_system_status: Callable[[str | None], Awaitable[None]] | None = None,
    ) -> OrchestratorResult:
        result = await self._command_registry.dispatch(cmd, self, chat_id, text)
        if result is not None:
            return result

        await self._ensure_docker()

        directives = parse_directives(text, self._known_model_ids)

        if directives.is_directive_only and directives.has_model:
            return OrchestratorResult(
                text=f"Next message will use: {directives.model}\n"
                f"(Send a message with @{directives.model} <text> to use it.)",
            )

        prompt_text = directives.cleaned or text

        if streaming:
            return await normal_streaming(
                self,
                chat_id,
                prompt_text,
                model_override=directives.model,
                on_text_delta=on_text_delta,
                on_tool_activity=on_tool_activity,
                on_system_status=on_system_status,
            )

        return await normal(
            self,
            chat_id,
            prompt_text,
            model_override=directives.model,
        )

    def _register_commands(self) -> None:
        reg = self._command_registry
        reg.register_async("/new", cmd_reset)
        reg.register_async("/stop", cmd_stop)
        reg.register_async("/status", cmd_status)
        reg.register_async("/model", cmd_model)
        reg.register_async("/model ", cmd_model)
        reg.register_async("/memory", cmd_memory)
        reg.register_async("/cron", cmd_cron)
        reg.register_async("/diagnose", cmd_diagnose)
        reg.register_async("/upgrade", cmd_upgrade)

    async def reset_session(self, chat_id: int) -> None:
        """Reset the session for a given chat."""
        await self._sessions.reset_session(chat_id)
        logger.info("Session reset")

    async def abort(self, chat_id: int) -> int:
        """Kill all active CLI processes for chat_id."""
        return await self._process_registry.kill_all(chat_id)

    def set_cron_result_handler(
        self,
        handler: Callable[[str, str, str], Awaitable[None]],
    ) -> None:
        """Forward cron job results to an external handler (e.g. Telegram)."""
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
        self._webhook_observer.set_result_handler(handler)

    def set_webhook_wake_handler(
        self,
        handler: Callable[[int, str], Awaitable[str | None]],
    ) -> None:
        """Set the webhook wake handler (provided by the bot layer)."""
        self._webhook_observer.set_wake_handler(handler)

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

    async def shutdown(self) -> None:
        """Cleanup on bot shutdown."""
        if self._rule_sync_task and not self._rule_sync_task.done():
            self._rule_sync_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._rule_sync_task
        await self._heartbeat.stop()
        await self._webhook_observer.stop()
        await self._cron_observer.stop()
        await self._cleanup_observer.stop()
        if self._docker:
            await self._docker.teardown()
        logger.info("Orchestrator shutdown")
