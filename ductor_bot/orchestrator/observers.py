"""Observer lifecycle management for the Orchestrator.

Consolidates creation, start, and teardown of all background observers
(cron, webhook, heartbeat, cleanup, model caches, config reloader,
rule/skill sync watchers) into a single manager.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING

from ductor_bot.background import BackgroundObserver, BackgroundResult

if TYPE_CHECKING:
    from ductor_bot.bus.bus import MessageBus
from ductor_bot.cleanup import CleanupObserver
from ductor_bot.cli.antigravity_cache_observer import AntigravityCacheObserver
from ductor_bot.cli.codex_cache import CodexModelCache
from ductor_bot.cli.codex_cache_observer import CodexCacheObserver
from ductor_bot.cli.gemini_cache_observer import GeminiCacheObserver
from ductor_bot.cli.grok_cache_observer import GrokCacheObserver
from ductor_bot.cli.service import CLIService
from ductor_bot.config import (
    AgentConfig,
    get_antigravity_models,
    get_gemini_models,
    get_grok_models,
)
from ductor_bot.config_reload import ConfigReloader
from ductor_bot.cron.manager import CronManager
from ductor_bot.cron.observer import CronObserver
from ductor_bot.heartbeat import HeartbeatObserver
from ductor_bot.webhook.manager import WebhookManager
from ductor_bot.webhook.models import WebhookResult
from ductor_bot.webhook.observer import WebhookObserver
from ductor_bot.workspace.init import watch_rule_files
from ductor_bot.workspace.paths import DuctorPaths
from ductor_bot.workspace.skill_sync import watch_skill_sync

logger = logging.getLogger(__name__)


class ObserverManager:
    """Owns all background observers and manages their lifecycle."""

    def __init__(self, config: AgentConfig, paths: DuctorPaths) -> None:
        self._config = config
        self._paths = paths
        self.heartbeat = HeartbeatObserver(config)
        self.cleanup = CleanupObserver(config, paths)

        self.cron: CronObserver | None = None
        self.webhook: WebhookObserver | None = None
        self.background: BackgroundObserver | None = None
        self.codex_cache: CodexModelCache | None = None
        self.codex_cache_obs: CodexCacheObserver | None = None
        self.gemini_cache_obs: GeminiCacheObserver | None = None
        self.antigravity_cache_obs: AntigravityCacheObserver | None = None
        self.grok_cache_obs: GrokCacheObserver | None = None

        self._config_reloader: ConfigReloader | None = None
        self._rule_sync_task: asyncio.Task[None] | None = None
        self._skill_sync_task: asyncio.Task[None] | None = None

    # -- Model cache initialization -------------------------------------------

    async def init_model_caches(
        self,
        *,
        installed_providers: frozenset[str],
        on_gemini_refresh: Callable[[tuple[str, ...]], None],
        on_antigravity_refresh: Callable[[tuple[str, ...]], None],
        on_grok_refresh: Callable[[tuple[str, ...]], None],
    ) -> CodexModelCache:
        """Start Gemini, Antigravity, Grok, and Codex cache observers, return Codex cache.

        *installed_providers* comes from the startup auth detection, which is
        fallback-aware (e.g. finds a Gemini CLI installed under NVM that plain
        PATH lookup would miss). Observers for providers not in the set are
        never created.
        """
        if "gemini" in installed_providers:
            gemini_cache_path = self._paths.config_path.parent / "gemini_models.json"
            gemini_observer = GeminiCacheObserver(gemini_cache_path, on_refresh=on_gemini_refresh)
            await gemini_observer.start()
            self.gemini_cache_obs = gemini_observer

            if not get_gemini_models():
                logger.warning("Gemini cache is empty after startup")
        else:
            logger.debug("Gemini CLI not found; cache observer disabled")

        if "antigravity" in installed_providers:
            antigravity_cache_path = self._paths.config_path.parent / "antigravity_models.json"
            antigravity_observer = AntigravityCacheObserver(
                antigravity_cache_path, on_refresh=on_antigravity_refresh
            )
            await antigravity_observer.start()
            self.antigravity_cache_obs = antigravity_observer

            if not get_antigravity_models():
                logger.warning("Antigravity cache is empty after startup")
        else:
            logger.debug("Antigravity CLI not found; cache observer disabled")

        if "grok" in installed_providers:
            grok_cache_path = self._paths.config_path.parent / "grok_models.json"
            grok_observer = GrokCacheObserver(grok_cache_path, on_refresh=on_grok_refresh)
            await grok_observer.start()
            self.grok_cache_obs = grok_observer

            if not get_grok_models():
                logger.warning("Grok cache is empty after startup")
        else:
            logger.debug("Grok CLI not found; cache observer disabled")

        codex_cache: CodexModelCache | None = None
        if "codex" in installed_providers:
            codex_cache_path = self._paths.config_path.parent / "codex_models.json"
            codex_observer = CodexCacheObserver(codex_cache_path)
            await codex_observer.start()
            self.codex_cache_obs = codex_observer
            codex_cache = codex_observer.get_cache()

            if not codex_cache or not codex_cache.models:
                logger.warning("Codex cache is empty after startup")
        else:
            logger.debug("Codex CLI not found; cache observer disabled")

        return codex_cache or CodexModelCache("", [])

    # -- Task observer initialization -----------------------------------------

    def init_task_observers(
        self,
        *,
        cron_manager: CronManager,
        webhook_manager: WebhookManager,
        cli_service: CLIService,
        codex_cache: CodexModelCache,
    ) -> None:
        """Create Background, Cron, and Webhook observers (after caches are ready)."""
        config, paths = self._config, self._paths
        self.codex_cache = codex_cache
        self.background = BackgroundObserver(
            paths,
            timeout_seconds=config.timeouts.background,
            cli_service=cli_service,
            config=config,
        )
        self.cron = CronObserver(paths, cron_manager, config=config, codex_cache=codex_cache)
        self.webhook = WebhookObserver(
            paths, webhook_manager, config=config, codex_cache=codex_cache
        )

    # -- Start / stop ---------------------------------------------------------

    async def start_all(self, *, docker_container: str = "") -> None:
        """Start all observers and background watchers."""
        if self.cron:
            await self.cron.start()
        await self.heartbeat.start()
        if self.webhook:
            await self.webhook.start()
        await self.cleanup.start()

        self._rule_sync_task = asyncio.create_task(watch_rule_files(self._paths.workspace))
        logger.info("Rule file watcher started (CLAUDE.md <-> AGENTS.md <-> GEMINI.md)")

        self._skill_sync_task = asyncio.create_task(
            watch_skill_sync(self._paths, docker_active=bool(docker_container))
        )
        logger.info("Skill sync watcher started")

    async def start_config_reloader(
        self,
        *,
        on_hot_reload: Callable[[AgentConfig, dict[str, object]], None],
        on_restart_needed: Callable[[list[str]], None],
    ) -> None:
        """Start the config file watcher."""
        self._config_reloader = ConfigReloader(
            self._paths.config_path,
            self._config,
            on_hot_reload=on_hot_reload,
            on_restart_needed=on_restart_needed,
        )
        await self._config_reloader.start()

    async def stop_all(self) -> None:
        """Stop all background observers and caches."""
        if self._config_reloader:
            await self._config_reloader.stop()
        if self.background:
            await self.background.shutdown()
        await self.heartbeat.stop()
        if self.webhook:
            await self.webhook.stop()
        if self.cron:
            await self.cron.stop()
        await self.cleanup.stop()
        cache_observer_attrs = (
            "codex_cache_obs",
            "gemini_cache_obs",
            "antigravity_cache_obs",
            "grok_cache_obs",
        )
        for attr in cache_observer_attrs:
            observer = getattr(self, attr)
            if observer:
                await observer.stop()
                setattr(self, attr, None)
        for task in (self._rule_sync_task, self._skill_sync_task):
            if task and not task.done():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    # -- Bus wiring (single entry point) --------------------------------------

    def wire_to_bus(
        self,
        bus: MessageBus,
        *,
        wake_handler: Callable[[int, str], Awaitable[str | None]] | None = None,
    ) -> None:
        """Wire all observer result callbacks to the message bus.

        Replaces the five individual setter methods with a single call.
        """
        from ductor_bot.bus.adapters import (
            from_background_result,
            from_cron_result,
            from_heartbeat,
            from_webhook_cron_result,
        )

        if self.cron:

            async def _on_cron(  # noqa: PLR0913
                title: str,
                result: str,
                status: str,
                chat_id: int = 0,
                topic_id: int | None = None,
                transport: str = "tg",
            ) -> tuple[bool, str]:
                env = from_cron_result(
                    title,
                    result,
                    status,
                    chat_id=chat_id,
                    topic_id=topic_id,
                    transport=transport,
                )
                await bus.submit(env)
                # #160: report the delivery acknowledgement back to the cron
                # observer so a swallowed send failure is persisted, not lost.
                return env.delivered, env.delivery_error

            self.cron.set_result_handler(_on_cron)

        async def _on_heartbeat(
            chat_id: int,
            text: str,
            topic_id: int | None = None,
            transport: str = "tg",
        ) -> None:
            await bus.submit(from_heartbeat(chat_id, text, topic_id, transport=transport))

        self.heartbeat.set_result_handler(_on_heartbeat)

        if self.background:

            async def _on_bg(result: BackgroundResult) -> None:
                await bus.submit(from_background_result(result))

            self.background.set_result_handler(_on_bg)

        if self.webhook:

            async def _on_webhook(result: WebhookResult) -> None:
                if result.mode != "wake":
                    await bus.submit(from_webhook_cron_result(result))

            self.webhook.set_result_handler(_on_webhook)
            if wake_handler:
                self.webhook.set_wake_handler(wake_handler)
