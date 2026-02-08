"""Shared test fixtures."""

from __future__ import annotations

from pathlib import Path

import pytest


@pytest.fixture
def tmp_ductor_home(tmp_path: Path) -> Path:
    """Temporary ~/.ductor equivalent."""
    home = tmp_path / ".ductor"
    home.mkdir()
    return home


@pytest.fixture
def tmp_workspace(tmp_ductor_home: Path) -> Path:
    """Temporary workspace directory."""
    ws = tmp_ductor_home / "workspace"
    ws.mkdir()
    return ws
