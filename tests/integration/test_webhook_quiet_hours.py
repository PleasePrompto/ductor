"""Integration tests: WebhookObserver quiet hour behaviour for cron_task dispatch."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import patch

import time_machine

from ductor_bot.cli.codex_cache import CodexModelCache
from ductor_bot.cli.param_resolver import TaskOverrides
from ductor_bot.config import AgentConfig, HeartbeatConfig, ModelRegistry, WebhookConfig
from ductor_bot.webhook.manager import WebhookManager
from ductor_bot.webhook.models import WebhookEntry
from ductor_bot.webhook.observer import WebhookObserver
from ductor_bot.workspace.paths import DuctorPaths

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_paths(tmp_path: Path) -> DuctorPaths:
    fw = tmp_path / "fw"
    paths = DuctorPaths(
        ductor_home=tmp_path / "home",
        home_defaults=fw / "workspace",
        framework_root=fw,
    )
    paths.cron_tasks_dir.mkdir(parents=True)
    return paths


def _make_manager(paths: DuctorPaths) -> WebhookManager:
    return WebhookManager(hooks_path=paths.webhooks_path)


def _make_config(**overrides: Any) -> AgentConfig:
    defaults: dict[str, Any] = {
        "webhooks": WebhookConfig(enabled=True, token="test-token"),
    }
    defaults.update(overrides)
    return AgentConfig(**defaults)


def _make_models() -> ModelRegistry:
    return ModelRegistry()


def _make_codex_cache() -> CodexModelCache:
    return CodexModelCache(last_updated=datetime.now(UTC).isoformat(), models=[])


def _make_observer(
    paths: DuctorPaths,
    mgr: WebhookManager,
    **config_overrides: Any,
) -> WebhookObserver:
    return WebhookObserver(
        paths,
        mgr,
        config=_make_config(**config_overrides),
        models=_make_models(),
        codex_cache=_make_codex_cache(),
    )


def _add_hook(mgr: WebhookManager, paths: DuctorPaths, **overrides: Any) -> WebhookEntry:
    defaults: dict[str, Any] = {
        "id": "test-hook",
        "title": "Test Hook",
        "description": "Testing",
        "mode": "cron_task",
        "prompt_template": "{{msg}}",
        "task_folder": "test_task",
    }
    defaults.update(overrides)
    hook = WebhookEntry(**defaults)
    mgr.add_hook(hook)
    task_folder = defaults.get("task_folder")
    if task_folder:
        (paths.cron_tasks_dir / task_folder).mkdir(exist_ok=True)
    return hook


def _default_overrides() -> TaskOverrides:
    return TaskOverrides(provider=None, model=None, reasoning_effort=None, cli_parameters=[])


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


@time_machine.travel("2025-06-15T23:30:00+00:00")
async def test_webhook_cron_task_skips_during_quiet_hours(tmp_path: Path) -> None:
    """Webhook cron_task returns skipped:quiet_hours when in global quiet window."""
    paths = _make_paths(tmp_path)
    mgr = _make_manager(paths)
    obs = _make_observer(
        paths,
        mgr,
        user_timezone="UTC",
        heartbeat=HeartbeatConfig(quiet_start=21, quiet_end=8),
    )
    _add_hook(mgr, paths)

    result = await obs._dispatch_cron_task(
        "test-hook",
        "Test Hook",
        "test_task",
        "test prompt",
        _default_overrides(),
    )

    assert result.status == "skipped:quiet_hours"
    assert result.hook_id == "test-hook"
    assert result.mode == "cron_task"


@time_machine.travel("2025-06-15T14:00:00+00:00")
async def test_webhook_runs_during_active_hours(tmp_path: Path) -> None:
    """Webhook cron_task proceeds past quiet-hours check during active hours."""
    paths = _make_paths(tmp_path)
    mgr = _make_manager(paths)
    obs = _make_observer(
        paths,
        mgr,
        user_timezone="UTC",
        heartbeat=HeartbeatConfig(quiet_start=21, quiet_end=8),
    )
    _add_hook(mgr, paths)

    with patch("ductor_bot.webhook.observer.build_cmd", return_value=None):
        result = await obs._dispatch_cron_task(
            "test-hook",
            "Test Hook",
            "test_task",
            "test prompt",
            _default_overrides(),
        )

    # Passed quiet-hours check, hit cli_not_found because build_cmd returned None
    assert "cli_not_found" in result.status


@time_machine.travel("2025-06-15T14:00:00+00:00")
async def test_webhook_respects_task_specific_quiet_hours(tmp_path: Path) -> None:
    """Webhook uses hook-specific quiet_start/quiet_end, overriding global config."""
    paths = _make_paths(tmp_path)
    mgr = _make_manager(paths)
    obs = _make_observer(
        paths,
        mgr,
        user_timezone="UTC",
        heartbeat=HeartbeatConfig(quiet_start=21, quiet_end=8),
    )
    # Hook-specific quiet hours: 10-16 (14:00 is quiet)
    _add_hook(mgr, paths, quiet_start=10, quiet_end=16)

    result = await obs._dispatch_cron_task(
        "test-hook",
        "Test Hook",
        "test_task",
        "test prompt",
        _default_overrides(),
    )

    assert result.status == "skipped:quiet_hours"
