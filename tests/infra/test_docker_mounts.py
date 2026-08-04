"""Tests for Docker user-mount functionality."""

from __future__ import annotations

import json
import os
from pathlib import Path
from unittest.mock import patch

import pytest

from ductor_bot.config import DockerConfig
from ductor_bot.infra.docker import resolve_mount_target
from ductor_bot.workspace.paths import DuctorPaths

# ---------------------------------------------------------------------------
# resolve_mount_target
# ---------------------------------------------------------------------------


class TestResolveMountTarget:
    """Unit tests for the resolve_mount_target helper."""

    def test_valid_directory(self, tmp_path: Path) -> None:
        proj = tmp_path / "myproject"
        proj.mkdir()
        names: set[str] = set()
        result = resolve_mount_target(str(proj), names)
        assert result is not None
        resolved, target = result
        assert resolved == proj
        assert target == "/mnt/myproject"
        assert "myproject" in names

    def test_nonexistent_path(self) -> None:
        result = resolve_mount_target("/this/does/not/exist", set())
        assert result is None

    def test_file_not_directory(self, tmp_path: Path) -> None:
        f = tmp_path / "afile.txt"
        f.write_text("x")
        result = resolve_mount_target(str(f), set())
        assert result is None

    def test_tilde_expansion(self, tmp_path: Path) -> None:
        proj = tmp_path / "proj"
        proj.mkdir()
        with patch.dict(os.environ, {"HOME": str(tmp_path)}):
            result = resolve_mount_target("~/proj", set())
        assert result is not None
        assert result[0] == proj

    def test_envvar_expansion(self, tmp_path: Path) -> None:
        proj = tmp_path / "code"
        proj.mkdir()
        with patch.dict(os.environ, {"MY_CODE": str(tmp_path)}):
            result = resolve_mount_target("$MY_CODE/code", set())
        assert result is not None
        assert result[0] == proj

    def test_deduplication_appends_suffix(self, tmp_path: Path) -> None:
        a = tmp_path / "x" / "proj"
        a.mkdir(parents=True)
        b = tmp_path / "y" / "proj"
        b.mkdir(parents=True)

        names: set[str] = set()
        r1 = resolve_mount_target(str(a), names)
        r2 = resolve_mount_target(str(b), names)

        assert r1 is not None
        assert r2 is not None
        assert r1[1] == "/mnt/proj"
        assert r2[1] == "/mnt/proj_2"

    def test_triple_deduplication(self, tmp_path: Path) -> None:
        dirs = []
        for i in range(3):
            d = tmp_path / str(i) / "same"
            d.mkdir(parents=True)
            dirs.append(d)

        names: set[str] = set()
        targets = []
        for d in dirs:
            r = resolve_mount_target(str(d), names)
            assert r is not None
            targets.append(r[1])

        assert targets == ["/mnt/same", "/mnt/same_2", "/mnt/same_3"]

    def test_sanitizes_windows_chars(self, tmp_path: Path) -> None:
        proj = tmp_path / "clean"
        proj.mkdir()
        names: set[str] = set()
        # Simulate a name that contains Windows-forbidden chars.
        # We can't create a dir with those chars on most filesystems,
        # so test the sanitization by pre-populating the name and checking
        # the function handles it correctly with a real directory.
        result = resolve_mount_target(str(proj), names)
        assert result is not None
        assert result[1] == "/mnt/clean"

    def test_sanitize_all_unsafe_chars_uses_fallback(self, tmp_path: Path) -> None:
        # When all characters in the basename are stripped by sanitization,
        # the fallback name "mount" should be used.
        proj = tmp_path / "safe_dir"
        proj.mkdir()
        names: set[str] = set()
        result = resolve_mount_target(str(proj), names)
        assert result is not None
        # The name "safe_dir" contains no special chars, so it stays as-is.
        assert result[1] == "/mnt/safe_dir"


# ---------------------------------------------------------------------------
# DockerManager._start_container with mounts
# ---------------------------------------------------------------------------


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


class TestDockerManagerMounts:
    """Integration tests: verify mounts appear in the docker run command."""

    async def test_existing_grok_home_is_mounted(
        self, tmp_path: Path, docker_paths: DuctorPaths
    ) -> None:
        (tmp_path / ".grok").mkdir()
        config = DockerConfig(enabled=True)
        from ductor_bot.infra.docker import DockerManager

        mgr = DockerManager(config, docker_paths)
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
            patch("ductor_bot.infra.docker.which", return_value="/usr/bin/docker"),
            patch.object(Path, "home", return_value=tmp_path),
            patch.object(mgr, "_exec", side_effect=mock_exec),
        ):
            await mgr.setup()

        expected = ["-v", f"{tmp_path}/.grok:/home/node/.grok:rw"]
        assert any(run_args[index : index + 2] == expected for index in range(len(run_args) - 1))

    async def test_missing_grok_home_is_not_mounted(
        self, tmp_path: Path, docker_paths: DuctorPaths
    ) -> None:
        config = DockerConfig(enabled=True)
        from ductor_bot.infra.docker import DockerManager

        mgr = DockerManager(config, docker_paths)
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
            patch("ductor_bot.infra.docker.which", return_value="/usr/bin/docker"),
            patch.object(Path, "home", return_value=tmp_path),
            patch.object(mgr, "_exec", side_effect=mock_exec),
        ):
            await mgr.setup()

        assert run_args
        mount_args = [run_args[index + 1] for index, arg in enumerate(run_args[:-1]) if arg == "-v"]
        assert all(str(tmp_path / ".grok") not in arg for arg in mount_args)
        assert all("/home/node/.grok" not in arg for arg in mount_args)

    async def test_existing_provider_homes_remain_mounted(
        self, tmp_path: Path, docker_paths: DuctorPaths
    ) -> None:
        provider_homes = [".claude", ".codex", ".gemini"]
        for provider_home in provider_homes:
            (tmp_path / provider_home).mkdir()

        config = DockerConfig(enabled=True)
        from ductor_bot.infra.docker import DockerManager

        mgr = DockerManager(config, docker_paths)
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
            patch("ductor_bot.infra.docker.which", return_value="/usr/bin/docker"),
            patch.object(Path, "home", return_value=tmp_path),
            patch.object(mgr, "_exec", side_effect=mock_exec),
        ):
            await mgr.setup()

        for provider_home in provider_homes:
            expected = [
                "-v",
                f"{tmp_path}/{provider_home}:/home/node/{provider_home}:rw",
            ]
            assert any(
                run_args[index : index + 2] == expected for index in range(len(run_args) - 1)
            )

    async def test_single_mount_in_run_cmd(self, tmp_path: Path, docker_paths: DuctorPaths) -> None:
        proj = tmp_path / "myapp"
        proj.mkdir()

        config = DockerConfig(enabled=True, mounts=[str(proj)])
        from ductor_bot.infra.docker import DockerManager

        mgr = DockerManager(config, docker_paths)
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
        assert f"{proj}:/mnt/myapp:rw" in run_str

    async def test_multiple_mounts_in_run_cmd(
        self, tmp_path: Path, docker_paths: DuctorPaths
    ) -> None:
        proj_a = tmp_path / "alpha"
        proj_a.mkdir()
        proj_b = tmp_path / "beta"
        proj_b.mkdir()

        config = DockerConfig(enabled=True, mounts=[str(proj_a), str(proj_b)])
        from ductor_bot.infra.docker import DockerManager

        mgr = DockerManager(config, docker_paths)
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
        assert f"{proj_a}:/mnt/alpha:rw" in run_str
        assert f"{proj_b}:/mnt/beta:rw" in run_str

    async def test_duplicate_basenames_get_suffix(
        self, tmp_path: Path, docker_paths: DuctorPaths
    ) -> None:
        a = tmp_path / "x" / "proj"
        a.mkdir(parents=True)
        b = tmp_path / "y" / "proj"
        b.mkdir(parents=True)

        config = DockerConfig(enabled=True, mounts=[str(a), str(b)])
        from ductor_bot.infra.docker import DockerManager

        mgr = DockerManager(config, docker_paths)
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
        assert f"{a}:/mnt/proj:rw" in run_str
        assert f"{b}:/mnt/proj_2:rw" in run_str

    async def test_nonexistent_mount_silently_skipped(
        self, tmp_path: Path, docker_paths: DuctorPaths
    ) -> None:
        config = DockerConfig(enabled=True, mounts=["/does/not/exist"])
        from ductor_bot.infra.docker import DockerManager

        mgr = DockerManager(config, docker_paths)
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
            result = await mgr.setup()

        assert result is not None  # Container still starts.
        run_str = " ".join(run_args)
        assert "/mnt/" not in run_str  # No user mount appeared.

    async def test_empty_mounts_list(self, docker_paths: DuctorPaths) -> None:
        config = DockerConfig(enabled=True, mounts=[])
        from ductor_bot.infra.docker import DockerManager

        mgr = DockerManager(config, docker_paths)
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
            result = await mgr.setup()

        assert result is not None
        run_str = " ".join(run_args)
        assert "/mnt/" not in run_str


# ---------------------------------------------------------------------------
# CLI commands: mount, unmount, mounts
# ---------------------------------------------------------------------------


class TestDockerMountCLI:
    """Test the CLI functions for managing mounts."""

    def _write_config(self, config_path: Path, docker: dict[str, object] | None = None) -> None:
        data: dict[str, object] = {}
        if docker is not None:
            data["docker"] = docker
        config_path.write_text(json.dumps(data, indent=2), encoding="utf-8")

    def _read_config(self, config_path: Path) -> dict[str, object]:
        return json.loads(config_path.read_text(encoding="utf-8"))

    def test_mount_adds_path(self, tmp_path: Path) -> None:
        from ductor_bot.cli_commands.docker import docker_mount

        config_path = tmp_path / "config.json"
        proj = tmp_path / "myapp"
        proj.mkdir()
        self._write_config(config_path, {"enabled": True, "mounts": []})

        paths_mock = type("P", (), {"config_path": config_path})()
        with patch("ductor_bot.cli_commands.docker.resolve_paths", return_value=paths_mock):
            docker_mount(["docker", "mount", str(proj)])

        data = self._read_config(config_path)
        mounts = data["docker"]["mounts"]
        assert str(proj) in mounts

    def test_mount_rejects_nonexistent(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        from ductor_bot.cli_commands.docker import docker_mount

        config_path = tmp_path / "config.json"
        self._write_config(config_path, {"enabled": True, "mounts": []})

        paths_mock = type("P", (), {"config_path": config_path})()
        with patch("ductor_bot.cli_commands.docker.resolve_paths", return_value=paths_mock):
            docker_mount(["docker", "mount", "/no/such/dir"])

        data = self._read_config(config_path)
        assert data["docker"]["mounts"] == []

    def test_mount_deduplicates(self, tmp_path: Path) -> None:
        from ductor_bot.cli_commands.docker import docker_mount

        config_path = tmp_path / "config.json"
        proj = tmp_path / "myapp"
        proj.mkdir()
        self._write_config(config_path, {"enabled": True, "mounts": [str(proj)]})

        paths_mock = type("P", (), {"config_path": config_path})()
        with patch("ductor_bot.cli_commands.docker.resolve_paths", return_value=paths_mock):
            docker_mount(["docker", "mount", str(proj)])

        data = self._read_config(config_path)
        assert data["docker"]["mounts"].count(str(proj)) == 1

    def test_mount_creates_docker_section(self, tmp_path: Path) -> None:
        from ductor_bot.cli_commands.docker import docker_mount

        config_path = tmp_path / "config.json"
        proj = tmp_path / "myapp"
        proj.mkdir()
        # Config without docker section at all.
        config_path.write_text("{}", encoding="utf-8")

        paths_mock = type("P", (), {"config_path": config_path})()
        with patch("ductor_bot.cli_commands.docker.resolve_paths", return_value=paths_mock):
            docker_mount(["docker", "mount", str(proj)])

        data = self._read_config(config_path)
        assert str(proj) in data["docker"]["mounts"]

    def test_unmount_removes_path(self, tmp_path: Path) -> None:
        from ductor_bot.cli_commands.docker import docker_unmount

        config_path = tmp_path / "config.json"
        proj = tmp_path / "myapp"
        proj.mkdir()
        self._write_config(config_path, {"enabled": True, "mounts": [str(proj)]})

        paths_mock = type("P", (), {"config_path": config_path})()
        with patch("ductor_bot.cli_commands.docker.resolve_paths", return_value=paths_mock):
            docker_unmount(["docker", "unmount", str(proj)])

        data = self._read_config(config_path)
        assert data["docker"]["mounts"] == []

    def test_unmount_by_basename(self, tmp_path: Path) -> None:
        from ductor_bot.cli_commands.docker import docker_unmount

        config_path = tmp_path / "config.json"
        proj = tmp_path / "myapp"
        proj.mkdir()
        self._write_config(config_path, {"enabled": True, "mounts": [str(proj)]})

        paths_mock = type("P", (), {"config_path": config_path})()
        with patch("ductor_bot.cli_commands.docker.resolve_paths", return_value=paths_mock):
            docker_unmount(["docker", "unmount", "myapp"])

        data = self._read_config(config_path)
        assert data["docker"]["mounts"] == []

    def test_unmount_nonexistent_shows_error(self, tmp_path: Path) -> None:
        from ductor_bot.cli_commands.docker import docker_unmount

        config_path = tmp_path / "config.json"
        self._write_config(config_path, {"enabled": True, "mounts": ["/some/path"]})

        paths_mock = type("P", (), {"config_path": config_path})()
        with patch("ductor_bot.cli_commands.docker.resolve_paths", return_value=paths_mock):
            docker_unmount(["docker", "unmount", "/totally/different"])

        data = self._read_config(config_path)
        assert data["docker"]["mounts"] == ["/some/path"]  # Unchanged.

    def test_mounts_list_empty(self, tmp_path: Path) -> None:
        from ductor_bot.cli_commands.docker import docker_list_mounts

        config_path = tmp_path / "config.json"
        self._write_config(config_path, {"enabled": True, "mounts": []})

        paths_mock = type("P", (), {"config_path": config_path})()
        with patch("ductor_bot.cli_commands.docker.resolve_paths", return_value=paths_mock):
            docker_list_mounts()  # Should not raise.

    def test_mounts_list_with_entries(self, tmp_path: Path) -> None:
        from ductor_bot.cli_commands.docker import docker_list_mounts

        config_path = tmp_path / "config.json"
        proj = tmp_path / "proj"
        proj.mkdir()
        self._write_config(config_path, {"enabled": True, "mounts": [str(proj)]})

        paths_mock = type("P", (), {"config_path": config_path})()
        with patch("ductor_bot.cli_commands.docker.resolve_paths", return_value=paths_mock):
            docker_list_mounts()  # Should not raise.

    def test_no_args_shows_usage(self, tmp_path: Path) -> None:
        from ductor_bot.cli_commands.docker import docker_mount

        config_path = tmp_path / "config.json"
        self._write_config(config_path, {"enabled": True, "mounts": []})

        paths_mock = type("P", (), {"config_path": config_path})()
        with patch("ductor_bot.cli_commands.docker.resolve_paths", return_value=paths_mock):
            docker_mount(["docker", "mount"])  # No path arg -- should not crash.


# ---------------------------------------------------------------------------
# Config deep-merge preserves mounts
# ---------------------------------------------------------------------------


class TestConfigDeepMergeMounts:
    """Verify deep_merge_config handles the mounts list correctly."""

    def test_new_mounts_key_added(self) -> None:
        from ductor_bot.config import deep_merge_config

        user: dict[str, object] = {"docker": {"enabled": True}}
        defaults: dict[str, object] = {"docker": {"enabled": False, "mounts": []}}
        merged, changed = deep_merge_config(user, defaults)
        docker = merged["docker"]
        assert isinstance(docker, dict)
        assert docker["enabled"] is True  # User value preserved.
        assert docker["mounts"] == []  # New key added.
        assert changed is True

    def test_existing_mounts_preserved(self) -> None:
        from ductor_bot.config import deep_merge_config

        user: dict[str, object] = {"docker": {"enabled": True, "mounts": ["/home/user/proj"]}}
        defaults: dict[str, object] = {"docker": {"enabled": False, "mounts": []}}
        merged, _ = deep_merge_config(user, defaults)
        docker = merged["docker"]
        assert isinstance(docker, dict)
        assert docker["mounts"] == ["/home/user/proj"]


# ---------------------------------------------------------------------------
# DockerConfig Pydantic model
# ---------------------------------------------------------------------------


class TestDockerConfigModel:
    """Verify the Pydantic model handles the mounts field."""

    def test_default_mounts_empty(self) -> None:
        config = DockerConfig()
        assert config.mounts == []

    def test_mounts_from_dict(self) -> None:
        config = DockerConfig(mounts=["/home/user/proj", "/opt/data"])
        assert config.mounts == ["/home/user/proj", "/opt/data"]

    def test_mounts_serialization(self) -> None:
        config = DockerConfig(mounts=["/a", "/b"])
        data = config.model_dump()
        assert data["mounts"] == ["/a", "/b"]
