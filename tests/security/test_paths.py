"""Tests for path validation."""

from pathlib import Path

import pytest

from ductor_bot.errors import PathValidationError
from ductor_bot.security.paths import is_path_safe, validate_file_path


def test_valid_path_inside_root(tmp_path: Path) -> None:
    f = tmp_path / "test.txt"
    f.write_text("ok")
    result = validate_file_path(str(f), [tmp_path])
    assert result == f.resolve()


def test_path_outside_all_roots(tmp_path: Path) -> None:
    with pytest.raises(PathValidationError, match="outside allowed roots"):
        validate_file_path("/etc/passwd", [tmp_path])


def test_null_byte_in_path(tmp_path: Path) -> None:
    with pytest.raises(PathValidationError, match="null byte"):
        validate_file_path("/tmp/evil\x00file", [tmp_path])


def test_control_characters_in_path(tmp_path: Path) -> None:
    with pytest.raises(PathValidationError, match="control characters"):
        validate_file_path("/tmp/evil\x01file", [tmp_path])


def test_multiple_allowed_roots(tmp_path: Path) -> None:
    root_a = tmp_path / "a"
    root_b = tmp_path / "b"
    root_a.mkdir()
    root_b.mkdir()
    f = root_b / "file.txt"
    f.write_text("ok")
    result = validate_file_path(str(f), [root_a, root_b])
    assert result == f.resolve()


def test_is_path_safe_returns_true(tmp_path: Path) -> None:
    f = tmp_path / "safe.txt"
    f.write_text("ok")
    assert is_path_safe(str(f), [tmp_path]) is True


def test_is_path_safe_returns_false(tmp_path: Path) -> None:
    assert is_path_safe("/etc/shadow", [tmp_path]) is False


def test_is_path_safe_no_exception_on_bad_path(tmp_path: Path) -> None:
    assert is_path_safe("/nonexistent/\x00evil", [tmp_path]) is False


def test_symlink_traversal(tmp_path: Path) -> None:
    allowed = tmp_path / "allowed"
    allowed.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("sensitive")
    link = allowed / "link.txt"
    link.symlink_to(secret)
    with pytest.raises(PathValidationError, match="outside allowed roots"):
        validate_file_path(str(link), [allowed])
