"""Tests for update observer, upgrade execution, and sentinel lifecycle."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock, patch

from ductor_bot.infra.updater import (
    UpdateObserver,
    consume_upgrade_sentinel,
    perform_upgrade,
    write_upgrade_sentinel,
)
from ductor_bot.infra.version import VersionInfo

# ---------------------------------------------------------------------------
# Upgrade Sentinel
# ---------------------------------------------------------------------------


class TestUpgradeSentinel:
    """Test sentinel write/read/delete lifecycle."""

    def test_write_and_consume(self, tmp_path: Path) -> None:
        write_upgrade_sentinel(tmp_path, chat_id=42, old_version="1.0.0", new_version="2.0.0")
        sentinel_file = tmp_path / "upgrade-sentinel.json"
        assert sentinel_file.exists()

        data = consume_upgrade_sentinel(tmp_path)
        assert data is not None
        assert data["chat_id"] == 42
        assert data["old_version"] == "1.0.0"
        assert data["new_version"] == "2.0.0"

        # File should be deleted after consumption
        assert not sentinel_file.exists()

    def test_consume_returns_none_when_absent(self, tmp_path: Path) -> None:
        assert consume_upgrade_sentinel(tmp_path) is None

    def test_consume_deletes_corrupt_file(self, tmp_path: Path) -> None:
        sentinel = tmp_path / "upgrade-sentinel.json"
        sentinel.write_text("not valid json{{{", encoding="utf-8")

        result = consume_upgrade_sentinel(tmp_path)
        assert result is None
        assert not sentinel.exists()

    def test_write_creates_directory(self, tmp_path: Path) -> None:
        nested = tmp_path / "deep" / "dir"
        write_upgrade_sentinel(nested, chat_id=1, old_version="0.1", new_version="0.2")
        assert (nested / "upgrade-sentinel.json").exists()

    def test_double_consume_returns_none(self, tmp_path: Path) -> None:
        write_upgrade_sentinel(tmp_path, chat_id=1, old_version="1.0", new_version="2.0")
        first = consume_upgrade_sentinel(tmp_path)
        second = consume_upgrade_sentinel(tmp_path)
        assert first is not None
        assert second is None

    def test_sentinel_content_is_valid_json(self, tmp_path: Path) -> None:
        write_upgrade_sentinel(tmp_path, chat_id=99, old_version="1.0.0", new_version="1.1.0")
        raw = (tmp_path / "upgrade-sentinel.json").read_text(encoding="utf-8")
        data = json.loads(raw)
        assert data == {"chat_id": 99, "old_version": "1.0.0", "new_version": "1.1.0"}


# ---------------------------------------------------------------------------
# perform_upgrade
# ---------------------------------------------------------------------------


class TestPerformUpgrade:
    """Test upgrade execution with mocked subprocess."""

    async def test_uses_pipx_when_detected(self) -> None:
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"Upgraded!", None))
        mock_proc.returncode = 0

        with (
            patch("ductor_bot.infra.install.detect_install_mode", return_value="pipx"),
            patch(
                "ductor_bot.infra.updater.asyncio.create_subprocess_exec",
                return_value=mock_proc,
            ) as mock_exec,
        ):
            success, output = await perform_upgrade()

        assert success is True
        assert output == "Upgraded!"
        args = mock_exec.call_args[0]
        assert "pipx" in args
        assert "upgrade" in args

    async def test_uses_pip_when_detected(self) -> None:
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"OK", None))
        mock_proc.returncode = 0

        with (
            patch("ductor_bot.infra.install.detect_install_mode", return_value="pip"),
            patch(
                "ductor_bot.infra.updater.asyncio.create_subprocess_exec",
                return_value=mock_proc,
            ) as mock_exec,
        ):
            success, _output = await perform_upgrade()

        assert success is True
        args = mock_exec.call_args[0]
        assert "pip" in " ".join(str(a) for a in args)
        assert "install" in args

    async def test_returns_failure_on_nonzero_exit(self) -> None:
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(b"Error: something broke", None))
        mock_proc.returncode = 1

        with (
            patch("ductor_bot.infra.install.detect_install_mode", return_value="pip"),
            patch(
                "ductor_bot.infra.updater.asyncio.create_subprocess_exec",
                return_value=mock_proc,
            ),
        ):
            success, output = await perform_upgrade()

        assert success is False
        assert "something broke" in output

    async def test_handles_empty_stdout(self) -> None:
        mock_proc = AsyncMock()
        mock_proc.communicate = AsyncMock(return_value=(None, None))
        mock_proc.returncode = 0

        with (
            patch("ductor_bot.infra.install.detect_install_mode", return_value="pip"),
            patch(
                "ductor_bot.infra.updater.asyncio.create_subprocess_exec",
                return_value=mock_proc,
            ),
        ):
            success, output = await perform_upgrade()

        assert success is True
        assert output == ""

    async def test_rejects_dev_mode(self) -> None:
        with patch("ductor_bot.infra.install.detect_install_mode", return_value="dev"):
            success, output = await perform_upgrade()

        assert success is False
        assert "git pull" in output.lower()


# ---------------------------------------------------------------------------
# UpdateObserver
# ---------------------------------------------------------------------------


class TestUpdateObserver:
    """Test background version check observer."""

    async def test_notifies_on_new_version(self) -> None:
        info = VersionInfo(current="1.0.0", latest="2.0.0", update_available=True, summary="New!")
        notify = AsyncMock()
        observer = UpdateObserver(notify=notify)

        with (
            patch("ductor_bot.infra.updater.check_pypi", return_value=info),
            patch("ductor_bot.infra.updater._INITIAL_DELAY_S", 0),
            patch("ductor_bot.infra.updater._CHECK_INTERVAL_S", 0.01),
        ):
            observer.start()
            await asyncio.sleep(0.1)
            await observer.stop()

        notify.assert_called_once_with(info)

    async def test_does_not_notify_when_up_to_date(self) -> None:
        info = VersionInfo(current="1.0.0", latest="1.0.0", update_available=False, summary="")
        notify = AsyncMock()
        observer = UpdateObserver(notify=notify)

        with (
            patch("ductor_bot.infra.updater.check_pypi", return_value=info),
            patch("ductor_bot.infra.updater._INITIAL_DELAY_S", 0),
            patch("ductor_bot.infra.updater._CHECK_INTERVAL_S", 0.01),
        ):
            observer.start()
            await asyncio.sleep(0.1)
            await observer.stop()

        notify.assert_not_called()

    async def test_deduplicates_same_version(self) -> None:
        info = VersionInfo(current="1.0.0", latest="2.0.0", update_available=True, summary="New!")
        notify = AsyncMock()
        observer = UpdateObserver(notify=notify)

        with (
            patch("ductor_bot.infra.updater.check_pypi", return_value=info),
            patch("ductor_bot.infra.updater._INITIAL_DELAY_S", 0),
            patch("ductor_bot.infra.updater._CHECK_INTERVAL_S", 0.01),
        ):
            observer.start()
            # Let multiple check cycles run
            await asyncio.sleep(0.15)
            await observer.stop()

        # Should only notify once for the same version
        notify.assert_called_once()

    async def test_handles_check_failure_gracefully(self) -> None:
        notify = AsyncMock()
        observer = UpdateObserver(notify=notify)

        with (
            patch("ductor_bot.infra.updater.check_pypi", side_effect=RuntimeError("network")),
            patch("ductor_bot.infra.updater._INITIAL_DELAY_S", 0),
            patch("ductor_bot.infra.updater._CHECK_INTERVAL_S", 0.01),
        ):
            observer.start()
            await asyncio.sleep(0.1)
            await observer.stop()

        notify.assert_not_called()

    async def test_handles_none_from_check_pypi(self) -> None:
        notify = AsyncMock()
        observer = UpdateObserver(notify=notify)

        with (
            patch("ductor_bot.infra.updater.check_pypi", return_value=None),
            patch("ductor_bot.infra.updater._INITIAL_DELAY_S", 0),
            patch("ductor_bot.infra.updater._CHECK_INTERVAL_S", 0.01),
        ):
            observer.start()
            await asyncio.sleep(0.1)
            await observer.stop()

        notify.assert_not_called()

    async def test_stop_without_start_is_safe(self) -> None:
        observer = UpdateObserver(notify=AsyncMock())
        await observer.stop()  # Should not raise

    async def test_notifies_again_for_newer_version(self) -> None:
        call_count = 0
        versions = [
            VersionInfo(current="1.0.0", latest="2.0.0", update_available=True, summary="v2"),
            VersionInfo(current="1.0.0", latest="3.0.0", update_available=True, summary="v3"),
        ]

        async def mock_check() -> VersionInfo:
            nonlocal call_count
            idx = min(call_count, len(versions) - 1)
            call_count += 1
            return versions[idx]

        notify = AsyncMock()
        observer = UpdateObserver(notify=notify)

        with (
            patch("ductor_bot.infra.updater.check_pypi", side_effect=mock_check),
            patch("ductor_bot.infra.updater._INITIAL_DELAY_S", 0),
            patch("ductor_bot.infra.updater._CHECK_INTERVAL_S", 0.01),
        ):
            observer.start()
            await asyncio.sleep(0.15)
            await observer.stop()

        assert notify.call_count == 2
