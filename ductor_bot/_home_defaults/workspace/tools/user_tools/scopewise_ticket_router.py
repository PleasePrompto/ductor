#!/usr/bin/env python3
"""Workspace wrapper for the Scopewise Software Factory ticket router."""

from __future__ import annotations

import os
import sys
from pathlib import Path


def _candidate_roots() -> list[Path]:
    candidates: list[Path] = []
    env_root = os.environ.get("DUCTOR_FRAMEWORK_ROOT")
    if env_root:
        candidates.append(Path(env_root).expanduser())
    candidates.append(Path.home() / "repos" / "ductor")
    return candidates


def _ensure_repo_on_path() -> None:
    for candidate in _candidate_roots():
        module_path = candidate / "ductor_bot" / "factory" / "scopewise_ticket_router.py"
        if module_path.is_file():
            sys.path.insert(0, str(candidate))
            return
    msg = (
        "Could not find the Ductor repo checkout. "
        "Set DUCTOR_FRAMEWORK_ROOT or clone the repo to ~/repos/ductor."
    )
    raise SystemExit(msg)


def main() -> int:
    _ensure_repo_on_path()
    from ductor_bot.factory.scopewise_ticket_router import main as router_main

    return router_main()


if __name__ == "__main__":
    raise SystemExit(main())
