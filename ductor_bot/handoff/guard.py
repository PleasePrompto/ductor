"""Keeping agent-written handoffs out of the user's git history.

``.git/info/exclude`` rather than ``.gitignore``: it is untracked, so the agent
never modifies a file git is watching and nothing can reach a commit by
accident. A ``.gitignore`` entry stays opt-in by the user.

It is re-applied on every write rather than once, because re-cloning a
repository recreates ``.git`` and takes the exclusion with it while leaving the
working tree looking untouched.

Ignore rules do not apply to files git already tracks, so the exclusion has to
exist *before* the first write, never after it.
"""

from __future__ import annotations

import logging
import subprocess
from dataclasses import dataclass
from pathlib import Path

from ductor_bot.handoff.paths import HANDOFF_DIR_NAME

logger = logging.getLogger(__name__)

_ENTRY = f"{HANDOFF_DIR_NAME}/"
_TIMEOUT_SECONDS = 10


@dataclass(frozen=True, slots=True)
class GuardResult:
    """Whether this folder can host a protected handoff, and why not."""

    ok: bool
    reason: str = ""
    detail: str = ""


def is_git_repo(folder: Path) -> bool:
    """True when *folder* is the top of a git working tree."""
    return (folder / ".git").exists()


def ensure_protected(folder: Path) -> GuardResult:
    """Make sure ``handoffs/`` is excluded from git. Safe to call every write."""
    if not is_git_repo(folder):
        return GuardResult(ok=True)

    info = folder / ".git" / "info"
    exclude = info / "exclude"
    try:
        info.mkdir(parents=True, exist_ok=True)
        current = exclude.read_text(encoding="utf-8") if exclude.is_file() else ""
        if any(line.strip() == _ENTRY for line in current.splitlines()):
            return GuardResult(ok=True)
        separator = "" if not current or current.endswith("\n") else "\n"
        exclude.write_text(f"{current}{separator}{_ENTRY}\n", encoding="utf-8")
    except OSError as exc:
        return GuardResult(
            ok=False,
            reason="exclude_unwritable",
            detail=f"{exclude}: {exc.strerror or exc}",
        )
    return GuardResult(ok=True)


def assert_ignored(path: Path) -> bool:
    """Ask git whether *path* is ignored.

    Writing the rule is not evidence that it applies — a tracked file, a
    negation later in the file, or a repository that was re-cloned mid-flight
    all produce a rule that reads correctly and does nothing.
    """
    try:
        proc = subprocess.run(
            ["git", "check-ignore", "-q", str(path)],
            cwd=str(path.parent),
            capture_output=True,
            timeout=_TIMEOUT_SECONDS,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning("git check-ignore failed for %s: %s", path, exc)
        return False
    return proc.returncode == 0
