"""Central path resolution for the workspace layout.

This module is the SINGLE SOURCE OF TRUTH for all paths in the framework.
Every path the framework needs is either a field or property of ``DuctorPaths``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

# ductor_bot/workspace/paths.py -> ductor_bot/workspace -> ductor_bot
_PKG_DIR = Path(__file__).resolve().parent.parent


def _default_home_defaults() -> Path:
    return _PKG_DIR / "_home_defaults"


def _default_framework_root() -> Path:
    return _PKG_DIR.parent


@dataclass(frozen=True)
class DuctorPaths:
    """Resolved, immutable paths for the workspace layout.

    All framework paths are derived from three roots:

    - ``ductor_home``:    User data directory (default ``~/.ductor``).
    - ``home_defaults``:  Bundled template that mirrors ``ductor_home`` (package-internal).
    - ``framework_root``: Repository root (for Dockerfile, config.example.json).
    """

    ductor_home: Path
    home_defaults: Path = field(default_factory=_default_home_defaults)
    framework_root: Path = field(default_factory=_default_framework_root)

    # -- User data paths (inside ductor_home) --

    @property
    def workspace(self) -> Path:
        return self.ductor_home / "workspace"

    @property
    def config_dir(self) -> Path:
        return self.ductor_home / "config"

    @property
    def config_path(self) -> Path:
        return self.config_dir / "config.json"

    @property
    def sessions_path(self) -> Path:
        return self.ductor_home / "sessions.json"

    @property
    def cron_jobs_path(self) -> Path:
        return self.ductor_home / "cron_jobs.json"

    @property
    def webhooks_path(self) -> Path:
        return self.ductor_home / "webhooks.json"

    @property
    def logs_dir(self) -> Path:
        return self.ductor_home / "logs"

    @property
    def cron_tasks_dir(self) -> Path:
        return self.workspace / "cron_tasks"

    @property
    def tools_dir(self) -> Path:
        return self.workspace / "tools"

    @property
    def user_tools_dir(self) -> Path:
        return self.workspace / "tools" / "user_tools"

    @property
    def output_to_user_dir(self) -> Path:
        return self.workspace / "output_to_user"

    @property
    def telegram_files_dir(self) -> Path:
        return self.workspace / "telegram_files"

    @property
    def memory_system_dir(self) -> Path:
        return self.workspace / "memory_system"

    @property
    def skills_dir(self) -> Path:
        return self.workspace / "skills"

    @property
    def mainmemory_path(self) -> Path:
        return self.memory_system_dir / "MAINMEMORY.md"

    # -- Framework paths (bundled with package or repo root) --

    @property
    def config_example_path(self) -> Path:
        """Config example: repo root (dev) or package-bundled (installed)."""
        repo_path = self.framework_root / "config.example.json"
        if repo_path.is_file():
            return repo_path
        return _PKG_DIR / "_config_example.json"


def resolve_paths(
    ductor_home: str | Path | None = None,
    *,
    framework_root: str | Path | None = None,
    home_defaults: str | Path | None = None,
) -> DuctorPaths:
    """Build DuctorPaths from explicit values, env vars, or defaults.

    Args:
        ductor_home: User data directory. Falls back to ``$DUCTOR_HOME`` or ``~/.ductor``.
        framework_root: Repository root. Falls back to ``$DUCTOR_FRAMEWORK_ROOT``.
        home_defaults: Template directory. Falls back to ``ductor_bot/_home_defaults/``.
    """
    if ductor_home is not None:
        home = Path(ductor_home).expanduser().resolve()
    else:
        home = (
            Path(
                os.environ.get("DUCTOR_HOME", str(Path.home() / ".ductor")),
            )
            .expanduser()
            .resolve()
        )

    if framework_root is not None:
        fw = Path(framework_root).expanduser().resolve()
    else:
        env_fw = os.environ.get("DUCTOR_FRAMEWORK_ROOT")
        fw = Path(env_fw).resolve() if env_fw else _default_framework_root()

    hd = Path(home_defaults).resolve() if home_defaults is not None else _default_home_defaults()

    return DuctorPaths(ductor_home=home, home_defaults=hd, framework_root=fw)
