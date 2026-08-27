"""Tests for the persona picker."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from ductor_bot.i18n import init
from ductor_bot.orchestrator.selectors.persona_selector import (
    PRS_PREFIX,
    is_persona_selector_callback,
    parse_callback,
    persona_selector,
    resolve_choice,
)
from ductor_bot.personas.catalog import Persona
from ductor_bot.personas.store import NO_PERSONA, PersonaStore
from ductor_bot.session.key import SessionKey

_PERSONAS = [
    Persona("coder", "Code, debugging, review", Path("/a/coder.md")),
    Persona("designer", "UI/UX and brand", Path("/a/designer.md")),
    Persona("scout", "Open-web investigation", Path("/a/scout.md")),
]


@pytest.fixture(autouse=True)
def _english() -> None:
    init("en")


def _orch(tmp_path: Path, chosen: str | None = None) -> Any:
    orch = MagicMock()
    store = PersonaStore(tmp_path / "personas.json")
    if chosen is not None:
        store.set(SessionKey.telegram(1, 2).storage_key, chosen)
    orch.personas = store
    return orch


def _patched(personas: list[Persona] | None = None) -> Any:
    return patch(
        "ductor_bot.orchestrator.selectors.persona_selector.load_personas",
        return_value=_PERSONAS if personas is None else personas,
    )


def _buttons(resp: Any) -> list:
    return [b for row in resp.buttons.rows for b in row]


def test_callback_matching() -> None:
    assert is_persona_selector_callback(f"{PRS_PREFIX}0")
    assert not is_persona_selector_callback("ms:p:claude")


def test_every_persona_is_offered(tmp_path: Path) -> None:
    with _patched():
        resp = persona_selector(_orch(tmp_path), SessionKey.telegram(1, 2))
    assert [b.text for b in _buttons(resp)] == ["coder", "designer", "scout"]


def test_no_default_button_when_personas_exist(tmp_path: Path) -> None:
    """An escape hatch would become the path of least resistance."""
    with _patched():
        resp = persona_selector(_orch(tmp_path), SessionKey.telegram(1, 2))
    assert all("Default" not in b.text for b in _buttons(resp))


def test_default_is_the_only_option_when_none_are_defined(tmp_path: Path) -> None:
    """Someone else's checkout must not be left with a dead menu."""
    with _patched([]):
        resp = persona_selector(_orch(tmp_path), SessionKey.telegram(1, 2))
    buttons = _buttons(resp)
    assert len(buttons) == 1
    assert "Default" in buttons[0].text


def test_current_choice_is_marked(tmp_path: Path) -> None:
    with _patched():
        resp = persona_selector(_orch(tmp_path, "designer"), SessionKey.telegram(1, 2))
    marked = [b.text for b in _buttons(resp) if b.text.startswith("✅")]
    assert marked == ["✅ designer"]


def test_descriptions_are_shown(tmp_path: Path) -> None:
    with _patched():
        resp = persona_selector(_orch(tmp_path), SessionKey.telegram(1, 2))
    assert "Open-web investigation" in resp.text


def test_ask_wording_differs_from_the_command(tmp_path: Path) -> None:
    with _patched():
        asked = persona_selector(_orch(tmp_path), SessionKey.telegram(1, 2), asking=True)
        listed = persona_selector(_orch(tmp_path), SessionKey.telegram(1, 2))
    assert asked.text != listed.text


def test_callback_data_stays_within_telegram_limit(tmp_path: Path) -> None:
    long_name = "p" * 300
    with _patched([Persona(long_name, "d", Path("/a/x.md"))]):
        resp = persona_selector(_orch(tmp_path), SessionKey.telegram(1, 2))
    for b in _buttons(resp):
        assert len(b.callback_data.encode()) <= 64


def test_resolve_choice_maps_index_to_name() -> None:
    with _patched():
        assert resolve_choice(0) == "coder"
        assert resolve_choice(2) == "scout"


def test_resolve_choice_default_index_means_no_persona() -> None:
    with _patched():
        assert resolve_choice(-1) == NO_PERSONA


def test_resolve_choice_out_of_range_is_none() -> None:
    with _patched():
        assert resolve_choice(99) is None


def test_parse_callback_rejects_rubbish() -> None:
    assert parse_callback(f"{PRS_PREFIX}notanint") is None
    assert parse_callback(f"{PRS_PREFIX}1") == 1
