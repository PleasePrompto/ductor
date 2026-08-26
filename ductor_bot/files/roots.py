"""The set of directories the file browser is allowed to show.

The browser used to be rooted at ``~/.ductor`` alone, which meant the project
directories configured in ``project_roots`` — the ones an agent actually works
in — were unreachable from Telegram. Those roots are already an explicit,
user-maintained allowlist, so widening the browser to them adds no new trust.

Containment is still enforced on every navigation: a resolved path must sit
inside one of these roots or it is refused.
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path


def browsable_roots(ductor_home: Path, project_roots: Mapping[str, str]) -> dict[str, Path]:
    """Return ``label -> directory`` for everything the browser may show.

    Duplicate directories collapse to one entry, so mapping two topic names at
    the same folder does not list it twice. Non-existent paths are dropped:
    showing a root that cannot be opened reads as a broken browser.
    """
    roots: dict[str, Path] = {}
    seen: set[Path] = set()

    home = ductor_home.expanduser().resolve()
    if home.is_dir():
        roots["~/.ductor"] = home
        seen.add(home)

    for label, raw in sorted(project_roots.items()):
        try:
            path = Path(raw).expanduser().resolve()
        except (OSError, RuntimeError):
            continue
        if not path.is_dir() or path in seen:
            continue
        roots[label] = path
        seen.add(path)

    return roots


def contains(roots: Mapping[str, Path], target: Path) -> bool:
    """True when *target* sits inside one of *roots*."""
    resolved = target.resolve()
    return any(resolved == root or resolved.is_relative_to(root) for root in roots.values())


def label_for(roots: Mapping[str, Path], target: Path) -> tuple[str, Path] | None:
    """Return the ``(label, root)`` owning *target*, deepest root first.

    Deepest wins so a project nested inside another root is labelled with the
    more specific name rather than its parent's.
    """
    resolved = target.resolve()
    best: tuple[str, Path] | None = None
    for label, root in roots.items():
        owns = resolved == root or resolved.is_relative_to(root)
        if owns and (best is None or len(root.parts) > len(best[1].parts)):
            best = (label, root)
    return best
