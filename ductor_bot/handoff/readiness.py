"""Refusing to start a turn the workspace cannot host.

A fallback location would work today and clutter the system tomorrow: a second
directory nobody is looking at, filling with files that were meant to live
beside the work. So this gate repairs what is unambiguously safe and otherwise
stops before a single token is spent, naming the file and the reason so it can
be fixed and retried.

Safe repairs are creating ``handoffs/`` and adding the git exclusion. Never
``chmod``, never ``chown``, never a tracked ``.gitignore`` — a gate that quietly
widens permissions is worse than the problem it solves.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ductor_bot.handoff.guard import ensure_protected
from ductor_bot.handoff.paths import handoff_dir

if TYPE_CHECKING:
    from pathlib import Path

    from ductor_bot.workspace.paths import DuctorPaths


@dataclass(frozen=True, slots=True)
class Readiness:
    """Whether a handoff can be written here, and what to tell the user.

    ``key`` is a translation key rather than a message, so the reason survives
    into every locale instead of being English-only in a log line.
    """

    ok: bool
    key: str = ""
    detail: str = ""


def check_readiness(folder: Path | None, paths: DuctorPaths) -> Readiness:
    """Check the conversation's handoff location, repairing what is safe."""
    base = folder if folder is not None else paths.ductor_home

    if not base.is_dir():
        return Readiness(ok=False, key="handoff.folder_missing", detail=str(base))
    if not os.access(base, os.W_OK):
        return Readiness(ok=False, key="handoff.not_writable", detail=str(base))

    if folder is not None:
        guard = ensure_protected(folder)
        if not guard.ok:
            return Readiness(ok=False, key="handoff.exclude_unwritable", detail=guard.detail)

    directory = handoff_dir(folder, paths)
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        return Readiness(
            ok=False,
            key="handoff.dir_uncreatable",
            detail=f"{directory}: {exc.strerror or exc}",
        )
    return Readiness(ok=True)
