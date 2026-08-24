"""Shared test fixtures."""

from __future__ import annotations

from collections.abc import Iterator
from unittest.mock import patch

import pytest

from ductor_bot.config import (
    reset_antigravity_models,
    reset_gemini_models,
    reset_opencode_models,
)


@pytest.fixture(autouse=True)
def _no_real_process_signals() -> object:
    """Globally prevent tests from sending real signals to system processes.

    Multiple modules import process_tree helpers that send real OS signals.
    Mock processes carry arbitrary PIDs (e.g. 1, 10) that correspond to real
    system processes — sending signals to them crashes the desktop session.
    """
    with (
        patch(
            "ductor_bot.cli.process_registry.terminate_process_tree",
            return_value=None,
        ),
        patch(
            "ductor_bot.cli.process_registry.force_kill_process_tree",
            return_value=None,
        ),
        patch(
            "ductor_bot.cli.process_registry.interrupt_process",
            return_value=None,
        ),
        patch(
            "ductor_bot.cli.executor.force_kill_process_tree",
            return_value=None,
        ),
        patch(
            "ductor_bot.cli.gemini_provider.force_kill_process_tree",
            return_value=None,
        ),
        patch(
            "ductor_bot.cron.execution.force_kill_process_tree",
            return_value=None,
        ),
        patch(
            "ductor_bot.infra.pidlock.terminate_process_tree",
            return_value=None,
        ),
        patch(
            "ductor_bot.infra.pidlock.force_kill_process_tree",
            return_value=None,
        ),
        patch(
            "ductor_bot.infra.pidlock.list_process_descendants",
            return_value=[],
        ),
    ):
        yield


@pytest.fixture(autouse=True)
def _no_real_service_management() -> object:
    """Prevent tests from stopping/starting the real systemd service.

    ``lifecycle.stop_bot()`` calls ``_stop_service_if_running()`` which runs
    ``systemctl --user stop ductor.service`` — killing the live service on any
    machine where ductor is installed and running.
    """
    with patch(
        "ductor_bot.cli_commands.lifecycle._stop_service_if_running",
    ):
        yield


@pytest.fixture(autouse=True)
def _reset_runtime_model_registries() -> Iterator[None]:
    """Isolate dynamically-discovered model registries between tests.

    Booting the orchestrator runs real provider discovery (e.g. ``agy
    models``) which populates module-level runtime sets. Reset them around
    every test so discovery in one test cannot leak into another.
    """
    reset_gemini_models()
    reset_antigravity_models()
    reset_opencode_models()
    yield
    reset_gemini_models()
    reset_antigravity_models()
    reset_opencode_models()
