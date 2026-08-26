"""Git state for a browsed directory, and the two actions offered on it.

Only ``ahead`` is exact: it compares two local refs. ``behind`` reflects
whatever the last ``git fetch`` recorded, so it is a lower bound that can be
stale. Fetching on every directory view would put seconds of network latency in
front of a file listing, so the pull action fetches instead and the label says
how fresh the number is.

Every command runs with an explicit timeout and a clean environment. A hung
credential prompt would otherwise block a chat handler indefinitely.
"""

from __future__ import annotations

import logging
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger(__name__)

_TIMEOUT = 20
_PUSH_TIMEOUT = 120
_PULL_TIMEOUT = 120


@dataclass(frozen=True, slots=True)
class GitState:
    """What the browser needs to know about a repository."""

    root: Path
    branch: str
    ahead: int
    behind: int
    dirty: int
    has_upstream: bool

    @property
    def can_push(self) -> bool:
        """Exact: both refs are local."""
        return self.ahead > 0

    @property
    def known_behind(self) -> int:
        """Commits to pull as of the last fetch. May understate reality."""
        return self.behind


def _run(args: list[str], cwd: Path, timeout: int = _TIMEOUT) -> tuple[int, str]:
    """Run a git command, returning ``(returncode, combined output)``."""
    env = {
        **os.environ,
        # Never block a chat handler on an interactive prompt.
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_ASKPASS": "",
        "SSH_ASKPASS": "",
    }
    try:
        proc = subprocess.run(
            ["git", *args],
            cwd=str(cwd),
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
            env=env,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        logger.warning("git %s failed in %s: %s", args[0], cwd, exc)
        return 1, str(exc)
    return proc.returncode, (proc.stdout + proc.stderr).strip()


def repo_root(directory: Path) -> Path | None:
    """The work-tree root containing *directory*, or None if it is not a repo."""
    code, out = _run(["rev-parse", "--show-toplevel"], directory)
    if code != 0 or not out:
        return None
    return Path(out.splitlines()[0])


def read_state(directory: Path) -> GitState | None:
    """Describe the repository containing *directory*. No network access."""
    root = repo_root(directory)
    if root is None:
        return None

    _, branch = _run(["rev-parse", "--abbrev-ref", "HEAD"], root)
    _, dirty_out = _run(["status", "--porcelain"], root)
    dirty = len([line for line in dirty_out.splitlines() if line.strip()])

    code, _ = _run(["rev-parse", "--abbrev-ref", "@{u}"], root)
    if code != 0:
        return GitState(root, branch, 0, 0, dirty, has_upstream=False)

    ahead = behind = 0
    code, counts = _run(["rev-list", "--left-right", "--count", "@{u}...HEAD"], root)
    if code == 0 and counts:
        parts = counts.split()
        if len(parts) == 2 and all(p.isdigit() for p in parts):
            behind, ahead = int(parts[0]), int(parts[1])

    return GitState(root, branch, ahead, behind, dirty, has_upstream=True)


def pending_commits(state: GitState, limit: int = 10) -> list[str]:
    """Subjects of the commits a push would publish.

    Shown before pushing so the confirmation is informed rather than a button
    press against an unknown number of changes.
    """
    if not state.can_push:
        return []
    _, out = _run(["log", "--oneline", "--no-decorate", f"-{limit}", "@{u}..HEAD"], state.root)
    return [line for line in out.splitlines() if line.strip()]


def pull(state: GitState) -> tuple[bool, str]:
    """Fast-forward the repository. Returns ``(ok, output)``.

    ``--ff-only`` on purpose: a pull that silently creates a merge commit, from
    a phone, in someone else's repository, is not a good surprise. Divergence is
    reported so it can be resolved deliberately.
    """
    code, out = _run(["pull", "--ff-only"], state.root, timeout=_PULL_TIMEOUT)
    return code == 0, out or "(no output)"


def push(state: GitState) -> tuple[bool, str]:
    """Push the current branch to its upstream. Returns ``(ok, output)``."""
    code, out = _run(["push"], state.root, timeout=_PUSH_TIMEOUT)
    return code == 0, out or "(no output)"
