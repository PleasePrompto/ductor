"""Tests for the exception hierarchy."""

from ductor_bot.errors import (
    CLIError,
    DuctorError,
    PathValidationError,
    SecurityError,
    WorkspaceError,
)


def test_base_error_is_exception() -> None:
    assert issubclass(DuctorError, Exception)


def test_cli_error_inherits_base() -> None:
    err = CLIError("cli broke")
    assert isinstance(err, DuctorError)
    assert str(err) == "cli broke"


def test_workspace_error_inherits_base() -> None:
    assert isinstance(WorkspaceError("no workspace"), DuctorError)


def test_security_error_inherits_base() -> None:
    assert isinstance(SecurityError("blocked"), DuctorError)


def test_path_validation_error_inherits_security() -> None:
    err = PathValidationError("outside root")
    assert isinstance(err, SecurityError)
    assert isinstance(err, DuctorError)


def test_catch_all_with_base() -> None:
    """All subclasses catchable via DuctorError."""
    for cls in (CLIError, WorkspaceError, SecurityError, PathValidationError):
        try:
            raise cls("test")
        except DuctorError:
            pass
