"""Base class for task-executing observers (cron, webhook)."""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from ductor_bot.cli.param_resolver import RunOverrides, resolve_cli_config

if TYPE_CHECKING:
    from ductor_bot.cli.codex_cache import CodexModelCache
    from ductor_bot.cli.param_resolver import CLIRunConfig
    from ductor_bot.config import AgentConfig
    from ductor_bot.infra.oneshot_runner import OneShotResult
    from ductor_bot.workspace.paths import DuctorPaths

logger = logging.getLogger(__name__)


class BaseOneShotObserver:
    """Shared base for observers that execute one-shot CLI tasks.

    Provides:
    - Config and cache storage
    - ``resolve_execution_config()`` for building CLI execution configs
    - ``log_execution_result()`` for shared post-execution logging
    """

    def __init__(
        self,
        paths: DuctorPaths,
        config: AgentConfig,
        codex_cache: CodexModelCache,
    ) -> None:
        self._paths = paths
        self._config = config
        self._codex_cache = codex_cache

    def resolve_execution_config(
        self,
        task_overrides: RunOverrides,
    ) -> CLIRunConfig:
        """Build a CLI execution config from current settings and overrides."""
        return resolve_cli_config(
            self._config,
            self._codex_cache,
            task_overrides=task_overrides,
        )

    def log_execution_result(
        self,
        result: OneShotResult,
        label: str,
        job_id: str,
    ) -> None:
        """Log common post-execution details (timeout, stderr)."""
        if result.execution is None:
            return
        if result.execution.timed_out:
            logger.warning(
                "%s %s timed out after %.0fs",
                label,
                job_id,
                self._config.cli_timeout,
            )
        if result.execution.stderr:
            stderr_preview = result.execution.stderr.decode(errors="replace")[:500]
            logger.debug("%s stderr (%s): %s", label, job_id, stderr_preview)
