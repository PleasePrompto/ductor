"""File path validation and containment checks."""

from __future__ import annotations

import logging
from collections.abc import Sequence
from pathlib import Path

from ductor_bot.errors import PathValidationError

logger = logging.getLogger(__name__)


def validate_file_path(
    path: str | Path,
    allowed_roots: Sequence[Path],
) -> Path:
    """Resolve and validate a file path against allowed root directories."""
    raw = str(path)

    if "\x00" in raw:
        msg = f"Path contains null byte: {raw!r}"
        raise PathValidationError(msg)

    if any(ord(c) < 32 for c in raw if c != "\n"):
        msg = f"Path contains control characters: {raw!r}"
        raise PathValidationError(msg)

    resolved = Path(raw).resolve()

    for root in allowed_roots:
        resolved_root = root.resolve()
        if resolved.is_relative_to(resolved_root):
            logger.debug("Path allowed: %s", resolved)
            return resolved

    logger.warning("Path blocked: %s (outside allowed roots)", resolved)
    root_list = ", ".join(str(r) for r in allowed_roots)
    msg = f"Path {resolved} is outside allowed roots: {root_list}"
    raise PathValidationError(msg)


def is_path_safe(
    path: str | Path,
    allowed_roots: Sequence[Path],
) -> bool:
    """Non-throwing version of validate_file_path."""
    try:
        validate_file_path(path, allowed_roots)
    except (PathValidationError, OSError):
        return False
    return True
