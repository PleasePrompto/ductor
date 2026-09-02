"""Reading, writing and archiving a conversation's handoff.

Archiving moves the file out of the project folder entirely. Archives are never
injected and never listed unless asked for: the reliable way to stop an agent
reading finished working state is for it not to be in the room, not a rule
asking it politely to look away.
"""

from __future__ import annotations

import logging
import re
import shutil
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from ductor_bot.handoff.guard import assert_ignored, ensure_protected, is_git_repo
from ductor_bot.handoff.paths import archive_dir, handoff_dir, handoff_file
from ductor_bot.handoff.prompts import TEMPLATE

_LOG_HEADING = "## Log"

if TYPE_CHECKING:
    from pathlib import Path

    from ductor_bot.session.key import SessionKey
    from ductor_bot.workspace.paths import DuctorPaths

logger = logging.getLogger(__name__)

_SLUG_ILLEGAL = re.compile(r"[^a-z0-9]+")
_SLUG_MAX = 40


def _slug(text: str) -> str:
    """A short, filesystem-safe hint of what a handoff was about.

    Headings are skipped: every handoff starts with the same ones, so naming
    archives after them would produce a directory of files called
    ``objective.md``.
    """
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        cleaned = _SLUG_ILLEGAL.sub("-", stripped.lower()).strip("-")
        if cleaned:
            return cleaned[:_SLUG_MAX]
    return "handoff"


def _unused(candidate: Path) -> Path:
    """A free filename near *candidate*.

    Two archives can land in the same second — clear twice in quick succession,
    or a test doing exactly that — and an archive silently overwriting an older
    archive defeats the point of keeping them.
    """
    if not candidate.exists():
        return candidate
    for suffix in range(2, 100):
        alternative = candidate.with_name(f"{candidate.stem}-{suffix}{candidate.suffix}")
        if not alternative.exists():
            return alternative
    return candidate.with_name(f"{candidate.stem}-{datetime.now(UTC).timestamp():.0f}{candidate.suffix}")


class HandoffStore:
    """The active handoff for each conversation, and its archives."""

    def __init__(self, paths: DuctorPaths) -> None:
        self._paths = paths

    def read(self, key: SessionKey, folder: Path | None) -> str:
        """The current handoff, or "" when there is none."""
        path = handoff_file(key, folder, self._paths)
        try:
            return path.read_text(encoding="utf-8") if path.is_file() else ""
        except OSError as exc:
            logger.warning("Cannot read handoff %s: %s", path, exc)
            return ""

    def write(self, key: SessionKey, folder: Path | None, text: str) -> bool:
        """Write the handoff. False when refused, failed, or *text* is empty.

        Empty is refused deliberately: a consolidation that returns nothing is a
        failed turn, and replacing a good handoff with an empty one would lose
        precisely what this exists to keep.
        """
        if not text.strip():
            return False

        if folder is not None:
            guard = ensure_protected(folder)
            if not guard.ok:
                logger.warning("Handoff refused for %s: %s", folder, guard.detail)
                return False

        target = handoff_file(key, folder, self._paths)
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        except OSError as exc:
            logger.warning("Cannot write handoff %s: %s", target, exc)
            return False

        if folder is not None and is_git_repo(folder) and not assert_ignored(target):
            # Writing the rule is not proof it applies. An unprotected handoff
            # in a repository is how a working file reaches a commit, so remove
            # it rather than leave it there.
            logger.error("Handoff at %s is not ignored by git; removing it", target)
            target.unlink(missing_ok=True)
            return False
        return True

    def ensure_exists(self, key: SessionKey, folder: Path | None) -> bool:
        """Create the handoff skeleton if it is missing. True when it now exists.

        Existence must not depend on the model choosing to act. Asking it to
        create the file worked in the prompt and not in practice: the
        instruction arrived, and eleven tool calls later nothing had been
        written. Code makes the file; the model fills it.
        """
        if self.read(key, folder).strip():
            return True
        return self.write(key, folder, TEMPLATE)

    def has_content(self, key: SessionKey, folder: Path | None) -> bool:
        """True when the handoff holds more than the empty skeleton."""
        body = self.read(key, folder)
        return any(
            line.strip() and not line.lstrip().startswith("#") for line in body.splitlines()
        )

    def append_log(self, key: SessionKey, folder: Path | None, line: str) -> bool:
        """Append one factual line to the handoff's ``## Log``.

        Written by code, not by asking. The model was given the path, the
        sections and an explicit instruction, and across three turns and a
        hundred tool calls it wrote nothing — it is busy doing the user's work,
        and a logging chore loses every time. So the mechanical part (what was
        asked, when) is recorded here, and the model's judgement is spent at
        consolidation instead, where it is the only thing being asked for.
        """
        body = self.read(key, folder)
        if not body:
            if not self.ensure_exists(key, folder):
                return False
            body = self.read(key, folder)
        if _LOG_HEADING not in body:
            body = body.rstrip("\n") + f"\n\n{_LOG_HEADING}\n"
        head, _, tail = body.partition(_LOG_HEADING)
        updated = f"{head}{_LOG_HEADING}{tail.rstrip()}\n{line.rstrip()}\n"
        return self.write(key, folder, updated)

    def archive(self, key: SessionKey, folder: Path | None) -> Path | None:
        """Move the active handoff out of the folder. ``None`` when absent."""
        source = handoff_file(key, folder, self._paths)
        if not source.is_file():
            return None
        try:
            body = source.read_text(encoding="utf-8")
            stamp = datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%S")
            destination = archive_dir(key, self._paths) / f"{stamp}-{_slug(body)}.md"
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination = _unused(destination)
            shutil.move(str(source), str(destination))
        except OSError as exc:
            logger.warning("Cannot archive handoff %s: %s", source, exc)
            return None
        return destination

    def list_archives(self, key: SessionKey) -> list[Path]:
        """This conversation's archives, newest first. Never another's."""
        directory = archive_dir(key, self._paths)
        if not directory.is_dir():
            return []
        return sorted((p for p in directory.glob("*.md") if p.is_file()), reverse=True)

    def dir_for(self, folder: Path | None) -> Path:
        """Where active handoffs live, for the readiness gate to check."""
        return handoff_dir(folder, self._paths)
