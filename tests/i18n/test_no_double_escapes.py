"""Translations must not contain double-escaped sequences.

A string written into TOML through a generator that escapes backslashes ends up
as ``\\\\n`` in the file, which TOML parses back to a literal backslash-n. The
result reaches the chat as the characters a user sees as `\\n` rather than a line
break — invisible to lint, types and every behavioural test, because the value
is technically a valid string throughout.

This caught four keys across eight locales after they had already shipped.
"""

from __future__ import annotations

from pathlib import Path

import pytest

_I18N = Path(__file__).resolve().parents[2] / "ductor_bot" / "i18n"


def _toml_files() -> list[Path]:
    return sorted(_I18N.rglob("*.toml"))


def test_there_are_translations_to_check() -> None:
    """Guards against the glob silently matching nothing."""
    assert len(_toml_files()) > 8


@pytest.mark.parametrize("path", _toml_files(), ids=lambda p: f"{p.parent.name}/{p.name}")
def test_no_double_escaped_newlines(path: Path) -> None:
    offenders = [
        f"{n}: {line.strip()[:80]}"
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if "\\\\n" in line
    ]
    assert not offenders, (
        f"{path.parent.name}/{path.name} has double-escaped newlines, which render "
        f"literally in chat:\n  " + "\n  ".join(offenders)
    )


@pytest.mark.parametrize("path", _toml_files(), ids=lambda p: f"{p.parent.name}/{p.name}")
def test_no_double_escaped_tabs_or_quotes(path: Path) -> None:
    """Same failure mode, other escapes."""
    offenders = [
        f"{n}: {line.strip()[:80]}"
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1)
        if "\\\\t" in line or '\\\\"' in line
    ]
    assert not offenders, "double-escaped sequences:\n  " + "\n  ".join(offenders)
