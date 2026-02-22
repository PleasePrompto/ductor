"""Tests for macOS launchd Launch Agent service management."""

from __future__ import annotations

import plistlib
import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

from ductor_bot.infra.service_macos import (
    _LABEL,
    _generate_plist_data,
    install_service,
    is_service_installed,
    is_service_running,
    print_service_logs,
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


class TestGeneratePlistData:
    def test_contains_binary_path(self) -> None:
        data = _generate_plist_data("/usr/local/bin/ductor")
        assert data["ProgramArguments"] == ["/usr/local/bin/ductor"]

    def test_has_label(self) -> None:
        data = _generate_plist_data("ductor")
        assert data["Label"] == "dev.ductor"

    def test_has_run_at_load(self) -> None:
        data = _generate_plist_data("ductor")
        assert data["RunAtLoad"] is True

    def test_keep_alive_only_on_crash(self) -> None:
        data = _generate_plist_data("ductor")
        assert data["KeepAlive"] == {"SuccessfulExit": False}

    def test_has_throttle_interval(self) -> None:
        data = _generate_plist_data("ductor")
        assert data["ThrottleInterval"] == 10

    def test_has_background_process_type(self) -> None:
        data = _generate_plist_data("ductor")
        assert data["ProcessType"] == "Background"

    def test_has_environment_variables(self) -> None:
        data = _generate_plist_data("ductor")
        env = data["EnvironmentVariables"]
        assert "PATH" in env
        assert "HOME" in env

    def test_path_includes_homebrew_dirs(self) -> None:
        data = _generate_plist_data("ductor")
        path_value = data["EnvironmentVariables"]["PATH"]
        assert "/opt/homebrew/bin" in path_value
        assert "/usr/local/bin" in path_value

    def test_has_log_paths(self) -> None:
        data = _generate_plist_data("ductor")
        assert "StandardOutPath" in data
        assert "StandardErrorPath" in data
        assert "service.log" in data["StandardOutPath"]
        assert "service.err" in data["StandardErrorPath"]

    def test_generates_valid_plist(self) -> None:
        data = _generate_plist_data("ductor")
        plist_bytes = plistlib.dumps(data, fmt=plistlib.FMT_XML)
        parsed = plistlib.loads(plist_bytes)
        assert parsed["Label"] == "dev.ductor"
        assert parsed["RunAtLoad"] is True


class TestIsServiceInstalled:
    @patch("ductor_bot.infra.service_macos._plist_path")
    def test_installed_when_plist_exists(self, mock_path: MagicMock) -> None:
        mock_path.return_value = MagicMock(exists=MagicMock(return_value=True))
        assert is_service_installed() is True

    @patch("ductor_bot.infra.service_macos._plist_path")
    def test_not_installed_when_plist_missing(self, mock_path: MagicMock) -> None:
        mock_path.return_value = MagicMock(exists=MagicMock(return_value=False))
        assert is_service_installed() is False


class TestIsServiceRunning:
    @patch("ductor_bot.infra.service_macos.is_service_installed", return_value=False)
    def test_not_running_when_not_installed(self, _mock: MagicMock) -> None:
        assert is_service_running() is False

    @patch("ductor_bot.infra.service_macos._run_launchctl")
    @patch("ductor_bot.infra.service_macos.is_service_installed", return_value=True)
    def test_running_when_pid_present(self, _installed: MagicMock, mock_run: MagicMock) -> None:
        mock_run.return_value = _completed(
            0,
            stdout='{\n\t"PID" = 12345;\n\t"Label" = "dev.ductor";\n};',
        )
        assert is_service_running() is True

    @patch("ductor_bot.infra.service_macos._run_launchctl")
    @patch("ductor_bot.infra.service_macos.is_service_installed", return_value=True)
    def test_not_running_when_no_pid(self, _installed: MagicMock, mock_run: MagicMock) -> None:
        mock_run.return_value = _completed(
            0,
            stdout='{\n\t"Label" = "dev.ductor";\n\t"LastExitStatus" = 0;\n};',
        )
        assert is_service_running() is False

    @patch("ductor_bot.infra.service_macos._run_launchctl")
    @patch("ductor_bot.infra.service_macos.is_service_installed", return_value=True)
    def test_not_running_when_launchctl_fails(
        self, _installed: MagicMock, mock_run: MagicMock
    ) -> None:
        mock_run.return_value = _completed(1, stderr="Could not find service")
        assert is_service_running() is False


class TestInstallService:
    @patch("ductor_bot.infra.service_macos._run_launchctl")
    @patch("ductor_bot.infra.service_macos.is_service_installed", return_value=False)
    @patch("ductor_bot.infra.service_macos.is_service_available", return_value=True)
    @patch(
        "ductor_bot.infra.service_macos._find_ductor_binary", return_value="/usr/local/bin/ductor"
    )
    @patch("ductor_bot.infra.service_macos._plist_path")
    @patch("ductor_bot.infra.service_macos.resolve_paths")
    def test_install_success(
        self,
        mock_paths: MagicMock,
        mock_plist_path: MagicMock,
        _binary: MagicMock,
        _avail: MagicMock,
        _installed: MagicMock,
        mock_run: MagicMock,
        tmp_path: Path,
    ) -> None:
        plist_file = tmp_path / "dev.ductor.plist"
        mock_plist_path.return_value = plist_file
        paths_obj = MagicMock()
        paths_obj.logs_dir = tmp_path / "logs"
        mock_paths.return_value = paths_obj
        mock_run.return_value = _completed(0)

        console = MagicMock()
        assert install_service(console) is True
        assert plist_file.exists()

        # Verify the plist is valid
        plist_data = plistlib.loads(plist_file.read_bytes())
        assert plist_data["Label"] == "dev.ductor"

    @patch("ductor_bot.infra.service_macos.is_service_available", return_value=False)
    def test_install_fails_without_launchctl(self, _avail: MagicMock) -> None:
        console = MagicMock()
        assert install_service(console) is False

    @patch("ductor_bot.infra.service_macos.is_service_available", return_value=True)
    @patch("ductor_bot.infra.service_macos._find_ductor_binary", return_value=None)
    def test_install_fails_without_binary(self, _binary: MagicMock, _avail: MagicMock) -> None:
        console = MagicMock()
        assert install_service(console) is False


class TestUninstallService:
    @patch("ductor_bot.infra.service_macos._run_launchctl")
    @patch("ductor_bot.infra.service_macos._plist_path")
    @patch("ductor_bot.infra.service_macos.is_service_installed", return_value=True)
    def test_uninstall_success(
        self, _installed: MagicMock, mock_path: MagicMock, mock_run: MagicMock
    ) -> None:
        mock_path.return_value = MagicMock()
        mock_run.return_value = _completed(0)
        console = MagicMock()
        assert uninstall_service(console) is True

    @patch("ductor_bot.infra.service_macos.is_service_installed", return_value=False)
    def test_uninstall_when_not_installed(self, _installed: MagicMock) -> None:
        console = MagicMock()
        assert uninstall_service(console) is False


class TestStartService:
    @patch("ductor_bot.infra.service_macos._run_launchctl")
    @patch("ductor_bot.infra.service_macos.is_service_installed", return_value=True)
    def test_start_success(self, _installed: MagicMock, mock_run: MagicMock) -> None:
        mock_run.return_value = _completed(0)
        console = MagicMock()
        start_service(console)
        mock_run.assert_called_once_with("start", _LABEL)

    @patch("ductor_bot.infra.service_macos.is_service_installed", return_value=False)
    def test_start_not_installed(self, _installed: MagicMock) -> None:
        console = MagicMock()
        start_service(console)
        console.print.assert_called_once()


class TestStopService:
    @patch("ductor_bot.infra.service_macos._run_launchctl")
    @patch("ductor_bot.infra.service_macos.is_service_running", return_value=True)
    def test_stop_success(self, _running: MagicMock, mock_run: MagicMock) -> None:
        mock_run.return_value = _completed(0)
        console = MagicMock()
        stop_service(console)
        mock_run.assert_called_once_with("stop", _LABEL)

    @patch("ductor_bot.infra.service_macos.is_service_running", return_value=False)
    def test_stop_not_running(self, _running: MagicMock) -> None:
        console = MagicMock()
        stop_service(console)
        console.print.assert_called_once()


class TestPrintServiceStatus:
    @patch("ductor_bot.infra.service_macos._run_launchctl")
    @patch("ductor_bot.infra.service_macos.is_service_installed", return_value=True)
    def test_prints_status(self, _installed: MagicMock, mock_run: MagicMock) -> None:
        mock_run.return_value = _completed(0, stdout="Agent details here")
        console = MagicMock()
        print_service_status(console)
        console.print.assert_called_with("Agent details here")


class TestPrintServiceLogs:
    @patch("ductor_bot.infra.service_macos.is_service_installed", return_value=False)
    def test_not_installed(self, _installed: MagicMock) -> None:
        console = MagicMock()
        print_service_logs(console)
        console.print.assert_called_once()

    @patch("ductor_bot.infra.service_macos.resolve_paths")
    @patch("ductor_bot.infra.service_macos.is_service_installed", return_value=True)
    def test_shows_logs_from_file(
        self, _installed: MagicMock, mock_paths: MagicMock, tmp_path: Path
    ) -> None:
        logs_dir = tmp_path / "logs"
        logs_dir.mkdir()
        log_file = logs_dir / "ductor_2026-02-22.log"
        log_file.write_text("line1\nline2\nline3\n", encoding="utf-8")

        paths_obj = MagicMock()
        paths_obj.logs_dir = logs_dir
        mock_paths.return_value = paths_obj

        console = MagicMock()
        print_service_logs(console)
        # header + 3 log lines + footer = 5 calls
        assert console.print.call_count == 5
