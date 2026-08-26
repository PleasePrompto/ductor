"""Tests for the interactive file browser.

Rewritten for token addressing: callback data used to carry a relative path,
which overflowed Telegram's 64-byte cap on deep trees. Paths are now addressed
by an opaque token, so the traversal cases below assert that a token resolving
outside the allowed roots is refused — a crafted "../" string is no longer
expressible in callback data at all.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from ductor_bot.files import path_tokens
from ductor_bot.i18n import init
from ductor_bot.messenger.telegram.file_browser import (
    SF_ASK_PREFIX,
    SF_FILE_PREFIX,
    SF_PREFIX,
    file_browser_start,
    handle_file_browser_callback,
    is_file_browser_callback,
)
from ductor_bot.workspace.paths import DuctorPaths


@pytest.fixture(autouse=True)
def _clean() -> None:
    path_tokens.clear()
    init("en")


@pytest.fixture
def paths(tmp_path: Path) -> DuctorPaths:
    home = tmp_path / "ductor"
    home.mkdir()

    (home / "config").mkdir()
    (home / "config" / "config.json").write_text("{}")
    (home / "workspace").mkdir()
    (home / "workspace" / "skills").mkdir()
    (home / "workspace" / "tools").mkdir()
    (home / "workspace" / "CLAUDE.md").write_text("# rules")
    (home / "sessions.json").write_text("[]")

    # Hidden entries and caches must never be listed.
    (home / ".hidden_file").write_text("secret")
    (home / ".hidden_dir").mkdir()
    (home / "workspace" / "__pycache__").mkdir()

    return DuctorPaths(ductor_home=home)


def _buttons(kb) -> list:
    return [b for row in kb.inline_keyboard for b in row]


def _open(paths: DuctorPaths, target: Path):
    return handle_file_browser_callback(paths, {}, f"{SF_PREFIX}{path_tokens.token_for(target)}")


# -- callback matching ---------------------------------------------------------


class TestCallbackMatching:
    def test_matches_own_prefixes(self) -> None:
        for prefix in (SF_PREFIX, SF_FILE_PREFIX, SF_ASK_PREFIX, "sf@"):
            assert is_file_browser_callback(f"{prefix}abc")

    def test_ignores_other_namespaces(self) -> None:
        assert not is_file_browser_callback("ms:p:claude")
        assert not is_file_browser_callback("acc:0")


# -- navigation ----------------------------------------------------------------


class TestDirectoryNavigation:
    @pytest.mark.asyncio
    async def test_root_view_lists_the_ductor_home(self, paths: DuctorPaths) -> None:
        _text, kb = await file_browser_start(paths, {})
        assert "~/.ductor/" in [b.text for b in _buttons(kb)]

    @pytest.mark.asyncio
    async def test_opening_home_lists_children(self, paths: DuctorPaths) -> None:
        action = await _open(paths, paths.ductor_home)
        assert "config/" in action.text
        assert "workspace/" in action.text
        assert "sessions.json" in action.text

    @pytest.mark.asyncio
    async def test_hidden_and_cache_entries_are_excluded(self, paths: DuctorPaths) -> None:
        action = await _open(paths, paths.ductor_home)
        assert ".hidden_file" not in action.text
        assert ".hidden_dir" not in action.text
        assert "__pycache__" not in action.text

    @pytest.mark.asyncio
    async def test_empty_directory_says_so(self, paths: DuctorPaths) -> None:
        action = await _open(paths, paths.ductor_home / "workspace" / "skills")
        assert "empty" in action.text.lower()

    @pytest.mark.asyncio
    async def test_subdirectory_offers_a_way_back(self, paths: DuctorPaths) -> None:
        action = await _open(paths, paths.ductor_home / "workspace")
        back = [b for b in _buttons(action.keyboard) if "Back" in b.text]
        assert len(back) == 1

    @pytest.mark.asyncio
    async def test_back_is_present_at_a_root_too(self, paths: DuctorPaths) -> None:
        """A button that vanishes at certain depths reads as a broken screen."""
        action = await _open(paths, paths.ductor_home)
        back = [b for b in _buttons(action.keyboard) if "Back" in b.text]
        assert len(back) == 1

    @pytest.mark.asyncio
    async def test_back_at_a_root_returns_to_the_location_picker(self, paths: DuctorPaths) -> None:
        action = await _open(paths, paths.ductor_home)
        back = next(b for b in _buttons(action.keyboard) if "Back" in b.text)
        assert back.callback_data == SF_PREFIX

    @pytest.mark.asyncio
    async def test_back_inside_a_tree_goes_to_the_parent(self, paths: DuctorPaths) -> None:
        target = paths.ductor_home / "workspace"
        action = await _open(paths, target)
        back = next(b for b in _buttons(action.keyboard) if "Back" in b.text)
        assert path_tokens.path_for(back.callback_data[len(SF_PREFIX) :]) == target.parent

    @pytest.mark.asyncio
    async def test_no_duplicate_navigation_button_at_a_root(self, paths: DuctorPaths) -> None:
        """Back and Locations do the same thing at a root; only one should show."""
        action = await _open(paths, paths.ductor_home)
        nav = [b for b in _buttons(action.keyboard) if b.callback_data == SF_PREFIX]
        assert len(nav) == 1

    @pytest.mark.asyncio
    async def test_nonexistent_directory_falls_back_to_roots(self, paths: DuctorPaths) -> None:
        action = await _open(paths, paths.ductor_home / "no-such-dir")
        assert "~/.ductor/" in [b.text for b in _buttons(action.keyboard)]


# -- containment ---------------------------------------------------------------


class TestPathSafety:
    @pytest.mark.asyncio
    async def test_parent_of_root_is_refused(self, paths: DuctorPaths, tmp_path: Path) -> None:
        action = await _open(paths, paths.ductor_home.parent)
        assert "~/.ductor/" in [b.text for b in _buttons(action.keyboard)]

    @pytest.mark.asyncio
    async def test_unrelated_absolute_path_is_refused(self, paths: DuctorPaths) -> None:
        action = await _open(paths, Path("/etc"))
        assert "passwd" not in action.text

    @pytest.mark.asyncio
    async def test_traversal_out_of_root_is_refused(self, paths: DuctorPaths) -> None:
        escaped = paths.ductor_home / ".." / ".."
        action = await _open(paths, escaped)
        assert "~/.ductor/" in [b.text for b in _buttons(action.keyboard)]


# -- agent handoff -------------------------------------------------------------


class TestFileRequest:
    @pytest.mark.asyncio
    async def test_ask_returns_a_prompt_naming_the_directory(self, paths: DuctorPaths) -> None:
        target = paths.ductor_home / "workspace"
        action = await handle_file_browser_callback(
            paths, {}, f"{SF_ASK_PREFIX}{path_tokens.token_for(target)}"
        )
        assert action.agent_prompt is not None
        assert str(target.resolve()) in action.agent_prompt

    @pytest.mark.asyncio
    async def test_ask_at_root_uses_the_home_directory(self, paths: DuctorPaths) -> None:
        action = await handle_file_browser_callback(
            paths, {}, f"{SF_ASK_PREFIX}{path_tokens.token_for(paths.ductor_home)}"
        )
        assert action.agent_prompt is not None
        assert str(paths.ductor_home.resolve()) in action.agent_prompt
