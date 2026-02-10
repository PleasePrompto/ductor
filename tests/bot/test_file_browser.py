"""Tests for the interactive ~/.ductor file browser."""

from __future__ import annotations

from pathlib import Path

import pytest

from ductor_bot.bot.file_browser import (
    SF_FILE_PREFIX,
    SF_PREFIX,
    file_browser_start,
    handle_file_browser_callback,
    is_file_browser_callback,
)
from ductor_bot.workspace.paths import DuctorPaths


@pytest.fixture
def paths(tmp_path: Path) -> DuctorPaths:
    """DuctorPaths with a temporary ductor_home populated with test dirs/files."""
    home = tmp_path / "ductor"
    home.mkdir()

    (home / "config").mkdir()
    (home / "config" / "config.json").write_text("{}")
    (home / "workspace").mkdir()
    (home / "workspace" / "skills").mkdir()
    (home / "workspace" / "tools").mkdir()
    (home / "workspace" / "tools" / "cron_tools").mkdir()
    (home / "workspace" / "CLAUDE.md").write_text("# rules")
    (home / "logs").mkdir()
    (home / "sessions.json").write_text("[]")

    # Hidden file/dir and __pycache__ should be excluded
    (home / ".hidden_file").write_text("secret")
    (home / ".hidden_dir").mkdir()
    (home / "workspace" / "__pycache__").mkdir()

    return DuctorPaths(ductor_home=home)


# ---------------------------------------------------------------------------
# is_file_browser_callback
# ---------------------------------------------------------------------------


class TestIsFileBrowserCallback:
    @pytest.mark.parametrize(
        "data",
        ["sf:", "sf:workspace", "sf:workspace/skills", "sf!config", "sf!workspace/tools"],
    )
    def test_valid_callbacks(self, data: str) -> None:
        assert is_file_browser_callback(data) is True

    @pytest.mark.parametrize(
        "data",
        ["", "ms:p:claude", "w:1", "mq:5", "sf", "SF:", "other"],
    )
    def test_invalid_callbacks(self, data: str) -> None:
        assert is_file_browser_callback(data) is False


# ---------------------------------------------------------------------------
# file_browser_start (root listing)
# ---------------------------------------------------------------------------


class TestFileBrowserStart:
    async def test_root_listing_shows_directories(self, paths: DuctorPaths) -> None:
        text, _keyboard = await file_browser_start(paths)

        assert "File Browser" in text
        assert "~/.ductor/" in text
        assert "config/" in text
        assert "logs/" in text
        assert "workspace/" in text

    async def test_root_listing_shows_files(self, paths: DuctorPaths) -> None:
        text, _ = await file_browser_start(paths)

        assert "sessions.json" in text

    async def test_root_listing_excludes_hidden(self, paths: DuctorPaths) -> None:
        text, _ = await file_browser_start(paths)

        assert ".hidden_file" not in text
        assert ".hidden_dir" not in text

    async def test_root_has_no_back_button(self, paths: DuctorPaths) -> None:
        _, keyboard = await file_browser_start(paths)

        assert not any("Back" in btn.text for row in keyboard.inline_keyboard for btn in row)

    async def test_root_has_file_request_button(self, paths: DuctorPaths) -> None:
        _, keyboard = await file_browser_start(paths)

        last_row = keyboard.inline_keyboard[-1]
        assert len(last_row) == 1
        assert last_row[0].callback_data == f"{SF_FILE_PREFIX}"
        assert "file" in last_row[0].text.lower()

    async def test_root_has_folder_buttons(self, paths: DuctorPaths) -> None:
        _, keyboard = await file_browser_start(paths)

        folder_callbacks = [
            btn.callback_data
            for row in keyboard.inline_keyboard
            for btn in row
            if btn.callback_data and btn.callback_data.startswith(SF_PREFIX)
        ]
        assert f"{SF_PREFIX}config" in folder_callbacks
        assert f"{SF_PREFIX}logs" in folder_callbacks
        assert f"{SF_PREFIX}workspace" in folder_callbacks


# ---------------------------------------------------------------------------
# handle_file_browser_callback -- directory navigation
# ---------------------------------------------------------------------------


class TestDirectoryNavigation:
    async def test_navigate_to_subfolder(self, paths: DuctorPaths) -> None:
        text, keyboard, prompt = await handle_file_browser_callback(paths, "sf:workspace")

        assert prompt is None
        assert keyboard is not None
        assert "~/.ductor/workspace/" in text
        assert "skills/" in text
        assert "tools/" in text
        assert "CLAUDE.md" in text

    async def test_subfolder_has_back_button(self, paths: DuctorPaths) -> None:
        _, keyboard, _ = await handle_file_browser_callback(paths, "sf:workspace")

        assert keyboard is not None
        back_buttons = [
            btn for row in keyboard.inline_keyboard for btn in row if "Back" in btn.text
        ]
        assert len(back_buttons) == 1
        assert back_buttons[0].callback_data == "sf:"

    async def test_deep_navigation(self, paths: DuctorPaths) -> None:
        text, keyboard, _ = await handle_file_browser_callback(paths, "sf:workspace/tools")

        assert "~/.ductor/workspace/tools/" in text
        assert "cron_tools/" in text
        assert keyboard is not None

    async def test_deep_back_goes_to_parent(self, paths: DuctorPaths) -> None:
        _, keyboard, _ = await handle_file_browser_callback(paths, "sf:workspace/tools")

        assert keyboard is not None
        back_buttons = [
            btn for row in keyboard.inline_keyboard for btn in row if "Back" in btn.text
        ]
        assert len(back_buttons) == 1
        assert back_buttons[0].callback_data == "sf:workspace"

    async def test_excludes_pycache(self, paths: DuctorPaths) -> None:
        text, _, _ = await handle_file_browser_callback(paths, "sf:workspace")

        assert "__pycache__" not in text

    async def test_empty_directory(self, paths: DuctorPaths) -> None:
        text, keyboard, _ = await handle_file_browser_callback(paths, "sf:logs")

        assert "(empty)" in text
        assert keyboard is not None

    async def test_nonexistent_directory(self, paths: DuctorPaths) -> None:
        text, keyboard, _ = await handle_file_browser_callback(paths, "sf:does_not_exist")

        assert "not found" in text.lower()
        assert keyboard is not None


# ---------------------------------------------------------------------------
# handle_file_browser_callback -- file request (sf!)
# ---------------------------------------------------------------------------


class TestFileRequest:
    async def test_file_request_returns_prompt(self, paths: DuctorPaths) -> None:
        text, keyboard, prompt = await handle_file_browser_callback(paths, "sf!workspace/tools")

        assert prompt is not None
        assert "file" in prompt.lower()
        assert text == ""
        assert keyboard is None

    async def test_file_request_root(self, paths: DuctorPaths) -> None:
        _, _, prompt = await handle_file_browser_callback(paths, "sf!")

        assert prompt is not None
        assert str(paths.ductor_home.resolve()) in prompt


# ---------------------------------------------------------------------------
# Path traversal safety
# ---------------------------------------------------------------------------


class TestPathSafety:
    async def test_parent_traversal_blocked(self, paths: DuctorPaths) -> None:
        text, _, _ = await handle_file_browser_callback(paths, "sf:..")

        assert "not found" in text.lower()

    async def test_double_parent_traversal_blocked(self, paths: DuctorPaths) -> None:
        text, _, _ = await handle_file_browser_callback(paths, "sf:workspace/../../etc")

        assert "not found" in text.lower()

    async def test_absolute_path_blocked(self, paths: DuctorPaths) -> None:
        text, _, _ = await handle_file_browser_callback(paths, "sf:/etc/passwd")

        assert "not found" in text.lower()
