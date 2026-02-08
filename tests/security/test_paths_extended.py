"""Extended path validation tests -- covering gaps."""

from __future__ import annotations

from pathlib import Path

import pytest

from ductor_bot.errors import PathValidationError
from ductor_bot.security.paths import validate_file_path


def test_dotdot_traversal_blocked(tmp_path: Path) -> None:
    """../  traversal should be caught by resolve()."""
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("secret")

    with pytest.raises(PathValidationError):
        validate_file_path(str(allowed / ".." / "outside" / "secret.txt"), [allowed])


def test_relative_path_rejected(tmp_path: Path) -> None:
    """Relative paths are inherently suspicious."""
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    # resolve() turns relative to absolute -- but it likely won't be inside allowed_roots
    with pytest.raises(PathValidationError):
        validate_file_path("./sneaky.txt", [allowed])
