"""Docker container management for sandboxed CLI execution."""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import tempfile
from pathlib import Path
from shutil import which

from ductor_bot.config import DockerConfig
from ductor_bot.workspace.paths import DuctorPaths

logger = logging.getLogger(__name__)

_DUCTOR_MOUNT = "/ductor"
_CONTAINER_WS = f"{_DUCTOR_MOUNT}/workspace"


def _needs_uid_mapping() -> bool:
    """Linux (incl. WSL) needs explicit UID/GID to avoid root-owned files."""
    return platform.system() == "Linux"


class DockerManager:
    """Manages a persistent Docker sidecar for sandboxed CLI execution.

    Every step that can fail logs a warning and returns ``None`` so the
    caller can fall back to host execution.
    """

    def __init__(self, config: DockerConfig, paths: DuctorPaths) -> None:
        self._config = config
        self._paths = paths
        self._container: str | None = None

    @property
    def container(self) -> str | None:
        """Currently active container name, or ``None``."""
        return self._container

    async def setup(self) -> str | None:
        """Start or reuse the sandbox container. Returns name or ``None``."""
        if not which("docker"):
            logger.warning("Docker binary not found, falling back to host execution")
            return None

        if not await self._daemon_available():
            logger.warning("Docker daemon not responding, falling back to host execution")
            return None

        image = self._config.image_name
        if not await self._image_exists(image):
            if not self._config.auto_build:
                logger.warning("Docker image '%s' not found and auto_build disabled", image)
                return None
            if not await self._build_image(image):
                logger.warning("Docker image build failed, falling back to host execution")
                return None

        container = self._config.container_name
        if await self._container_running(container):
            logger.info("Reusing running Docker container '%s'", container)
        else:
            await self._remove_container(container)
            if not await self._start_container(container, image):
                logger.warning("Failed to start container, falling back to host execution")
                return None

        self._container = container
        return container

    async def ensure_running(self) -> str | None:
        """Verify the container is alive; auto-recover if it stopped.

        Returns the container name on success, or ``None`` if recovery failed.
        """
        if not self._container:
            return await self.setup()

        if await self._container_running(self._container):
            return self._container

        logger.warning(
            "Docker container '%s' stopped unexpectedly, recovering...",
            self._container,
        )
        self._container = None
        return await self.setup()

    async def teardown(self) -> None:
        """Stop and remove the container."""
        if not self._container:
            return
        name = self._container
        self._container = None
        await self._exec("docker", "stop", "-t", "5", name)
        await self._exec("docker", "rm", "-f", name)
        logger.info("Docker container '%s' stopped and removed", name)

    # -- internal helpers -------------------------------------------------

    async def _daemon_available(self) -> bool:
        rc, _ = await self._exec("docker", "info")
        return rc == 0

    async def _image_exists(self, image: str) -> bool:
        rc, _ = await self._exec("docker", "image", "inspect", image)
        return rc == 0

    async def _build_image(self, image: str) -> bool:
        dockerfile = self._paths.framework_root / "Dockerfile.sandbox"
        if not dockerfile.exists():
            logger.error("Dockerfile.sandbox not found at %s", dockerfile)
            return False
        logger.info("Building Docker image '%s'...", image)
        with tempfile.TemporaryDirectory() as ctx:
            rc, output = await self._exec(
                "docker",
                "build",
                "-t",
                image,
                "-f",
                str(dockerfile),
                ctx,
                deadline_seconds=300,
            )
        if rc != 0:
            logger.error("Docker build failed:\n%s", output[-2000:])
        return rc == 0

    async def _container_running(self, name: str) -> bool:
        rc, output = await self._exec(
            "docker",
            "container",
            "inspect",
            "-f",
            "{{.State.Running}}",
            name,
        )
        return rc == 0 and output.strip() == "true"

    async def _remove_container(self, name: str) -> None:
        await self._exec("docker", "rm", "-f", name)

    async def _start_container(self, name: str, image: str) -> bool:
        ductor_home = self._paths.ductor_home

        cmd: list[str] = [
            "docker",
            "run",
            "-d",
            "--name",
            name,
            "-w",
            _CONTAINER_WS,
            # Mount the ENTIRE ~/.ductor so the CLI sees all framework files.
            "-v",
            f"{ductor_home}:{_DUCTOR_MOUNT}",
            "-e",
            f"DUCTOR_HOME={_DUCTOR_MOUNT}",
        ]

        # Linux (incl. WSL) needs explicit UID/GID so files created inside the
        # container are owned by the host user, not root.
        # macOS and Windows Docker Desktop handle this transparently.
        if _needs_uid_mapping():
            uid = os.getuid()
            gid = os.getgid()
            cmd += ["--user", f"{uid}:{gid}"]

        # Auth directories -- mount only if they exist on the host.
        home = Path.home()
        for auth_dir, target, mode in [
            (home / ".claude", "/home/node/.claude", "rw"),
            (home / ".codex", "/home/node/.codex", "rw"),
        ]:
            if auth_dir.is_dir():
                cmd += ["-v", f"{auth_dir}:{target}:{mode}"]

        cmd.append(image)

        logger.info("Starting Docker container '%s' from image '%s'", name, image)
        logger.debug("docker run cmd: %s", " ".join(cmd))
        rc, output = await self._exec(*cmd)
        if rc != 0:
            logger.error("docker run failed:\n%s", output[-2000:])
            return False

        logger.info("Container '%s' started successfully", name)
        return True

    @staticmethod
    async def _exec(
        *args: str,
        deadline_seconds: float = 30,
    ) -> tuple[int, str]:
        """Run a Docker command and return ``(returncode, stdout)``."""
        proc: asyncio.subprocess.Process | None = None
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            async with asyncio.timeout(deadline_seconds):
                stdout, _ = await proc.communicate()
            return proc.returncode or 0, stdout.decode(errors="replace") if stdout else ""
        except TimeoutError:
            if proc is not None:
                proc.kill()
                await proc.wait()
            logger.debug("Docker command timed out: %s", args[:3])
            return 1, f"Timed out after {deadline_seconds}s"
        except OSError as exc:
            logger.debug("Docker command failed: %s -> %s", args[:3], exc)
            return 1, str(exc)
