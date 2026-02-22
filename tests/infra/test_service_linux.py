"""Tests for Linux systemd service management."""

from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

from ductor_bot.infra.service_linux import (
    _SERVICE_NAME,
    _generate_service_unit,
    is_service_available,
    is_service_running,
    print_service_status,
    start_service,
    stop_service,
    uninstall_service,
)


def _completed(returncode: int = 0, stdout: str = "", stderr: str = "") -> MagicMock:
    r = MagicMock(spec=subprocess.CompletedProcess)
    r.returncode = returncode
    r.stdout = stdout
    r.stderr = stderr
    return r


class TestGenerateServiceUnit:
    def test_contains_binary_path(self) -> None:
        unit = _generate_service_unit("/usr/local/bin/ductor")
        assert "ExecStart=/usr/local/bin/ductor" in unit

    def test_has_restart_policy(self) -> None:
        unit = _generate_service_unit("ductor")
        assert "Restart=on-failure" in unit

    def test_has_service_section(self) -> None:
        unit = _generate_service_unit("ductor")
        assert "[Service]" in unit
        assert "[Unit]" in unit
        assert "[Install]" in unit


class TestIsServiceAvailable:
    @patch("ductor_bot.infra.service_linux.shutil.which", return_value="/usr/bin/systemctl")
    def test_available_with_systemctl(self, _mock: MagicMock) -> None:
        assert is_service_available() is True

    @patch("ductor_bot.infra.service_linux.shutil.which", return_value=None)
    def test_unavailable_without_systemctl(self, _mock: MagicMock) -> None:
        assert is_service_available() is False


class TestIsServiceRunning:
    @patch("ductor_bot.infra.service_linux._run_systemctl")
    @patch("ductor_bot.infra.service_linux.is_service_installed", return_value=True)
    @patch("ductor_bot.infra.service_linux._has_systemd", return_value=True)
    def test_running(self, _sys: MagicMock, _inst: MagicMock, mock_run: MagicMock) -> None:
        mock_run.return_value = _completed(0, stdout="active")
        assert is_service_running() is True

    @patch("ductor_bot.infra.service_linux._run_systemctl")
    @patch("ductor_bot.infra.service_linux.is_service_installed", return_value=True)
    @patch("ductor_bot.infra.service_linux._has_systemd", return_value=True)
    def test_not_running(self, _sys: MagicMock, _inst: MagicMock, mock_run: MagicMock) -> None:
        mock_run.return_value = _completed(0, stdout="inactive")
        assert is_service_running() is False


class TestStartService:
    @patch("ductor_bot.infra.service_linux._run_systemctl")
    @patch("ductor_bot.infra.service_linux._has_systemd", return_value=False)
    def test_start_without_systemd(self, _has: MagicMock, mock_run: MagicMock) -> None:
        console = MagicMock()
        start_service(console)
        mock_run.assert_not_called()
        console.print.assert_called_with("[dim]systemd not available.[/dim]")

    @patch("ductor_bot.infra.service_linux._run_systemctl")
    @patch("ductor_bot.infra.service_linux.is_service_installed", return_value=False)
    @patch("ductor_bot.infra.service_linux._has_systemd", return_value=True)
    def test_start_not_installed(
        self,
        _has: MagicMock,
        _installed: MagicMock,
        mock_run: MagicMock,
    ) -> None:
        console = MagicMock()
        start_service(console)
        mock_run.assert_not_called()
        console.print.assert_called_with(
            "[dim]Service not installed. Run [bold]ductor service install[/bold].[/dim]"
        )

    @patch("ductor_bot.infra.service_linux._run_systemctl")
    @patch("ductor_bot.infra.service_linux.is_service_installed", return_value=True)
    @patch("ductor_bot.infra.service_linux._has_systemd", return_value=True)
    def test_start_success(
        self,
        _has: MagicMock,
        _installed: MagicMock,
        mock_run: MagicMock,
    ) -> None:
        mock_run.return_value = _completed(0)
        console = MagicMock()
        start_service(console)
        mock_run.assert_called_once_with("start", _SERVICE_NAME)


class TestStopService:
    @patch("ductor_bot.infra.service_linux._run_systemctl")
    @patch("ductor_bot.infra.service_linux.is_service_running", return_value=True)
    def test_stop_success(self, _running: MagicMock, mock_run: MagicMock) -> None:
        mock_run.return_value = _completed(0)
        console = MagicMock()
        stop_service(console)
        mock_run.assert_called_once_with("stop", _SERVICE_NAME)


class TestUninstallService:
    @patch("ductor_bot.infra.service_linux._run_systemctl")
    @patch("ductor_bot.infra.service_linux._has_systemd", return_value=False)
    def test_uninstall_without_systemd(self, _has: MagicMock, mock_run: MagicMock) -> None:
        console = MagicMock()
        assert uninstall_service(console) is False
        mock_run.assert_not_called()
        console.print.assert_called_with("[dim]systemd not available.[/dim]")

    @patch("ductor_bot.infra.service_linux._run_systemctl")
    @patch("ductor_bot.infra.service_linux._service_path")
    @patch("ductor_bot.infra.service_linux.is_service_installed", return_value=True)
    @patch("ductor_bot.infra.service_linux._has_systemd", return_value=True)
    def test_uninstall(
        self,
        _has: MagicMock,
        _inst: MagicMock,
        mock_path: MagicMock,
        mock_run: MagicMock,
    ) -> None:
        mock_path.return_value = MagicMock()
        mock_run.return_value = _completed(0)
        console = MagicMock()
        assert uninstall_service(console) is True

    @patch("ductor_bot.infra.service_linux.is_service_installed", return_value=False)
    @patch("ductor_bot.infra.service_linux._has_systemd", return_value=True)
    def test_uninstall_not_installed(self, _has: MagicMock, _inst: MagicMock) -> None:
        console = MagicMock()
        assert uninstall_service(console) is False


class TestPrintServiceStatus:
    @patch("ductor_bot.infra.service_linux._run_systemctl")
    @patch("ductor_bot.infra.service_linux.is_service_installed", return_value=True)
    @patch("ductor_bot.infra.service_linux._has_systemd", return_value=True)
    def test_prints_status(self, _sys: MagicMock, _inst: MagicMock, mock_run: MagicMock) -> None:
        mock_run.return_value = _completed(0, stdout="active running")
        console = MagicMock()
        print_service_status(console)
        console.print.assert_called_with("active running")
