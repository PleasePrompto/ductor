"""Extended paths tests for runtime properties."""

from __future__ import annotations

from pathlib import Path

from ductor_bot.workspace.paths import DuctorPaths


def _paths(tmp_path: Path) -> DuctorPaths:
    fw = tmp_path / "fw"
    return DuctorPaths(
        ductor_home=tmp_path / "home", home_defaults=fw / "workspace", framework_root=fw
    )


def test_cron_tasks_dir(tmp_path: Path) -> None:
    p = _paths(tmp_path)
    assert p.cron_tasks_dir == p.workspace / "cron_tasks"


def test_tools_dir(tmp_path: Path) -> None:
    p = _paths(tmp_path)
    assert p.tools_dir == p.workspace / "tools"


def test_mainmemory_path(tmp_path: Path) -> None:
    p = _paths(tmp_path)
    assert p.mainmemory_path == p.workspace / "memory_system" / "MAINMEMORY.md"


def test_config_example_path(tmp_path: Path) -> None:
    p = _paths(tmp_path)
    fw = tmp_path / "fw"
    fw.mkdir(exist_ok=True)
    (fw / "config.example.json").write_text("{}")
    assert p.config_example_path == fw / "config.example.json"


def test_home_defaults_path(tmp_path: Path) -> None:
    p = _paths(tmp_path)
    assert p.home_defaults == tmp_path / "fw" / "workspace"
