"""Tests for __main__.py entry point."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ductor_bot.config import AgentConfig
from ductor_bot.workspace.paths import DuctorPaths


class TestLoadConfig:
    """Test config loading, creation, and smart-merge."""

    def test_creates_config_from_example(self, tmp_path: Path) -> None:
        from ductor_bot.__main__ import load_config

        home = tmp_path / ".ductor"
        fw = tmp_path / "framework"
        fw.mkdir()
        # Create config.example.json
        example = {"telegram_token": "TEST", "provider": "claude"}
        (fw / "config.example.json").write_text(json.dumps(example))

        with patch("ductor_bot.__main__.resolve_paths") as mock_paths:
            from ductor_bot.workspace.paths import DuctorPaths

            paths = DuctorPaths(ductor_home=home, home_defaults=fw / "workspace", framework_root=fw)
            mock_paths.return_value = paths
            with patch("ductor_bot.__main__.init_workspace"):
                config = load_config()

        assert config.telegram_token == "TEST"
        assert paths.config_path.exists()

    def test_preserves_existing_user_config(self, tmp_path: Path) -> None:
        from ductor_bot.__main__ import load_config

        home = tmp_path / ".ductor"
        config_dir = home / "config"
        config_dir.mkdir(parents=True)
        fw = tmp_path / "framework"
        fw.mkdir()

        user_cfg = {"telegram_token": "MY_TOKEN", "provider": "codex", "model": "gpt-5.2-codex"}
        (config_dir / "config.json").write_text(json.dumps(user_cfg))

        with patch("ductor_bot.__main__.resolve_paths") as mock_paths:
            from ductor_bot.workspace.paths import DuctorPaths

            paths = DuctorPaths(ductor_home=home, home_defaults=fw / "workspace", framework_root=fw)
            mock_paths.return_value = paths
            with patch("ductor_bot.__main__.init_workspace"):
                config = load_config()

        assert config.telegram_token == "MY_TOKEN"
        assert config.provider == "codex"

    def test_merges_new_defaults_into_existing(self, tmp_path: Path) -> None:
        from ductor_bot.__main__ import load_config

        home = tmp_path / ".ductor"
        config_dir = home / "config"
        config_dir.mkdir(parents=True)
        fw = tmp_path / "framework"
        fw.mkdir()

        # Old config missing streaming key
        old_cfg = {"telegram_token": "TOKEN", "provider": "claude"}
        (config_dir / "config.json").write_text(json.dumps(old_cfg))

        with patch("ductor_bot.__main__.resolve_paths") as mock_paths:
            from ductor_bot.workspace.paths import DuctorPaths

            paths = DuctorPaths(ductor_home=home, home_defaults=fw / "workspace", framework_root=fw)
            mock_paths.return_value = paths
            with patch("ductor_bot.__main__.init_workspace"):
                config = load_config()

        # Should have picked up default streaming config
        assert config.streaming.enabled is True

    def test_creates_default_config_when_no_example(self, tmp_path: Path) -> None:
        from ductor_bot.__main__ import load_config

        home = tmp_path / ".ductor"
        fw = tmp_path / "framework"
        fw.mkdir()
        # No config.example.json

        with patch("ductor_bot.__main__.resolve_paths") as mock_paths:
            from ductor_bot.workspace.paths import DuctorPaths

            paths = DuctorPaths(ductor_home=home, home_defaults=fw / "workspace", framework_root=fw)
            mock_paths.return_value = paths
            with patch("ductor_bot.__main__.init_workspace"):
                config = load_config()

        assert paths.config_path.exists()
        assert config.provider == "claude"


class TestIsConfigured:
    """Test configuration detection."""

    def test_unconfigured_when_no_config(self, tmp_path: Path) -> None:
        from ductor_bot.__main__ import _is_configured

        with patch("ductor_bot.__main__.resolve_paths") as mock_paths:
            from ductor_bot.workspace.paths import DuctorPaths

            paths = DuctorPaths(
                ductor_home=tmp_path / "home",
                home_defaults=tmp_path / "fw" / "workspace",
                framework_root=tmp_path / "fw",
            )
            mock_paths.return_value = paths
            assert _is_configured() is False

    def test_configured_with_valid_token(self, tmp_path: Path) -> None:
        from ductor_bot.__main__ import _is_configured

        home = tmp_path / "home"
        config_dir = home / "config"
        config_dir.mkdir(parents=True)
        cfg = {"telegram_token": "123456:ABC", "allowed_user_ids": [1]}
        (config_dir / "config.json").write_text(json.dumps(cfg))

        with patch("ductor_bot.__main__.resolve_paths") as mock_paths:
            from ductor_bot.workspace.paths import DuctorPaths

            paths = DuctorPaths(
                ductor_home=home,
                home_defaults=tmp_path / "fw" / "workspace",
                framework_root=tmp_path / "fw",
            )
            mock_paths.return_value = paths
            assert _is_configured() is True


class TestRunTelegram:
    """Test the run_telegram entry point."""

    async def test_exits_on_missing_token(self, tmp_path: Path) -> None:
        from ductor_bot.__main__ import run_telegram

        config = AgentConfig(telegram_token="", ductor_home=str(tmp_path))
        with pytest.raises(SystemExit):
            await run_telegram(config)

    async def test_exits_on_placeholder_token(self, tmp_path: Path) -> None:
        from ductor_bot.__main__ import run_telegram

        config = AgentConfig(telegram_token="YOUR_TOKEN_HERE", ductor_home=str(tmp_path))
        with pytest.raises(SystemExit):
            await run_telegram(config)

    async def test_exits_on_empty_allowed_users(self, tmp_path: Path) -> None:
        from ductor_bot.__main__ import run_telegram

        config = AgentConfig(
            telegram_token="valid:token",
            allowed_user_ids=[],
            ductor_home=str(tmp_path),
        )
        with pytest.raises(SystemExit):
            await run_telegram(config)

    async def test_runs_bot_with_valid_config(self, tmp_path: Path) -> None:
        from ductor_bot.__main__ import run_telegram

        config = AgentConfig(
            telegram_token="valid:token",
            allowed_user_ids=[123],
            ductor_home=str(tmp_path),
        )
        mock_bot = MagicMock()
        mock_bot.run = AsyncMock(return_value=0)
        mock_bot.shutdown = AsyncMock()

        with (
            patch("ductor_bot.__main__.resolve_paths"),
            patch("ductor_bot.infra.pidlock.acquire_lock"),
            patch("ductor_bot.infra.pidlock.release_lock"),
            patch("ductor_bot.bot.app.TelegramBot", return_value=mock_bot),
        ):
            await run_telegram(config)

        mock_bot.run.assert_called_once()
        mock_bot.shutdown.assert_called_once()


def _make_paths(tmp_path: Path) -> DuctorPaths:
    """Helper to create test DuctorPaths."""
    home = tmp_path / "home"
    fw = tmp_path / "fw"
    fw.mkdir(parents=True, exist_ok=True)
    return DuctorPaths(ductor_home=home, home_defaults=fw / "workspace", framework_root=fw)


def _write_config(paths: DuctorPaths, data: dict[str, object]) -> None:
    """Write a config.json for testing."""
    paths.config_path.parent.mkdir(parents=True, exist_ok=True)
    paths.config_path.write_text(json.dumps(data), encoding="utf-8")


class TestIsConfiguredExtended:
    """Extended configuration detection tests."""

    def test_unconfigured_with_placeholder_token(self, tmp_path: Path) -> None:
        from ductor_bot.__main__ import _is_configured

        paths = _make_paths(tmp_path)
        _write_config(paths, {"telegram_token": "YOUR_TOKEN", "allowed_user_ids": [1]})

        with patch("ductor_bot.__main__.resolve_paths", return_value=paths):
            assert _is_configured() is False

    def test_unconfigured_with_empty_users(self, tmp_path: Path) -> None:
        from ductor_bot.__main__ import _is_configured

        paths = _make_paths(tmp_path)
        _write_config(paths, {"telegram_token": "123:ABC", "allowed_user_ids": []})

        with patch("ductor_bot.__main__.resolve_paths", return_value=paths):
            assert _is_configured() is False

    def test_unconfigured_with_corrupt_json(self, tmp_path: Path) -> None:
        from ductor_bot.__main__ import _is_configured

        paths = _make_paths(tmp_path)
        paths.config_path.parent.mkdir(parents=True)
        paths.config_path.write_text("{invalid json", encoding="utf-8")

        with patch("ductor_bot.__main__.resolve_paths", return_value=paths):
            assert _is_configured() is False


class TestStopBot:
    """Test bot stop logic."""

    def test_stop_kills_running_process(self, tmp_path: Path) -> None:
        from ductor_bot.__main__ import _stop_bot

        paths = _make_paths(tmp_path)
        paths.ductor_home.mkdir(parents=True)
        pid_file = paths.ductor_home / "bot.pid"
        pid_file.write_text("12345", encoding="utf-8")

        with (
            patch("ductor_bot.__main__.resolve_paths", return_value=paths),
            patch("ductor_bot.infra.pidlock._is_process_alive", return_value=True),
            patch("ductor_bot.infra.pidlock._kill_and_wait") as mock_kill,
        ):
            _stop_bot()

        mock_kill.assert_called_once_with(12345)

    def test_stop_no_running_instance(self, tmp_path: Path) -> None:
        from ductor_bot.__main__ import _stop_bot

        paths = _make_paths(tmp_path)
        paths.ductor_home.mkdir(parents=True)

        with patch("ductor_bot.__main__.resolve_paths", return_value=paths):
            _stop_bot()  # Should not raise

    def test_stop_with_docker(self, tmp_path: Path) -> None:
        from ductor_bot.__main__ import _stop_bot

        paths = _make_paths(tmp_path)
        paths.ductor_home.mkdir(parents=True)
        _write_config(
            paths,
            {
                "docker": {"enabled": True, "container_name": "test-container"},
                "telegram_token": "x",
                "allowed_user_ids": [1],
            },
        )

        with (
            patch("ductor_bot.__main__.resolve_paths", return_value=paths),
            patch("ductor_bot.__main__.shutil.which", return_value="/usr/bin/docker"),
            patch("ductor_bot.__main__.subprocess.run") as mock_run,
        ):
            _stop_bot()

        # Should attempt docker stop and rm
        docker_calls = [c for c in mock_run.call_args_list if "docker" in str(c)]
        assert len(docker_calls) >= 2


class TestUpgradeCli:
    """Test CLI upgrade command."""

    def test_upgrade_with_pipx(self, tmp_path: Path) -> None:
        from ductor_bot.__main__ import _upgrade

        paths = _make_paths(tmp_path)
        paths.ductor_home.mkdir(parents=True)

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "upgraded ductor 1.0.0 -> 2.0.0"
        mock_result.stderr = ""

        with (
            patch("ductor_bot.infra.install.detect_install_mode", return_value="pipx"),
            patch("ductor_bot.__main__.resolve_paths", return_value=paths),
            patch("ductor_bot.__main__.subprocess.run", return_value=mock_result) as mock_run,
            patch("ductor_bot.__main__._re_exec_bot") as mock_exec,
            patch("ductor_bot.__main__._stop_bot"),
        ):
            _upgrade()

        # Check pipx upgrade was called
        run_args = mock_run.call_args[0][0]
        assert "pipx" in run_args
        assert "upgrade" in run_args
        mock_exec.assert_called_once()

    def test_upgrade_with_pip(self, tmp_path: Path) -> None:
        from ductor_bot.__main__ import _upgrade

        paths = _make_paths(tmp_path)
        paths.ductor_home.mkdir(parents=True)

        mock_result = MagicMock()
        mock_result.returncode = 0
        mock_result.stdout = "installed ductor-2.0.0"
        mock_result.stderr = ""

        with (
            patch("ductor_bot.infra.install.detect_install_mode", return_value="pip"),
            patch("ductor_bot.__main__.resolve_paths", return_value=paths),
            patch("ductor_bot.__main__.subprocess.run", return_value=mock_result) as mock_run,
            patch("ductor_bot.__main__._re_exec_bot") as mock_exec,
            patch("ductor_bot.__main__._stop_bot"),
        ):
            _upgrade()

        run_args = mock_run.call_args[0][0]
        assert "pip" in " ".join(str(a) for a in run_args)
        mock_exec.assert_called_once()

    def test_upgrade_fails_no_restart(self, tmp_path: Path) -> None:
        from ductor_bot.__main__ import _upgrade

        paths = _make_paths(tmp_path)
        paths.ductor_home.mkdir(parents=True)

        mock_result = MagicMock()
        mock_result.returncode = 1
        mock_result.stdout = ""
        mock_result.stderr = "error: package not found"

        with (
            patch("ductor_bot.infra.install.detect_install_mode", return_value="pip"),
            patch("ductor_bot.__main__.resolve_paths", return_value=paths),
            patch("ductor_bot.__main__.subprocess.run", return_value=mock_result),
            patch("ductor_bot.__main__._re_exec_bot") as mock_exec,
            patch("ductor_bot.__main__._stop_bot"),
        ):
            _upgrade()

        # Should NOT restart on failure
        mock_exec.assert_not_called()

    def test_upgrade_rejects_dev_mode(self) -> None:
        from ductor_bot.__main__ import _upgrade

        with (
            patch("ductor_bot.infra.install.detect_install_mode", return_value="dev"),
            patch("ductor_bot.__main__._stop_bot") as mock_stop,
            patch("ductor_bot.__main__._re_exec_bot") as mock_exec,
        ):
            _upgrade()

        # Should NOT stop or restart -- just show message
        mock_stop.assert_not_called()
        mock_exec.assert_not_called()


class TestReExecBot:
    """Test cross-platform re-exec helper."""

    def test_posix_uses_execv(self) -> None:
        from ductor_bot.__main__ import _re_exec_bot

        with (
            patch("ductor_bot.__main__._IS_WINDOWS", False),
            patch("ductor_bot.__main__.os.execv") as mock_execv,
        ):
            _re_exec_bot()

        mock_execv.assert_called_once()

    def test_windows_uses_popen_and_exit(self) -> None:
        from ductor_bot.__main__ import _re_exec_bot

        with (
            patch("ductor_bot.__main__._IS_WINDOWS", True),
            patch("ductor_bot.__main__.subprocess.Popen") as mock_popen,
            pytest.raises(SystemExit) as exc_info,
        ):
            _re_exec_bot()

        mock_popen.assert_called_once()
        assert exc_info.value.code == 0


def _mock_asyncio_run(return_value: int):
    """Create a side_effect for asyncio.run that closes the coroutine to avoid warnings."""

    def _side_effect(coro):
        coro.close()
        return return_value

    return _side_effect


class TestStartBotRestart:
    """Test supervisor-aware restart in _start_bot."""

    def _mock_config(self) -> AgentConfig:
        return AgentConfig(telegram_token="test:token", allowed_user_ids=[1])

    def test_exit42_with_supervisor_exits(self) -> None:
        from ductor_bot.__main__ import _start_bot

        with (
            patch("ductor_bot.__main__.resolve_paths"),
            patch("ductor_bot.__main__.setup_logging"),
            patch("ductor_bot.__main__.load_config", return_value=self._mock_config()),
            patch("ductor_bot.__main__.asyncio.run", side_effect=_mock_asyncio_run(42)),
            patch.dict("os.environ", {"DUCTOR_SUPERVISOR": "1"}),
            pytest.raises(SystemExit) as exc_info,
        ):
            _start_bot()

        assert exc_info.value.code == 42

    def test_exit42_without_supervisor_re_execs(self) -> None:
        from ductor_bot.__main__ import _start_bot

        with (
            patch("ductor_bot.__main__.resolve_paths"),
            patch("ductor_bot.__main__.setup_logging"),
            patch("ductor_bot.__main__.load_config", return_value=self._mock_config()),
            patch("ductor_bot.__main__.asyncio.run", side_effect=_mock_asyncio_run(42)),
            patch.dict("os.environ", {}, clear=True),
            patch("ductor_bot.__main__._re_exec_bot") as mock_exec,
        ):
            _start_bot()

        mock_exec.assert_called_once()

    def test_exit0_does_nothing(self) -> None:
        from ductor_bot.__main__ import _start_bot

        with (
            patch("ductor_bot.__main__.resolve_paths"),
            patch("ductor_bot.__main__.setup_logging"),
            patch("ductor_bot.__main__.load_config", return_value=self._mock_config()),
            patch("ductor_bot.__main__.asyncio.run", side_effect=_mock_asyncio_run(0)),
            patch("ductor_bot.__main__._re_exec_bot") as mock_exec,
        ):
            _start_bot()

        mock_exec.assert_not_called()

    def test_nonzero_non42_exits(self) -> None:
        from ductor_bot.__main__ import _start_bot

        with (
            patch("ductor_bot.__main__.resolve_paths"),
            patch("ductor_bot.__main__.setup_logging"),
            patch("ductor_bot.__main__.load_config", return_value=self._mock_config()),
            patch("ductor_bot.__main__.asyncio.run", side_effect=_mock_asyncio_run(1)),
            pytest.raises(SystemExit) as exc_info,
        ):
            _start_bot()

        assert exc_info.value.code == 1


class TestCountLogErrors:
    """Test log error counting."""

    def test_counts_errors_in_log(self, tmp_path: Path) -> None:
        from ductor_bot.__main__ import _count_log_errors

        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        log_file = log_dir / "ductor.log"
        log_file.write_text(
            "2024-01-01 INFO Started\n"
            "2024-01-01 ERROR Something broke\n"
            "2024-01-01 INFO Continued\n"
            "2024-01-01 ERROR Another error\n",
            encoding="utf-8",
        )
        assert _count_log_errors(log_dir) == 2

    def test_returns_zero_no_dir(self, tmp_path: Path) -> None:
        from ductor_bot.__main__ import _count_log_errors

        assert _count_log_errors(tmp_path / "nonexistent") == 0

    def test_returns_zero_no_log_files(self, tmp_path: Path) -> None:
        from ductor_bot.__main__ import _count_log_errors

        log_dir = tmp_path / "logs"
        log_dir.mkdir()
        assert _count_log_errors(log_dir) == 0


class TestUninstall:
    """Test uninstall flow."""

    def test_uninstall_removes_workspace(self, tmp_path: Path) -> None:
        from ductor_bot.__main__ import _uninstall

        paths = _make_paths(tmp_path)
        paths.ductor_home.mkdir(parents=True)
        _write_config(paths, {"telegram_token": "x", "allowed_user_ids": [1]})

        with (
            patch("ductor_bot.__main__.resolve_paths", return_value=paths),
            patch("questionary.confirm") as mock_confirm,
            patch("ductor_bot.__main__._stop_bot"),
            patch("ductor_bot.__main__.shutil.which", return_value=None),
            patch("ductor_bot.__main__.subprocess.run"),
        ):
            mock_confirm.return_value.ask.return_value = True
            _uninstall()

        assert not paths.ductor_home.exists()

    def test_uninstall_cancelled(self, tmp_path: Path) -> None:
        from ductor_bot.__main__ import _uninstall

        paths = _make_paths(tmp_path)
        paths.ductor_home.mkdir(parents=True)
        _write_config(paths, {"telegram_token": "x", "allowed_user_ids": [1]})

        with (
            patch("ductor_bot.__main__.resolve_paths", return_value=paths),
            patch("questionary.confirm") as mock_confirm,
        ):
            mock_confirm.return_value.ask.return_value = False
            _uninstall()

        # Workspace should NOT be deleted
        assert paths.ductor_home.exists()


class TestMainDispatch:
    """Test CLI command dispatch."""

    def test_help_command(self) -> None:
        from ductor_bot.__main__ import main

        with (
            patch("sys.argv", ["ductor", "help"]),
            patch("ductor_bot.__main__._print_usage") as mock_usage,
        ):
            main()

        mock_usage.assert_called_once()

    def test_status_command(self) -> None:
        from ductor_bot.__main__ import main

        with (
            patch("sys.argv", ["ductor", "status"]),
            patch("ductor_bot.__main__._cmd_status") as mock_status,
        ):
            main()

        mock_status.assert_called_once()

    def test_stop_command(self) -> None:
        from ductor_bot.__main__ import main

        with (
            patch("sys.argv", ["ductor", "stop"]),
            patch("ductor_bot.__main__._stop_bot") as mock_stop,
        ):
            main()

        mock_stop.assert_called_once()

    def test_default_starts_bot_when_configured(self) -> None:
        from ductor_bot.__main__ import main

        with (
            patch("sys.argv", ["ductor"]),
            patch("ductor_bot.__main__._is_configured", return_value=True),
            patch("ductor_bot.__main__._start_bot") as mock_start,
        ):
            main()

        mock_start.assert_called_once_with(False)

    def test_default_runs_onboarding_when_unconfigured(self) -> None:
        from ductor_bot.__main__ import main

        with (
            patch("sys.argv", ["ductor"]),
            patch("ductor_bot.__main__._is_configured", return_value=False),
            patch("ductor_bot.cli.init_wizard.run_onboarding") as mock_onboard,
            patch("ductor_bot.__main__._start_bot"),
        ):
            main()

        mock_onboard.assert_called_once()

    def test_verbose_flag_passed(self) -> None:
        from ductor_bot.__main__ import main

        with (
            patch("sys.argv", ["ductor", "-v"]),
            patch("ductor_bot.__main__._is_configured", return_value=True),
            patch("ductor_bot.__main__._start_bot") as mock_start,
        ):
            main()

        mock_start.assert_called_once_with(True)

    def test_dash_h_maps_to_help(self) -> None:
        from ductor_bot.__main__ import main

        with (
            patch("sys.argv", ["ductor", "-h"]),
            patch("ductor_bot.__main__._print_usage") as mock_usage,
        ):
            main()

        mock_usage.assert_called_once()

    def test_upgrade_command(self) -> None:
        from ductor_bot.__main__ import main

        with (
            patch("sys.argv", ["ductor", "upgrade"]),
            patch("ductor_bot.__main__._upgrade") as mock_upgrade,
        ):
            main()

        mock_upgrade.assert_called_once()

    def test_onboarding_command(self) -> None:
        from ductor_bot.__main__ import main

        with (
            patch("sys.argv", ["ductor", "onboarding"]),
            patch("ductor_bot.__main__._cmd_setup") as mock_setup,
        ):
            main()

        mock_setup.assert_called_once()

    def test_reset_maps_to_setup(self) -> None:
        from ductor_bot.__main__ import main

        with (
            patch("sys.argv", ["ductor", "reset"]),
            patch("ductor_bot.__main__._cmd_setup") as mock_setup,
        ):
            main()

        mock_setup.assert_called_once()
