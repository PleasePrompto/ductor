"""Tests for Linux systemd service management."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock, patch

from ductor_bot.infra.service_linux import (
    _SERVICE_NAME,
    _enable_linger,
    _generate_service_unit,
    is_service_available,
    is_service_running,
    print_service_status,
    start_service,
    stop_service,
    uninstall_service,
)
from tests.infra.conftest import make_completed


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

    def test_includes_all_nvm_bins(self, tmp_path: Path) -> None:
        (tmp_path / ".nvm" / "versions" / "node" / "v24.0.0" / "bin").mkdir(parents=True)
        (tmp_path / ".nvm" / "versions" / "node" / "v22.0.0" / "bin").mkdir(parents=True)

        with patch("ductor_bot.infra.service_linux.Path.home", return_value=tmp_path):
            unit = _generate_service_unit("ductor")

        home = tmp_path.as_posix()
        assert f"{home}/.nvm/versions/node/v24.0.0/bin" in unit
        assert f"{home}/.nvm/versions/node/v22.0.0/bin" in unit


class TestEnableLinger:
    @patch("ductor_bot.infra.service_linux.subprocess.run")
    @patch("ductor_bot.infra.service_linux.os.geteuid", return_value=0)
    def test_root_runs_loginctl_without_sudo(
        self,
        _euid: MagicMock,
        mock_run: MagicMock,
    ) -> None:
        mock_run.return_value = make_completed(0)
        assert _enable_linger("root") is True
        mock_run.assert_called_once_with(
            ["loginctl", "enable-linger", "root"],
            capture_output=True,
            text=True,
            check=False,
        )

    @patch("ductor_bot.infra.service_linux.subprocess.run")
    @patch("ductor_bot.infra.service_linux.shutil.which", return_value="/usr/bin/sudo")
    @patch("ductor_bot.infra.service_linux.os.geteuid", return_value=1000)
    def test_non_root_prefixes_sudo(
        self,
        _euid: MagicMock,
        _which: MagicMock,
        mock_run: MagicMock,
    ) -> None:
        mock_run.return_value = make_completed(0)
        assert _enable_linger("alice") is True
        mock_run.assert_called_once_with(
            ["sudo", "loginctl", "enable-linger", "alice"],
            capture_output=True,
            text=True,
            check=False,
        )

    @patch("ductor_bot.infra.service_linux.subprocess.run")
    @patch("ductor_bot.infra.service_linux.shutil.which", return_value=None)
    @patch("ductor_bot.infra.service_linux.os.geteuid", return_value=1000)
    def test_missing_sudo_degrades_gracefully(
        self,
        _euid: MagicMock,
        _which: MagicMock,
        mock_run: MagicMock,
    ) -> None:
        assert _enable_linger("alice") is False
        mock_run.assert_not_called()

    @patch(
        "ductor_bot.infra.service_linux.subprocess.run",
        side_effect=FileNotFoundError("loginctl"),
    )
    @patch("ductor_bot.infra.service_linux.os.geteuid", return_value=0)
    def test_missing_loginctl_degrades_gracefully(
        self,
        _euid: MagicMock,
        _run: MagicMock,
    ) -> None:
        assert _enable_linger("root") is False

    @patch("ductor_bot.infra.service_linux.subprocess.run")
    @patch("ductor_bot.infra.service_linux.os.geteuid", return_value=0)
    def test_nonzero_exit_reports_failure(
        self,
        _euid: MagicMock,
        mock_run: MagicMock,
    ) -> None:
        mock_run.return_value = make_completed(1, stderr="boom")
        assert _enable_linger("root") is False


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
        mock_run.return_value = make_completed(0, stdout="active")
        assert is_service_running() is True

    @patch("ductor_bot.infra.service_linux._run_systemctl")
    @patch("ductor_bot.infra.service_linux.is_service_installed", return_value=True)
    @patch("ductor_bot.infra.service_linux._has_systemd", return_value=True)
    def test_not_running(self, _sys: MagicMock, _inst: MagicMock, mock_run: MagicMock) -> None:
        mock_run.return_value = make_completed(0, stdout="inactive")
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
        mock_run.return_value = make_completed(0)
        console = MagicMock()
        start_service(console)
        mock_run.assert_called_once_with("start", _SERVICE_NAME)


class TestStopService:
    @patch("ductor_bot.infra.service_linux._run_systemctl")
    @patch("ductor_bot.infra.service_linux.is_service_running", return_value=True)
    def test_stop_success(self, _running: MagicMock, mock_run: MagicMock) -> None:
        mock_run.return_value = make_completed(0)
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
        mock_run.return_value = make_completed(0)
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
        mock_run.return_value = make_completed(0, stdout="active running")
        console = MagicMock()
        print_service_status(console)
        console.print.assert_called_with("active running")
