"""Orchestrator lifecycle: async factory, startup, shutdown, infra management."""

from __future__ import annotations

import asyncio
import logging
import os
import secrets
from typing import TYPE_CHECKING

from ductor_bot.files.allowed_roots import resolve_allowed_roots
from ductor_bot.infra.docker import DockerManager
from ductor_bot.workspace.init import inject_runtime_environment
from ductor_bot.workspace.paths import DuctorPaths, resolve_paths
from ductor_bot.workspace.skill_sync import cleanup_ductor_links, sync_bundled_skills, sync_skills

if TYPE_CHECKING:
    from ductor_bot.config import AgentConfig
    from ductor_bot.orchestrator.core import Orchestrator

logger = logging.getLogger(__name__)


def _docker_skill_resync(paths: DuctorPaths) -> None:
    """Re-run skill sync with copies so skills resolve inside Docker."""
    sync_bundled_skills(paths, docker_active=True)
    sync_skills(paths, docker_active=True)


async def create_orchestrator(
    config: AgentConfig,
    *,
    agent_name: str = "main",
) -> Orchestrator:
    """Async factory: build an Orchestrator.

    Workspace must already be initialized by the caller (``__main__.load_config``).
    """
    from ductor_bot.orchestrator.core import Orchestrator

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
        transport=config.transport,
    )

    orch = Orchestrator(
        config,
        paths,
        docker_container=docker_container,
        agent_name=agent_name,
        interagent_port=config.interagent_port,
    )
    orch._docker = docker_mgr

    from ductor_bot.cli.auth import AuthStatus, check_all_auth

    auth_results = await asyncio.to_thread(check_all_auth)
    orch._providers.apply_auth_results(
        auth_results,
        auth_status_enum=AuthStatus,
        cli_service=orch._cli_service,
    )

    if not orch._providers.available_providers:
        logger.error("No authenticated providers found! CLI calls will fail.")
    else:
        logger.info(
            "Available providers: %s",
            ", ".join(sorted(orch._providers.available_providers)),
        )

    await asyncio.to_thread(orch._providers.init_gemini_state, paths.workspace)

    installed_providers = frozenset(
        name for name, result in auth_results.items() if result.status is not AuthStatus.NOT_FOUND
    )
    codex_cache = await orch._observers.init_model_caches(
        installed_providers=installed_providers,
        on_gemini_refresh=orch._providers.on_gemini_models_refresh,
        on_antigravity_refresh=orch._providers.on_antigravity_models_refresh,
        on_grok_refresh=orch._providers.on_grok_models_refresh,
    )
    orch._observers.init_task_observers(
        cron_manager=orch._cron_manager,
        webhook_manager=orch._webhook_manager,
        cli_service=orch._cli_service,
        codex_cache=codex_cache,
    )
    orch._providers._codex_cache_fn = lambda: orch._observers.codex_cache
    await orch._observers.start_all(docker_container=docker_container)

    # Direct API server (WebSocket, designed for Tailscale)
    if config.api.enabled:
        await start_api_server(orch, config, paths)

    await orch._observers.start_config_reloader(
        on_hot_reload=orch._on_config_hot_reload,
        on_restart_needed=lambda fields: logger.warning(
            "Config changed but requires restart: %s", ", ".join(fields)
        ),
    )

    return orch


async def start_api_server(
    orch: Orchestrator,
    config: AgentConfig,
    paths: DuctorPaths,
) -> None:
    """Initialize and start the direct WebSocket API server."""
    try:
        from ductor_bot.api.server import ApiServer
    except ImportError:
        logger.warning(
            "API server enabled but PyNaCl is not installed. Install with: pip install ductor[api]"
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
    server.set_message_handler(orch.handle_message_streaming)
    server.set_abort_handler(orch.abort)
    server.set_file_context(
        allowed_roots=resolve_allowed_roots(config.file_access, paths.workspace),
        upload_dir=paths.api_files_dir,
        workspace=paths.workspace,
    )
    server.set_provider_info(orch._providers.build_provider_info(orch._observers.codex_cache_obs))
    server.set_active_state_getter(
        lambda: orch._providers.resolve_runtime_target(orch._config.model)
    )

    try:
        await server.start()
    except OSError:
        logger.exception(
            "Failed to start API server on %s:%d",
            config.api.host,
            config.api.port,
        )
        return

    orch._api_stop = server.stop


async def ensure_docker(orch: Orchestrator) -> None:
    """Health-check Docker before CLI calls; auto-recover or fall back."""
    if not orch._docker:
        return
    container = await orch._docker.ensure_running()
    if container:
        orch._cli_service.update_docker_container(container)
    elif orch._cli_service._config.docker_container:
        logger.warning("Docker recovery failed, falling back to host execution")
        orch._cli_service.update_docker_container("")


async def shutdown(orch: Orchestrator) -> None:
    """Cleanup on bot shutdown."""
    killed = await orch._process_registry.kill_all_active()
    if killed:
        logger.info("Shutdown terminated %d active CLI process(es)", killed)
    if orch._api_stop is not None:
        await orch._api_stop()
    await asyncio.to_thread(cleanup_ductor_links, orch._paths)
    await orch._observers.stop_all()
    if orch._docker:
        await orch._docker.teardown()
    logger.info("Orchestrator shutdown")
