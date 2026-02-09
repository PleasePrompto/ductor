"""Tests for DuctorPaths and resolve_paths."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

from ductor_bot.workspace.paths import DuctorPaths, resolve_paths


def test_workspace_property() -> None:
    paths = DuctorPaths(
        ductor_home=Path("/home/test/.ductor"),
        home_defaults=Path("/opt/ductor/workspace"),
        framework_root=Path("/opt/ductor"),
    )
    assert paths.workspace == Path("/home/test/.ductor/workspace")


def test_config_path() -> None:
    paths = DuctorPaths(
        ductor_home=Path("/home/test/.ductor"),
        home_defaults=Path("/opt/ductor/workspace"),
        framework_root=Path("/opt/ductor"),
    )
    assert paths.config_path == Path("/home/test/.ductor/config/config.json")


def test_sessions_path() -> None:
    paths = DuctorPaths(
        ductor_home=Path("/home/test/.ductor"),
        home_defaults=Path("/opt/ductor/workspace"),
        framework_root=Path("/opt/ductor"),
    )
    assert paths.sessions_path == Path("/home/test/.ductor/sessions.json")


def test_logs_dir() -> None:
    paths = DuctorPaths(
        ductor_home=Path("/home/test/.ductor"),
        home_defaults=Path("/opt/ductor/workspace"),
        framework_root=Path("/opt/ductor"),
    )
    assert paths.logs_dir == Path("/home/test/.ductor/logs")


def test_home_defaults() -> None:
    paths = DuctorPaths(
        ductor_home=Path("/x"),
        home_defaults=Path("/opt/ductor/workspace"),
        framework_root=Path("/opt/ductor"),
    )
    assert paths.home_defaults == Path("/opt/ductor/workspace")


def test_resolve_paths_explicit() -> None:
    paths = resolve_paths(ductor_home="/tmp/test_home", framework_root="/tmp/test_fw")
    assert paths.ductor_home == Path("/tmp/test_home").resolve()
    assert paths.framework_root == Path("/tmp/test_fw").resolve()


def test_resolve_paths_env_vars() -> None:
    with patch.dict(
        os.environ, {"DUCTOR_HOME": "/tmp/env_home", "DUCTOR_FRAMEWORK_ROOT": "/tmp/env_fw"}
    ):
        paths = resolve_paths()
        assert paths.ductor_home == Path("/tmp/env_home").resolve()
        assert paths.framework_root == Path("/tmp/env_fw").resolve()


def test_resolve_paths_defaults() -> None:
    with patch.dict(os.environ, {}, clear=True):
        env_clean = {
            k: v for k, v in os.environ.items() if k not in ("DUCTOR_HOME", "DUCTOR_FRAMEWORK_ROOT")
        }
        with patch.dict(os.environ, env_clean, clear=True):
            paths = resolve_paths()
            assert paths.ductor_home == (Path.home() / ".ductor").resolve()
