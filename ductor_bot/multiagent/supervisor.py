"""AgentSupervisor: manages main agent + dynamic sub-agents in a single process."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING

from ductor_bot.cli.process_registry import ProcessRegistry
from ductor_bot.config import AgentConfig, update_config_file_async
from ductor_bot.infra.file_watcher import FileWatcher
from ductor_bot.infra.restart import EXIT_RESTART
from ductor_bot.multiagent.health import AgentHealth
from ductor_bot.multiagent.models import SubAgentConfig, merge_sub_agent_config
from ductor_bot.multiagent.registry import AgentRegistry
from ductor_bot.multiagent.stack import AgentStack
from ductor_bot.workspace.paths import resolve_paths

if TYPE_CHECKING:
    from ductor_bot.multiagent.bus import InterAgentBus
    from ductor_bot.multiagent.internal_api import InternalAgentAPI
    from ductor_bot.multiagent.shared_knowledge import SharedKnowledgeSync
    from ductor_bot.tasks.hub import TaskHub

logger = logging.getLogger(__name__)

_MAX_RESTART_RETRIES = 5
_RESTART_BACKOFF_BASE = 5  # seconds, doubles each retry
_MAX_BACKOFF = 60  # seconds; cap for the main agent's unbounded transient retries
_MAX_BACKOFF_SHIFT = 6  # clamp the doubling exponent so unbounded retries stay a small int


def _is_transient(exc: Exception) -> bool:
    """Classify an exception as a transient (retryable) network failure.

    Name-based for transport error types: the supervisor is transport-agnostic
    and must not import aiogram (or any transport SDK). Matches the transient
    branch of aiogram's error hierarchy (``TelegramNetworkError``,
    ``TelegramServerError``, both subclasses of ``TelegramAPIError``) plus the
    stdlib connection/timeout errors. Deliberately excludes fatal errors such as
    ``TelegramUnauthorizedError`` (bad token) and ``TelegramConflictError``
    (double polling), which must terminate the supervisor.
    """
    if isinstance(exc, (ConnectionError, TimeoutError)):
        return True
    return type(exc).__name__ in {"TelegramNetworkError", "TelegramServerError"}


def _capped_backoff(retry_count: int) -> int:
    """Exponential backoff in seconds, capped at ``_MAX_BACKOFF`` (5, 10, 20, 40, 60, 60, …).

    ``retry_count`` is 1-based and unbounded for the main agent, which retries
    transient network errors indefinitely; the exponent is clamped so the
    intermediate value never grows large.
    """
    shift = min(retry_count - 1, _MAX_BACKOFF_SHIFT)
    return min(_RESTART_BACKOFF_BASE * (1 << shift), _MAX_BACKOFF)


def _config_changed(new: AgentConfig, old: AgentConfig) -> bool:
    """Detect meaningful config changes that require agent restart."""
    if new.transport != old.transport:
        return True
    if new.transports != old.transports:
        return True
    return _TRANSPORT_IDENTITY_CHANGED.get(new.transport, _default_identity_check)(new, old)


def _telegram_identity_check(new: AgentConfig, old: AgentConfig) -> bool:
    return new.telegram_token != old.telegram_token


def _matrix_identity_check(new: AgentConfig, old: AgentConfig) -> bool:
    return (
        new.matrix.homeserver != old.matrix.homeserver or new.matrix.user_id != old.matrix.user_id
    )


def _default_identity_check(_new: AgentConfig, _old: AgentConfig) -> bool:
    return False


_IdentityCheck = Callable[[AgentConfig, AgentConfig], bool]

_TRANSPORT_IDENTITY_CHANGED: dict[str, _IdentityCheck] = {
    "telegram": _telegram_identity_check,
    "matrix": _matrix_identity_check,
}


class AgentSupervisor:
    """Manages the main agent and dynamically created sub-agents.

    Watches ``agents.json`` via FileWatcher and starts/stops sub-agents
    as entries are added or removed. Each agent runs as a supervised
    asyncio task with automatic crash recovery.
    """

    def __init__(self, main_config: AgentConfig) -> None:
        self._main_config = main_config
        self._main_paths = resolve_paths(ductor_home=main_config.ductor_home)
        self._agents_path = self._main_paths.ductor_home / "agents.json"
        self._registry = AgentRegistry(self._agents_path)
        self._stacks: dict[str, AgentStack] = {}
        self._tasks: dict[str, asyncio.Task[int]] = {}
        self._health: dict[str, AgentHealth] = {}
        self._watcher = FileWatcher(self._agents_path, self._on_agents_changed)
        self._running = False
        self._main_done: asyncio.Event = asyncio.Event()
        self._main_ready: asyncio.Event = asyncio.Event()
        self._agents_lock = asyncio.Lock()

        # Bus, internal API, shared knowledge, task hub — created lazily in start()
        self._bus: InterAgentBus | None = None
        self._internal_api: InternalAgentAPI | None = None
        self._shared_knowledge: SharedKnowledgeSync | None = None
        self._task_hub: TaskHub | None = None

    @property
    def stacks(self) -> dict[str, AgentStack]:
        return self._stacks

    @property
    def health(self) -> dict[str, AgentHealth]:
        return self._health

    @property
    def bus(self) -> InterAgentBus | None:
        return self._bus

    async def start(self) -> int:
        """Start main agent + all sub-agents. Blocks until main agent exits."""
        self._running = True

        # Initialize inter-agent bus
        from ductor_bot.multiagent.bus import InterAgentBus
        from ductor_bot.multiagent.internal_api import InternalAgentAPI

        self._bus = InterAgentBus()
        self._internal_api = InternalAgentAPI(
            self._bus,
            port=self._main_config.interagent_port,
            docker_mode=self._main_config.docker.enabled,
        )
        self._internal_api.set_health_ref(self._health)
        started = await self._internal_api.start()
        if not started:
            msg = "Internal agent API failed to start"
            raise RuntimeError(msg)
        logger.info("InterAgentBus and internal API started")

        # Initialize task hub (background task delegation)
        if self._main_config.tasks.enabled:
            from ductor_bot.tasks.hub import TaskHub
            from ductor_bot.tasks.registry import TaskRegistry

            registry = TaskRegistry(
                registry_path=self._main_paths.tasks_registry_path,
                tasks_dir=self._main_paths.tasks_dir,
            )
            # #92: shared fallback ProcessRegistry. In practice every agent's
            # CLIService holds its OWN registry (see ``Orchestrator.__init__``),
            # so per-agent registries wired in ``_wire_task_hub`` take precedence.
            # The shared fallback is kept for test setups and single-agent deploys.
            self._task_process_registry = ProcessRegistry()
            self._task_hub = TaskHub(
                registry,
                self._main_paths,
                cli_service=None,  # Set per-agent in _post_startup
                config=self._main_config.tasks,
                append_system_prompt_files=self._main_config.append_system_prompt_files,
                process_registry=self._task_process_registry,
            )
            self._internal_api.set_task_hub(self._task_hub)
            logger.info(
                "TaskHub initialized (max_parallel=%d)", self._main_config.tasks.max_parallel
            )

        # 1. Start main agent
        main_stack = await AgentStack.create(
            "main",
            self._main_config,
            is_main=True,
        )
        self._stacks["main"] = main_stack
        self._health["main"] = AgentHealth(name="main")
        self._bus.register("main", main_stack)
        self._bus.set_async_result_handler("main", main_stack.bot.on_async_interagent_result)

        self._tasks["main"] = asyncio.create_task(
            self._supervised_run("main", main_stack),
            name="agent:main",
        )

        # 2. Wait for main agent startup (Docker, workspace, auth) before
        #    starting sub-agents.  This ensures Docker is set up exactly once
        #    by the main agent; sub-agents reuse the existing container.
        #    Timeout is extended when Docker extras are configured because the
        #    first image build can take several minutes.
        startup_timeout = 120
        if self._main_config.docker.enabled and self._main_config.docker.extras:
            from ductor_bot.infra.docker_extras import calculate_build_timeout, resolve_extras

            startup_timeout = max(
                startup_timeout,
                calculate_build_timeout(resolve_extras(self._main_config.docker.extras)),
            )
        try:
            await asyncio.wait_for(self._main_ready.wait(), timeout=startup_timeout)
        except TimeoutError:
            logger.warning(
                "Main agent startup timed out after %ds, starting sub-agents anyway",
                startup_timeout,
            )

        # 3. Load and start sub-agents from agents.json
        await self._sync_sub_agents()

        # 4. Start shared knowledge sync (SHAREDMEMORY.md → all agents)
        from ductor_bot.multiagent.shared_knowledge import SharedKnowledgeSync

        shared_path = self._main_paths.ductor_home / "SHAREDMEMORY.md"
        self._shared_knowledge = SharedKnowledgeSync(shared_path, self)
        await self._shared_knowledge.start()

        # 5. Start FileWatcher for agents.json
        await self._watcher.start()

        # 6. Wait for main agent to finish — it determines the exit code
        await self._main_done.wait()
        main_task = self._tasks.get("main")
        exit_code = 0
        if main_task and main_task.done():
            try:
                exit_code = main_task.result()
            except (asyncio.CancelledError, Exception):
                exit_code = 1

        return exit_code

    def _finish_agent(self, name: str, health: AgentHealth) -> None:
        """Mark an agent as stopped and signal main-done if it is the main agent."""
        health.mark_stopped()
        if name == "main":
            self._main_done.set()

    async def _handle_restart_exit(
        self, name: str, stack: AgentStack, health: AgentHealth
    ) -> tuple[AgentStack, bool]:
        """Handle EXIT_RESTART for an agent.

        Returns ``(stack, should_return)`` — when *should_return* is True the
        caller must ``return EXIT_RESTART``.
        """
        if name == "main":
            logger.info("Main agent requested full service restart")
            self._finish_agent(name, health)
            return stack, True

        # Sub-agent: in-process hot-reload (rebuild stack only)
        logger.info("Sub-agent '%s' requested restart (hot-reload)", name)
        health.mark_starting()
        await stack.shutdown()
        new_stack = await self._rebuild_stack(name, stack)
        return new_stack, False

    async def _handle_crash(
        self,
        name: str,
        stack: AgentStack,
        health: AgentHealth,
        retry_count: int,
        exc: Exception,
    ) -> tuple[AgentStack, int, bool]:
        """Handle a crash in ``_supervised_run``.

        Returns ``(stack, retry_count, should_return)`` — when *should_return*
        is True the caller must ``return 1``.

        The main agent terminates the supervisor on fatal errors, but retries
        transient network failures (see ``_is_transient``) indefinitely with
        capped backoff — an early-startup network blip must not take down the
        whole service. Sub-agents retry any crash up to ``_MAX_RESTART_RETRIES``
        times.
        """
        error_msg = f"{type(exc).__name__}: {exc}"
        health.mark_crashed(error_msg)

        if name == "main":
            if not _is_transient(exc):
                logger.exception(
                    "Main agent crashed (fatal), terminating supervisor: %s", error_msg
                )
                self._main_ready.set()  # unblock sub-agent startup if still waiting
                self._main_done.set()
                return stack, retry_count, True

            logger.warning(
                "Main agent crashed (transient, attempt %d): %s — retrying",
                retry_count,
                error_msg,
            )
            stack = await self._backoff_and_rebuild(
                name, stack, health, _capped_backoff(retry_count)
            )
            return stack, retry_count, False

        logger.exception(
            "Agent '%s' crashed (attempt %d/%d): %s",
            name,
            retry_count,
            _MAX_RESTART_RETRIES,
            error_msg,
        )

        if retry_count > _MAX_RESTART_RETRIES:
            logger.exception(
                "Agent '%s' exceeded max retries (%d), giving up",
                name,
                _MAX_RESTART_RETRIES,
            )
            await self._notify_main_agent(
                f"Sub-agent '{name}' stopped after {_MAX_RESTART_RETRIES} crashes: {error_msg}"
            )
            return stack, retry_count, True

        wait = _RESTART_BACKOFF_BASE * (2 ** (retry_count - 1))
        stack = await self._backoff_and_rebuild(name, stack, health, wait)
        return stack, retry_count, False

    async def _backoff_and_rebuild(
        self, name: str, stack: AgentStack, health: AgentHealth, wait: int
    ) -> AgentStack:
        """Sleep for *wait* seconds, then shut down and rebuild the agent stack.

        Shared by the sub-agent recovery path and the main-agent transient-retry
        path. Rebuild failures are logged and swallowed so the supervised loop
        retries again on its next iteration.
        """
        logger.info("Agent '%s' restarting in %ds", name, wait)
        await asyncio.sleep(wait)

        try:
            with contextlib.suppress(Exception):
                await stack.shutdown()
            stack = await self._rebuild_stack(name, stack)
            health.mark_starting()
        except Exception:
            logger.exception("Failed to rebuild agent '%s'", name)

        return stack

    async def _supervised_run(self, name: str, stack: AgentStack) -> int:
        """Run an agent with automatic crash recovery.

        On crash:
          - Sub-agents: retry with exponential backoff (5s, 10s, 20s, 40s, 80s);
            give up after ``_MAX_RESTART_RETRIES`` consecutive failures.
          - Main agent: fatal errors terminate the supervisor; transient network
            errors (see ``_is_transient``) retry indefinitely with capped backoff.
        On clean exit: return the exit code.
        On restart request (exit code 42):
          - Main agent: propagate EXIT_RESTART to trigger full service restart.
          - Sub-agents: rebuild stack in-process (hot-reload).
        """
        from ductor_bot.log_context import set_log_context

        set_log_context(agent_name=name)
        health = self._health[name]
        health.mark_starting()
        retry_count = 0

        while self._running:
            try:
                self._inject_supervisor_hook(stack)
                health.mark_running()
                logger.info("Agent '%s' running", name)
                exit_code = await stack.run()

                if exit_code == EXIT_RESTART:
                    stack, should_return = await self._handle_restart_exit(name, stack, health)
                    if should_return:
                        return EXIT_RESTART
                    retry_count = 0
                    continue

                # Clean exit
                logger.info("Agent '%s' exited cleanly (code=%d)", name, exit_code)
                self._finish_agent(name, health)

            except asyncio.CancelledError:
                logger.info("Agent '%s' cancelled", name)
                health.mark_stopped()
                raise

            except Exception as exc:
                retry_count += 1
                stack, retry_count, should_return = await self._handle_crash(
                    name,
                    stack,
                    health,
                    retry_count,
                    exc,
                )
                if should_return:
                    return 1
                continue

            else:
                return exit_code

        self._finish_agent(name, health)
        return 0

    def _inject_supervisor_hook(self, stack: AgentStack) -> None:
        """Register a dispatcher startup hook to inject supervisor reference.

        The orchestrator is created during TelegramBot._on_startup(). We register
        an additional startup handler that fires AFTER _on_startup and sets the
        supervisor reference + registers multi-agent commands on the main agent.

        For the main agent this also signals ``_main_ready`` so the supervisor
        knows Docker and workspace init are complete before starting sub-agents.
        """
        supervisor = self

        async def _post_startup() -> None:
            orch = stack.bot.orchestrator
            if orch is None:
                return
            orch.supervisor = supervisor
            if stack.is_main:
                orch.register_multiagent_commands()
                stack.bot.set_abort_all_callback(supervisor.abort_all_agents)
                supervisor._main_ready.set()

            # Wire task hub: set CLI service and register handlers
            if supervisor._task_hub is not None:
                supervisor._wire_task_hub(stack)

            logger.debug("Supervisor reference injected into agent '%s'", stack.name)

        # Startup handlers run in registration order;
        # TelegramBot registers _on_startup in __init__, so ours runs after.
        stack.bot.register_startup_hook(_post_startup)

    def _wire_task_hub(self, stack: AgentStack) -> None:
        """Wire task hub handlers for an agent stack.

        Called after the orchestrator is initialized (in _post_startup).
        Registers the agent's CLI service and per-agent result/question handlers.
        """
        hub = self._task_hub
        if hub is None:
            return

        orch = stack.bot.orchestrator
        if orch is None:
            return

        name = stack.name
        orch.set_task_hub(hub)

        # Register this agent's CLI service and workspace paths for task execution
        hub.set_cli_service(name, orch.cli_service)
        hub.set_agent_paths(name, stack.paths)
        # #92: register this agent's ProcessRegistry so TaskHub.cancel routes
        # kill_for_task to the registry where task:<id> subprocesses actually
        # live (each orchestrator owns its own — see Orchestrator.__init__).
        hub.set_agent_process_registry(name, orch.process_registry)

        hub.set_result_handler(name, stack.bot.on_task_result)
        hub.set_question_handler(name, stack.bot.on_task_question)

        # Register agent's primary chat_id for resolving CLI-submitted tasks
        if stack.config.allowed_user_ids:
            hub.set_agent_chat_id(name, stack.config.allowed_user_ids[0])

        logger.debug("Task hub wired for agent '%s'", name)

    async def _rebuild_stack(self, name: str, old_stack: AgentStack) -> AgentStack:
        """Rebuild an AgentStack from its config."""
        new_stack = await AgentStack.create(
            name,
            old_stack.config,
            is_main=old_stack.is_main,
        )
        self._stacks[name] = new_stack
        if self._bus:
            self._bus.register(name, new_stack)
            self._bus.set_async_result_handler(name, new_stack.bot.on_async_interagent_result)
        return new_stack

    # -- Sub-agent lifecycle ------------------------------------------------

    async def _sync_sub_agents(self) -> None:
        """Load agents.json and start any sub-agents not yet running."""
        sub_agents = self._registry.load()
        for sub_cfg in sub_agents:
            if sub_cfg.name not in self._stacks:
                await self._start_sub_agent(sub_cfg)

    async def _start_sub_agent(self, sub_cfg: SubAgentConfig) -> None:
        """Create and start a new sub-agent."""
        name = sub_cfg.name
        if name == "main":
            logger.warning("Cannot create sub-agent named 'main' — reserved")
            return

        agent_home = self._main_paths.ductor_home / "agents" / name
        config = merge_sub_agent_config(self._main_config, sub_cfg, agent_home)

        try:
            stack = await AgentStack.create(name, config)
        except Exception:
            logger.exception("Failed to create sub-agent '%s'", name)
            return

        # Workspace init creates config.json from config.example (main defaults).
        # Overwrite model/provider/effort so the on-disk config matches agents.json.
        config_path = agent_home / "config" / "config.json"
        if config_path.exists():
            await update_config_file_async(
                config_path,
                provider=config.provider,
                model=config.model,
                reasoning_effort=config.reasoning_effort,
            )

        self._stacks[name] = stack
        self._health[name] = AgentHealth(name=name)
        if self._bus:
            self._bus.register(name, stack)
            self._bus.set_async_result_handler(name, stack.bot.on_async_interagent_result)

        self._tasks[name] = asyncio.create_task(
            self._supervised_run(name, stack),
            name=f"agent:{name}",
        )

        # Sync shared knowledge into the new agent's MAINMEMORY.md
        if self._shared_knowledge:
            await self._shared_knowledge.sync_agent(stack.paths.mainmemory_path)

        logger.info("Sub-agent '%s' started (home=%s)", name, agent_home)

    async def stop_agent(self, name: str) -> None:
        """Stop a sub-agent gracefully."""
        if name == "main":
            logger.warning("Cannot stop main agent via stop_agent()")
            return

        task = self._tasks.pop(name, None)
        if task and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

        stack = self._stacks.pop(name, None)
        if stack:
            with contextlib.suppress(Exception):
                await stack.shutdown()

        if self._bus:
            self._bus.unregister(name)

        health = self._health.get(name)
        if health:
            health.mark_stopped()

        logger.info("Sub-agent '%s' stopped", name)

    async def start_agent_by_name(self, name: str) -> str:
        """Start a sub-agent by name from the registry. Returns status message."""
        if name in self._stacks:
            return f"Agent '{name}' is already running."

        agents = self._registry.load()
        match = next((a for a in agents if a.name == name), None)
        if match is None:
            return f"Agent '{name}' not found in agents.json."

        await self._start_sub_agent(match)
        return f"Agent '{name}' started."

    async def restart_agent(self, name: str) -> str:
        """Restart a sub-agent (stop + start). Returns status message."""
        if name == "main":
            return "Cannot restart main agent via this command. Use /restart instead."

        agents = self._registry.load()
        match = next((a for a in agents if a.name == name), None)
        if match is None:
            return f"Agent '{name}' not found in agents.json."

        if name in self._stacks:
            await self.stop_agent(name)

        await self._start_sub_agent(match)
        return f"Agent '{name}' restarted."

    # -- FileWatcher callback -----------------------------------------------

    async def _on_agents_changed(self) -> None:
        """Called when agents.json mtime changes. Sync running agents."""
        async with self._agents_lock:
            desired = {a.name: a for a in self._registry.load()}
            current_sub = set(self._stacks.keys()) - {"main"}
            desired_names = set(desired.keys())

            # Start new agents
            for name in desired_names - current_sub:
                logger.info("agents.json: new agent '%s' detected, starting", name)
                await self._start_sub_agent(desired[name])

            # Stop removed agents
            for name in current_sub - desired_names:
                logger.info("agents.json: agent '%s' removed, stopping", name)
                await self.stop_agent(name)

            # Check for config changes on existing agents
            for name in desired_names & current_sub:
                sub_cfg = desired[name]
                existing = self._stacks.get(name)
                if existing is None:
                    continue

                # Rebuild config and compare credentials
                agent_home = self._main_paths.ductor_home / "agents" / name
                new_config = merge_sub_agent_config(self._main_config, sub_cfg, agent_home)
                if _config_changed(new_config, existing.config):
                    logger.info("agents.json: agent '%s' config changed, restarting", name)
                    await self.stop_agent(name)
                    await self._start_sub_agent(sub_cfg)

    # -- Notifications ------------------------------------------------------

    async def _notify_main_agent(self, message: str) -> None:
        """Send a system notification to the main agent's users/rooms."""
        main = self._stacks.get("main")
        if main is None:
            return
        try:
            await main.bot.notification_service.notify_all(f"**[Supervisor]** {message}")
        except Exception:
            logger.exception("Failed to notify main agent")

    # -- Abort all ----------------------------------------------------------

    async def abort_all_agents(self) -> int:
        """Kill active CLI processes on ALL agent stacks (without stopping agents).

        Called by the main bot's ``/stop_all`` handler via callback.
        Returns total number of killed processes across all agents.
        """
        killed = 0
        for name, stack in list(self._stacks.items()):
            orch = stack.bot.orchestrator
            if orch is None:
                continue
            try:
                k = await orch.process_registry.kill_all_active()
                if orch.bg_observer:
                    chat_ids = {t.chat_id for t in orch.bg_observer._tasks.values()}
                    for cid in chat_ids:
                        k += await orch.bg_observer.cancel_all(cid)
                if k:
                    logger.info("Abort-all killed %d process(es) on agent '%s'", k, name)
                killed += k
            except Exception:
                logger.exception("Error aborting processes on agent '%s'", name)

        # Cancel in-flight async inter-agent tasks on the bus
        if self._bus:
            cancelled = await self._bus.cancel_all_async()
            if cancelled:
                logger.info("Abort-all cancelled %d async inter-agent task(s)", cancelled)
            killed += cancelled

        # Cancel in-flight background tasks
        killed += await self._abort_all_tasks()

        return killed

    async def _abort_all_tasks(self) -> int:
        """Cancel all in-flight background tasks across all agents."""
        if self._task_hub is None:
            return 0
        total = 0
        for stack in list(self._stacks.values()):
            for cid in stack.config.allowed_user_ids:
                k = await self._task_hub.cancel_all(cid)
                if k:
                    logger.info("Abort-all cancelled %d task(s) for agent '%s'", k, stack.name)
                total += k
        return total

    # -- Shutdown -----------------------------------------------------------

    async def stop_all(self) -> None:
        """Shut down all agents and cleanup."""
        self._running = False
        await self._watcher.stop()
        if self._shared_knowledge:
            await self._shared_knowledge.stop()

        # Cancel in-flight async tasks before tearing down agents
        if self._bus:
            cancelled = await self._bus.cancel_all_async()
            if cancelled:
                logger.warning("Cancelled %d in-flight async inter-agent task(s)", cancelled)

        # Stop sub-agents first, then main
        sub_names = [n for n in list(self._stacks.keys()) if n != "main"]
        for name in sub_names:
            await self.stop_agent(name)

        # Stop main
        main_task = self._tasks.pop("main", None)
        if main_task and not main_task.done():
            main_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await main_task

        main_stack = self._stacks.pop("main", None)
        if main_stack:
            with contextlib.suppress(Exception):
                await main_stack.shutdown()

        if self._bus:
            self._bus.unregister("main")

        # Stop task hub
        if self._task_hub:
            await self._task_hub.shutdown()

        # Stop internal API
        if self._internal_api:
            await self._internal_api.stop()

        logger.info("AgentSupervisor stopped (all agents shut down)")
