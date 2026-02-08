"""Shared fixtures for orchestrator tests."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from ductor_bot.config import AgentConfig
from ductor_bot.orchestrator.core import Orchestrator
from ductor_bot.workspace.init import init_workspace
from ductor_bot.workspace.paths import DuctorPaths


def setup_framework(fw_root: Path) -> None:
    """Create minimal home-defaults template for testing."""
    ws = fw_root / "workspace"
    ws.mkdir(parents=True)
    (ws / "CLAUDE.md").write_text("# Ductor Home")

    config_dir = ws / "config"
    config_dir.mkdir()

    inner = ws / "workspace"
    inner.mkdir()
    (inner / "CLAUDE.md").write_text("# Framework CLAUDE.md")

    for subdir in ("memory_system", "cron_tasks", "output_to_user", "telegram_files"):
        d = inner / subdir
        d.mkdir()
        (d / "CLAUDE.md").write_text(f"# {subdir}")

    (inner / "memory_system" / "MAINMEMORY.md").write_text("# Main Memory\n")

    tools = inner / "tools"
    tools.mkdir()
    (tools / "CLAUDE.md").write_text("# Tools")

    (fw_root / "config.example.json").write_text('{"provider": "claude", "model": "opus"}')


@pytest.fixture
def workspace(tmp_path: Path) -> tuple[DuctorPaths, AgentConfig]:
    """Fully initialized workspace with models and config."""
    fw_root = tmp_path / "fw"
    setup_framework(fw_root)
    paths = DuctorPaths(
        ductor_home=tmp_path / "home", home_defaults=fw_root / "workspace", framework_root=fw_root
    )
    init_workspace(paths)
    config = AgentConfig()
    return paths, config


@pytest.fixture
def orch(workspace: tuple[DuctorPaths, AgentConfig]) -> Orchestrator:
    """Orchestrator with mocked CLIService."""
    paths, config = workspace
    o = Orchestrator(config, paths)
    mock_cli = MagicMock()
    mock_cli.execute = AsyncMock()
    mock_cli.execute_streaming = AsyncMock()
    object.__setattr__(o, "_cli_service", mock_cli)
    return o
