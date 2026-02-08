"""Tests for /upgrade Telegram command."""

from __future__ import annotations

from unittest.mock import patch

from ductor_bot.infra.version import VersionInfo
from ductor_bot.orchestrator.commands import cmd_upgrade
from ductor_bot.orchestrator.core import Orchestrator


class TestCmdUpgrade:
    """Test /upgrade command handler."""

    async def test_shows_update_with_buttons(self, orch: Orchestrator) -> None:
        info = VersionInfo(
            current="1.0.0", latest="2.0.0", update_available=True, summary="Big update"
        )
        with (
            patch("ductor_bot.infra.install.detect_install_mode", return_value="pipx"),
            patch("ductor_bot.orchestrator.commands.check_pypi", return_value=info),
        ):
            result = await cmd_upgrade(orch, 1, "/upgrade")

        assert "Update available" in result.text
        assert "1.0.0" in result.text
        assert "2.0.0" in result.text
        assert "Big update" in result.text
        assert result.reply_markup is not None

        # Check button callback data
        buttons = result.reply_markup.inline_keyboard[0]
        callback_values = [b.callback_data for b in buttons]
        assert "upg:yes:2.0.0" in callback_values
        assert "upg:no" in callback_values

    async def test_shows_up_to_date(self, orch: Orchestrator) -> None:
        info = VersionInfo(current="2.0.0", latest="2.0.0", update_available=False, summary="")
        with (
            patch("ductor_bot.infra.install.detect_install_mode", return_value="pip"),
            patch("ductor_bot.orchestrator.commands.check_pypi", return_value=info),
        ):
            result = await cmd_upgrade(orch, 1, "/upgrade")

        assert "up to date" in result.text.lower()
        assert "2.0.0" in result.text
        assert result.reply_markup is None

    async def test_handles_pypi_failure(self, orch: Orchestrator) -> None:
        with (
            patch("ductor_bot.infra.install.detect_install_mode", return_value="pipx"),
            patch("ductor_bot.orchestrator.commands.check_pypi", return_value=None),
        ):
            result = await cmd_upgrade(orch, 1, "/upgrade")

        assert "could not reach" in result.text.lower() or "pypi" in result.text.lower()
        assert result.reply_markup is None

    async def test_button_text_is_user_friendly(self, orch: Orchestrator) -> None:
        info = VersionInfo(current="1.0.0", latest="1.1.0", update_available=True, summary="Patch")
        with (
            patch("ductor_bot.infra.install.detect_install_mode", return_value="pipx"),
            patch("ductor_bot.orchestrator.commands.check_pypi", return_value=info),
        ):
            result = await cmd_upgrade(orch, 1, "/upgrade")

        buttons = result.reply_markup.inline_keyboard[0]
        labels = [b.text for b in buttons]
        assert any("upgrade" in label.lower() or "yes" in label.lower() for label in labels)
        assert any("not" in label.lower() or "no" in label.lower() for label in labels)

    async def test_version_info_in_up_to_date(self, orch: Orchestrator) -> None:
        info = VersionInfo(current="3.5.1", latest="3.5.1", update_available=False, summary="")
        with (
            patch("ductor_bot.infra.install.detect_install_mode", return_value="pip"),
            patch("ductor_bot.orchestrator.commands.check_pypi", return_value=info),
        ):
            result = await cmd_upgrade(orch, 1, "/upgrade")

        assert "3.5.1" in result.text
        assert "latest" in result.text.lower()

    async def test_dev_mode_rejects_upgrade(self, orch: Orchestrator) -> None:
        with patch("ductor_bot.infra.install.detect_install_mode", return_value="dev"):
            result = await cmd_upgrade(orch, 1, "/upgrade")

        assert "source" in result.text.lower() or "git pull" in result.text.lower()
        assert result.reply_markup is None
