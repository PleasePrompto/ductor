"""Tests for ductor install CLI command."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from ductor_bot.cli_commands.install import (
    _install_extra,
    _is_installed,
    cmd_install,
    print_install_help,
)


class TestIsInstalled:
    """Tests for _is_installed helper."""

    def test_known_module_returns_true(self) -> None:
        assert _is_installed("json") is True

    def test_unknown_module_returns_false(self) -> None:
        assert _is_installed("nonexistent_module_xyz_123") is False


class TestPrintInstallHelp:
    """Tests for print_install_help."""

    def test_runs_without_error(self) -> None:
        print_install_help()


class TestCmdInstall:
    """Tests for cmd_install entry point."""

    def test_no_args_shows_help(self) -> None:
        with patch("ductor_bot.cli_commands.install.print_install_help") as mock_help:
            cmd_install([])
            mock_help.assert_called_once()

    def test_single_arg_shows_help(self) -> None:
        with patch("ductor_bot.cli_commands.install.print_install_help") as mock_help:
            cmd_install(["install"])
            mock_help.assert_called_once()

    def test_unknown_extra_shows_help(self) -> None:
        with patch("ductor_bot.cli_commands.install.print_install_help") as mock_help:
            cmd_install(["install", "nonexistent"])
            mock_help.assert_called_once()

    def test_valid_extra_calls_install(self) -> None:
        with patch("ductor_bot.cli_commands.install._install_extra") as mock_install:
            cmd_install(["install", "matrix"])
            mock_install.assert_called_once_with("matrix")

    def test_valid_extra_api_calls_install(self) -> None:
        with patch("ductor_bot.cli_commands.install._install_extra") as mock_install:
            cmd_install(["install", "api"])
            mock_install.assert_called_once_with("api")

    def test_valid_extra_slack_calls_install(self) -> None:
        with patch("ductor_bot.cli_commands.install._install_extra") as mock_install:
            cmd_install(["install", "slack"])
            mock_install.assert_called_once_with("slack")


class TestInstallExtra:
    """Tests for _install_extra."""

    def test_already_installed_shows_message(self) -> None:
        with patch("ductor_bot.cli_commands.install._is_installed", return_value=True):
            console = MagicMock()
            with patch("ductor_bot.cli_commands.install.Console", return_value=console):
                _install_extra("matrix")
            # Should mention "already installed"
            call_args = console.print.call_args_list
            assert any("already installed" in str(c) for c in call_args)

    def test_unknown_extra_shows_error(self) -> None:
        console = MagicMock()
        with patch("ductor_bot.cli_commands.install.Console", return_value=console):
            _install_extra("bogus")
        call_args = console.print.call_args_list
        assert any("Unknown extra" in str(c) for c in call_args)

    def test_not_installed_runs_subprocess_pip(self) -> None:
        with (
            patch("ductor_bot.cli_commands.install._is_installed", return_value=False),
            patch(
                "ductor_bot.cli_commands.install.detect_install_mode",
                return_value="pip",
            ),
            patch("ductor_bot.cli_commands.install.subprocess.run") as mock_run,
            patch("ductor_bot.cli_commands.install.Console", return_value=MagicMock()),
        ):
            mock_run.return_value = MagicMock(returncode=0, stderr="")
            _install_extra("matrix")
            mock_run.assert_called_once()
            cmd = mock_run.call_args[0][0]
            assert "ductor[matrix]" in cmd

    def test_not_installed_runs_subprocess_pipx(self) -> None:
        with (
            patch("ductor_bot.cli_commands.install._is_installed", return_value=False),
            patch(
                "ductor_bot.cli_commands.install.detect_install_mode",
                return_value="pipx",
            ),
            patch("ductor_bot.cli_commands.install.subprocess.run") as mock_run,
            patch("ductor_bot.cli_commands.install.Console", return_value=MagicMock()),
        ):
            mock_run.return_value = MagicMock(returncode=0, stderr="")
            _install_extra("api")
            mock_run.assert_called_once()
            cmd = mock_run.call_args[0][0]
            assert "ductor[api]" in cmd
            assert "-e" not in cmd

    def test_not_installed_runs_subprocess_dev(self) -> None:
        with (
            patch("ductor_bot.cli_commands.install._is_installed", return_value=False),
            patch(
                "ductor_bot.cli_commands.install.detect_install_mode",
                return_value="dev",
            ),
            patch("ductor_bot.cli_commands.install.subprocess.run") as mock_run,
            patch("ductor_bot.cli_commands.install.Console", return_value=MagicMock()),
        ):
            mock_run.return_value = MagicMock(returncode=0, stderr="")
            _install_extra("matrix")
            mock_run.assert_called_once()
            cmd = mock_run.call_args[0][0]
            assert "-e" in cmd
            assert ".[matrix]" in cmd

    def test_failed_install_shows_error(self) -> None:
        with (
            patch("ductor_bot.cli_commands.install._is_installed", return_value=False),
            patch(
                "ductor_bot.cli_commands.install.detect_install_mode",
                return_value="pip",
            ),
            patch("ductor_bot.cli_commands.install.subprocess.run") as mock_run,
            patch("ductor_bot.cli_commands.install.Console", return_value=MagicMock()) as console,
        ):
            mock_run.return_value = MagicMock(returncode=1, stderr="some error")
            _install_extra("matrix")
            call_args = console.return_value.print.call_args_list
            assert any("Installation failed" in str(c) for c in call_args)
