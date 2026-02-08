"""Tests for Docker container management."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, patch

import pytest

from ductor_bot.config import DockerConfig
from ductor_bot.workspace.paths import DuctorPaths


@pytest.fixture
def docker_paths(tmp_path: Path) -> DuctorPaths:
    home = tmp_path / ".ductor"
    home.mkdir()
    ws = home / "workspace"
    ws.mkdir()
    (ws / "tools").mkdir()
    fw = tmp_path / "framework"
    fw.mkdir()
    return DuctorPaths(ductor_home=home, home_defaults=fw / "workspace", framework_root=fw)


@pytest.fixture
def docker_config() -> DockerConfig:
    return DockerConfig(enabled=True, image_name="test-img", container_name="test-ctr")


class TestDockerManager:
    """Test simplified Docker manager."""

    async def test_setup_returns_none_when_docker_not_found(
        self, docker_config: DockerConfig, docker_paths: DuctorPaths
    ) -> None:
        from ductor_bot.infra.docker import DockerManager

        mgr = DockerManager(docker_config, docker_paths)
        with patch("shutil.which", return_value=None):
            result = await mgr.setup()
        assert result is None

    async def test_setup_returns_none_when_daemon_unavailable(
        self, docker_config: DockerConfig, docker_paths: DuctorPaths
    ) -> None:
        from ductor_bot.infra.docker import DockerManager

        mgr = DockerManager(docker_config, docker_paths)
        with (
            patch("shutil.which", return_value="/usr/bin/docker"),
            patch.object(mgr, "_exec", new_callable=AsyncMock, return_value=(1, "error")),
        ):
            result = await mgr.setup()
        assert result is None

    async def test_setup_returns_container_name_on_success(
        self, docker_config: DockerConfig, docker_paths: DuctorPaths
    ) -> None:
        from ductor_bot.infra.docker import DockerManager

        mgr = DockerManager(docker_config, docker_paths)

        async def mock_exec(*args: str, **_kwargs: object) -> tuple[int, str]:
            cmd = " ".join(args)
            if "docker info" in cmd:
                return 0, "ok"
            if "image inspect" in cmd:
                return 0, "ok"
            if "container inspect" in cmd:
                return 1, ""  # Not running -> start fresh
            if "rm -f" in cmd:
                return 0, ""
            if "docker run" in cmd:
                return 0, "container_id"
            return 0, ""

        with (
            patch("shutil.which", return_value="/usr/bin/docker"),
            patch.object(mgr, "_exec", side_effect=mock_exec),
        ):
            result = await mgr.setup()
        assert result == "test-ctr"

    async def test_setup_builds_image_when_auto_build(
        self, docker_config: DockerConfig, docker_paths: DuctorPaths
    ) -> None:
        from ductor_bot.infra.docker import DockerManager

        docker_config.auto_build = True
        # Create Dockerfile.sandbox
        (docker_paths.framework_root / "Dockerfile.sandbox").write_text("FROM ubuntu")
        mgr = DockerManager(docker_config, docker_paths)
        build_called = False

        async def mock_exec(*args: str, **_kwargs: object) -> tuple[int, str]:
            nonlocal build_called
            cmd = " ".join(args)
            if "docker info" in cmd:
                return 0, "ok"
            if "image inspect" in cmd:
                return 1, ""  # Image missing
            if "docker build" in cmd:
                build_called = True
                return 0, "built"
            if "container inspect" in cmd:
                return 1, ""
            if "rm -f" in cmd:
                return 0, ""
            if "docker run" in cmd:
                return 0, "cid"
            return 0, ""

        with (
            patch("shutil.which", return_value="/usr/bin/docker"),
            patch.object(mgr, "_exec", side_effect=mock_exec),
        ):
            result = await mgr.setup()
        assert build_called
        assert result == "test-ctr"

    async def test_setup_returns_none_when_auto_build_disabled(
        self, docker_config: DockerConfig, docker_paths: DuctorPaths
    ) -> None:
        from ductor_bot.infra.docker import DockerManager

        docker_config.auto_build = False
        mgr = DockerManager(docker_config, docker_paths)

        async def mock_exec(*args: str, **_kwargs: object) -> tuple[int, str]:
            cmd = " ".join(args)
            if "docker info" in cmd:
                return 0, "ok"
            if "image inspect" in cmd:
                return 1, ""  # Image missing
            return 0, ""

        with (
            patch("shutil.which", return_value="/usr/bin/docker"),
            patch.object(mgr, "_exec", side_effect=mock_exec),
        ):
            result = await mgr.setup()
        assert result is None

    async def test_teardown_stops_and_removes(
        self, docker_config: DockerConfig, docker_paths: DuctorPaths
    ) -> None:
        from ductor_bot.infra.docker import DockerManager

        mgr = DockerManager(docker_config, docker_paths)
        mgr._container = "test-ctr"
        exec_calls: list[str] = []

        async def mock_exec(*args: str, **_kwargs: object) -> tuple[int, str]:
            exec_calls.append(" ".join(args))
            return 0, ""

        with patch.object(mgr, "_exec", side_effect=mock_exec):
            await mgr.teardown()

        container_after: str | None = mgr._container
        assert container_after is None
        assert any("stop" in c for c in exec_calls)
        assert any("rm" in c for c in exec_calls)

    async def test_teardown_noop_when_no_container(
        self, docker_config: DockerConfig, docker_paths: DuctorPaths
    ) -> None:
        from ductor_bot.infra.docker import DockerManager

        mgr = DockerManager(docker_config, docker_paths)
        assert mgr._container is None
        await mgr.teardown()  # Should not raise

    async def test_exec_returns_exit_code_and_output(
        self, docker_config: DockerConfig, docker_paths: DuctorPaths
    ) -> None:
        from ductor_bot.infra.docker import DockerManager

        mgr = DockerManager(docker_config, docker_paths)
        # Test the static method with a simple command
        rc, output = await mgr._exec("echo", "hello")
        assert rc == 0
        assert "hello" in output

    async def test_exec_handles_timeout(
        self, docker_config: DockerConfig, docker_paths: DuctorPaths
    ) -> None:
        from ductor_bot.infra.docker import DockerManager

        mgr = DockerManager(docker_config, docker_paths)
        rc, _ = await mgr._exec("sleep", "10", deadline_seconds=0.1)
        assert rc != 0

    async def test_mounts_full_ductor_home(
        self, docker_config: DockerConfig, docker_paths: DuctorPaths
    ) -> None:
        """Verify run command mounts entire ~/.ductor, not just workspace."""
        from ductor_bot.infra.docker import DockerManager

        mgr = DockerManager(docker_config, docker_paths)
        run_args: list[str] = []

        async def mock_exec(*args: str, **_kwargs: object) -> tuple[int, str]:
            cmd = " ".join(args)
            if "docker info" in cmd:
                return 0, "ok"
            if "image inspect" in cmd:
                return 0, "ok"
            if "container inspect" in cmd:
                return 1, ""
            if "rm -f" in cmd:
                return 0, ""
            if "docker run" in cmd:
                run_args.extend(args)
                return 0, "cid"
            return 0, ""

        with (
            patch("shutil.which", return_value="/usr/bin/docker"),
            patch.object(mgr, "_exec", side_effect=mock_exec),
        ):
            await mgr.setup()

        run_str = " ".join(run_args)
        # Full ductor_home mounted at /ductor
        assert f"{docker_paths.ductor_home}:/ductor" in run_str
        # Working dir is /ductor/workspace
        assert "-w /ductor/workspace" in run_str
        # DUCTOR_HOME env var set inside container
        assert "DUCTOR_HOME=/ductor" in run_str
        # Template tools overlay should NOT be present
        assert "tools:ro" not in run_str

    async def test_ensure_running_returns_container_when_healthy(
        self, docker_config: DockerConfig, docker_paths: DuctorPaths
    ) -> None:
        from ductor_bot.infra.docker import DockerManager

        mgr = DockerManager(docker_config, docker_paths)
        mgr._container = "test-ctr"

        with patch.object(mgr, "_container_running", new_callable=AsyncMock, return_value=True):
            result = await mgr.ensure_running()
        assert result == "test-ctr"

    async def test_ensure_running_recovers_stopped_container(
        self, docker_config: DockerConfig, docker_paths: DuctorPaths
    ) -> None:
        from ductor_bot.infra.docker import DockerManager

        mgr = DockerManager(docker_config, docker_paths)
        mgr._container = "test-ctr"

        with (
            patch.object(mgr, "_container_running", new_callable=AsyncMock, return_value=False),
            patch.object(mgr, "setup", new_callable=AsyncMock, return_value="test-ctr"),
        ):
            result = await mgr.ensure_running()
        assert result == "test-ctr"

    async def test_ensure_running_returns_none_on_recovery_failure(
        self, docker_config: DockerConfig, docker_paths: DuctorPaths
    ) -> None:
        from ductor_bot.infra.docker import DockerManager

        mgr = DockerManager(docker_config, docker_paths)
        mgr._container = "test-ctr"

        with (
            patch.object(mgr, "_container_running", new_callable=AsyncMock, return_value=False),
            patch.object(mgr, "setup", new_callable=AsyncMock, return_value=None),
        ):
            result = await mgr.ensure_running()
        assert result is None

    async def test_ensure_running_calls_setup_when_no_container(
        self, docker_config: DockerConfig, docker_paths: DuctorPaths
    ) -> None:
        from ductor_bot.infra.docker import DockerManager

        mgr = DockerManager(docker_config, docker_paths)
        assert mgr._container is None

        with patch.object(mgr, "setup", new_callable=AsyncMock, return_value="new-ctr") as mock:
            result = await mgr.ensure_running()
        assert result == "new-ctr"
        mock.assert_awaited_once()

    async def test_uid_mapping_on_linux(
        self, docker_config: DockerConfig, docker_paths: DuctorPaths
    ) -> None:
        """Verify --user flag is added on Linux."""
        from ductor_bot.infra.docker import DockerManager

        mgr = DockerManager(docker_config, docker_paths)
        run_args: list[str] = []

        async def mock_exec(*args: str, **_kwargs: object) -> tuple[int, str]:
            cmd = " ".join(args)
            if "docker info" in cmd:
                return 0, "ok"
            if "image inspect" in cmd:
                return 0, "ok"
            if "container inspect" in cmd:
                return 1, ""
            if "rm -f" in cmd:
                return 0, ""
            if "docker run" in cmd:
                run_args.extend(args)
                return 0, "cid"
            return 0, ""

        with (
            patch("shutil.which", return_value="/usr/bin/docker"),
            patch.object(mgr, "_exec", side_effect=mock_exec),
            patch("ductor_bot.infra.docker._needs_uid_mapping", return_value=True),
            patch("os.getuid", return_value=1000),
            patch("os.getgid", return_value=1000),
        ):
            await mgr.setup()

        run_str = " ".join(run_args)
        assert "--user 1000:1000" in run_str

    async def test_no_uid_mapping_on_macos(
        self, docker_config: DockerConfig, docker_paths: DuctorPaths
    ) -> None:
        """Verify --user flag is NOT added on macOS."""
        from ductor_bot.infra.docker import DockerManager

        mgr = DockerManager(docker_config, docker_paths)
        run_args: list[str] = []

        async def mock_exec(*args: str, **_kwargs: object) -> tuple[int, str]:
            cmd = " ".join(args)
            if "docker info" in cmd:
                return 0, "ok"
            if "image inspect" in cmd:
                return 0, "ok"
            if "container inspect" in cmd:
                return 1, ""
            if "rm -f" in cmd:
                return 0, ""
            if "docker run" in cmd:
                run_args.extend(args)
                return 0, "cid"
            return 0, ""

        with (
            patch("shutil.which", return_value="/usr/bin/docker"),
            patch.object(mgr, "_exec", side_effect=mock_exec),
            patch("ductor_bot.infra.docker._needs_uid_mapping", return_value=False),
        ):
            await mgr.setup()

        run_str = " ".join(run_args)
        assert "--user" not in run_str

    async def test_container_property(
        self, docker_config: DockerConfig, docker_paths: DuctorPaths
    ) -> None:
        from ductor_bot.infra.docker import DockerManager

        mgr = DockerManager(docker_config, docker_paths)
        assert mgr.container is None
        mgr._container = "x"
        assert mgr.container == "x"
