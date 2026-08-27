"""The folder gate, and how it composes with the persona gate.

The composition is the risky part. Two gates both want to hold the incoming
message, and a mistake there loses it silently — which is exactly the class of
bug that shipped once already, when a redundant condition meant a conversation
could never be asked for its persona. Lint, types and the rest of the suite all
pass while it happens.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from ductor_bot.i18n import init
from ductor_bot.orchestrator.selectors import folder_selector as fs
from ductor_bot.workspace.topic_bindings import SHARED_WORKSPACE, BindingStore

KEY = SimpleNamespace(storage_key="tg:-100123:7", chat_id=-100123)


@pytest.fixture(autouse=True)
def _english() -> None:
    init("en")


@pytest.fixture
def orch(tmp_path: Path) -> SimpleNamespace:
    emr = tmp_path / "IT" / "EMR"
    emr.mkdir(parents=True)
    phoenix = tmp_path / "IT" / "Phoenix"
    phoenix.mkdir()
    roots = {"EMR": str(emr), "Phoenix": str(phoenix)}
    return SimpleNamespace(
        config=SimpleNamespace(project_roots=roots),
        bindings=BindingStore(tmp_path / "bindings.json"),
    )


def _labels(resp) -> list[str]:
    return [b.text for row in resp.buttons.rows for b in row]


def test_selector_lists_every_root_plus_the_shared_choice(orch) -> None:
    resp = fs.folder_selector(orch, KEY, asking=True)
    labels = _labels(resp)
    assert "EMR" in labels
    assert "Phoenix" in labels
    assert any("Shared workspace" in label for label in labels)


def test_a_root_that_no_longer_exists_is_not_offered(orch, tmp_path: Path) -> None:
    """Binding to it would only produce a working directory that cannot be used."""
    orch.config.project_roots["Ghost"] = str(tmp_path / "not-here")
    assert "Ghost" not in _labels(fs.folder_selector(orch, KEY))


def test_choice_resolves_by_index(orch) -> None:
    roots = orch.config.project_roots
    assert fs.resolve_choice(roots, 0) == roots["EMR"]
    assert fs.resolve_choice(roots, -1) == SHARED_WORKSPACE
    assert fs.resolve_choice(roots, 99) is None


def test_index_is_stable_against_the_64_byte_callback_cap(orch) -> None:
    """Paths would overflow callback_data; indices cannot."""
    resp = fs.folder_selector(orch, KEY)
    for row in resp.buttons.rows:
        for button in row:
            assert len(button.callback_data.encode()) <= 64


def test_current_choice_is_marked(orch) -> None:
    orch.bindings.set(KEY.storage_key, orch.config.project_roots["EMR"])
    assert any(label.startswith("✅") and "EMR" in label for label in _labels(
        fs.folder_selector(orch, KEY)
    ))


def test_shared_workspace_is_offered_when_nothing_is_configured(orch) -> None:
    """A gate with no acceptable answer is a lock, not a question."""
    orch.config.project_roots.clear()
    resp = fs.folder_selector(orch, KEY, asking=True)
    labels = _labels(resp)
    assert len(labels) == 1
    assert "Shared workspace" in labels[0]


def test_shared_workspace_satisfies_the_gate_without_a_directory(orch) -> None:
    orch.bindings.set(KEY.storage_key, SHARED_WORKSPACE)
    assert orch.bindings.has_choice(KEY.storage_key) is True
    assert orch.bindings.resolve(KEY.storage_key) is None


# ---------------------------------------------------------------------------
# Gate composition
# ---------------------------------------------------------------------------


class _FakeGates:
    """The ordering logic from TelegramApp, isolated from aiogram.

    Mirrors ``_on_message`` and ``_handle_folder_selector``: folder first, then
    persona, each holding the message rather than discarding it.
    """

    def __init__(self, bindings: BindingStore, roots: dict[str, str]) -> None:
        self.bindings = bindings
        self.roots = roots
        self.personas: dict[str, str] = {}
        self.persona_held: dict[str, str] = {}
        self.asked: list[str] = []
        self.ran: list[str] = []

    def on_message(self, key: str, text: str) -> None:
        if self._ask_folder(key, text):
            return
        if self._ask_persona(key, text):
            return
        self.ran.append(text)

    def _ask_folder(self, key: str, text: str) -> bool:
        if self.bindings.has_choice(key) or not self.roots:
            return False
        self.bindings.hold(key, text)
        self.asked.append("folder")
        return True

    def _ask_persona(self, key: str, text: str) -> bool:
        if key in self.personas:
            return False
        self.persona_held[key] = text
        self.asked.append("persona")
        return True

    def choose_folder(self, key: str, directory: str) -> None:
        self.bindings.set(key, directory)
        held = self.bindings.take(key)
        if not held:
            return
        if self._ask_persona(key, held):
            return
        self.ran.append(held)

    def choose_persona(self, key: str, persona: str) -> None:
        self.personas[key] = persona
        held = self.persona_held.pop(key, None)
        if held:
            self.ran.append(held)


def test_both_gates_run_the_original_message_exactly_once(orch) -> None:
    gates = _FakeGates(orch.bindings, orch.config.project_roots)
    key = KEY.storage_key

    gates.on_message(key, "fix the login bug")
    assert gates.asked == ["folder"]
    assert gates.ran == []

    gates.choose_folder(key, orch.config.project_roots["EMR"])
    assert gates.asked == ["folder", "persona"]
    assert gates.ran == [], "work must not start before the persona is chosen"

    gates.choose_persona(key, "coder")
    assert gates.ran == ["fix the login bug"]


def test_a_bound_conversation_asks_only_for_the_persona(orch) -> None:
    orch.bindings.set(KEY.storage_key, orch.config.project_roots["EMR"])
    gates = _FakeGates(orch.bindings, orch.config.project_roots)

    gates.on_message(KEY.storage_key, "hello")
    assert gates.asked == ["persona"]


def test_a_fully_answered_conversation_asks_nothing(orch) -> None:
    gates = _FakeGates(orch.bindings, orch.config.project_roots)
    orch.bindings.set(KEY.storage_key, SHARED_WORKSPACE)
    gates.personas[KEY.storage_key] = "coder"

    gates.on_message(KEY.storage_key, "carry on")
    assert gates.asked == []
    assert gates.ran == ["carry on"]


def test_no_roots_means_no_folder_gate(orch) -> None:
    """An installation that configured nothing must not be locked out."""
    gates = _FakeGates(orch.bindings, {})
    gates.on_message(KEY.storage_key, "hello")
    assert gates.asked == ["persona"]
