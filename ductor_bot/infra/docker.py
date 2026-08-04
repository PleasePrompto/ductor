"""Docker container management for sandboxed CLI execution."""

from __future__ import annotations

import asyncio
import logging
import os
import platform
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path
from shutil import which
from typing import TYPE_CHECKING, ClassVar

from ductor_bot.cli._log_redact import redact_cmd_for_log
from ductor_bot.config import DockerConfig
from ductor_bot.workspace.paths import DuctorPaths

if TYPE_CHECKING:
    from rich.console import Console

logger = logging.getLogger(__name__)

_DUCTOR_MOUNT = "/ductor"
_CONTAINER_WS = f"{_DUCTOR_MOUNT}/workspace"
_MOUNT_PREFIX = "/mnt"


def _needs_uid_mapping() -> bool:
    """Linux (incl. WSL) needs explicit UID/GID to avoid root-owned files."""
    return platform.system() == "Linux"


def _host_cache_dir() -> Path | None:
    """Return the platform-specific user cache directory.

    - Linux:   ~/.cache
    - macOS:   ~/Library/Caches
    - Windows: %LOCALAPPDATA%
    """
    system = platform.system()
    if system == "Linux":
        return Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    if system == "Darwin":
        return Path.home() / "Library" / "Caches"
    if system == "Windows":
        local = os.environ.get("LOCALAPPDATA")
        return Path(local) if local else None
    return None


def resolve_mount_target(host_path: str, existing_names: set[str]) -> tuple[Path, str] | None:
    """Resolve a host path to a ``(resolved_path, container_target)`` pair.

    Returns ``None`` when the path does not exist or is not a directory.
    Deduplicates container-side basenames by appending ``_2``, ``_3``, etc.
    """
    expanded = Path(os.path.expandvars(host_path)).expanduser()
    try:
        resolved = expanded.resolve(strict=True)
    except OSError:
        logger.warning("Docker mount path does not exist: %s", host_path)
        return None
    if not resolved.is_dir():
        logger.warning("Docker mount path is not a directory: %s", host_path)
        return None

    base = resolved.name or "root"
    # Sanitize: strip characters not safe in Linux paths.
    safe = "".join(c for c in base if c not in '<>:"|?*')
    name = safe or "mount"
    candidate = name
    counter = 2
    while candidate in existing_names:
        candidate = f"{name}_{counter}"
        counter += 1
    existing_names.add(candidate)
    return resolved, f"{_MOUNT_PREFIX}/{candidate}"


def _build_user_mount_flags(mounts: list[str]) -> list[str]:
    """Return ``-v`` flags for user-configured project mounts."""
    flags: list[str] = []
    used_names: set[str] = set()
    for mount_path in mounts:
        pair = resolve_mount_target(mount_path, used_names)
        if pair is not None:
            host_resolved, container_target = pair
            flags += ["-v", f"{host_resolved}:{container_target}:rw"]
            logger.info("User mount: %s -> %s", host_resolved, container_target)
    return flags


def _build_published_port_flags(ports: Sequence[object]) -> list[str]:
    """Return ``-p`` flags for user-configured container port publications.

    Each entry is passed verbatim to ``docker run -p`` so the full docker
    syntax is supported (``host_port:container_port``,
    ``host_ip:host_port:container_port``, ``host_port:container_port/udp``).
    Empty or non-string entries are skipped; docker validates the rest.
    """
    flags: list[str] = []
    for entry in ports:
        if not isinstance(entry, str):
            logger.warning("Ignoring non-string published_ports entry: %r", entry)
            continue
        spec = entry.strip()
        if not spec:
            continue
        flags += ["-p", spec]
        logger.info("Published port: %s", spec)
    return flags


class DockerManager:
    """Manages a persistent Docker sidecar for sandboxed CLI execution.

    Every step that can fail logs a warning and returns ``None`` so the
    caller can fall back to host execution.

    In multi-agent mode multiple ``DockerManager`` instances (one per agent)
    share a single container.  A class-level lock serialises ``setup()`` so
    the first caller creates the container and subsequent callers reuse it.
    """

    _setup_lock: ClassVar[asyncio.Lock] = asyncio.Lock()

    def __init__(self, config: DockerConfig, paths: DuctorPaths) -> None:
        self._config = config
        self._paths = paths
        self._container: str | None = None
        self._console: Console | None = self._create_console()

    @property
    def container(self) -> str | None:
        """Currently active container name, or ``None``."""
        return self._container

    @staticmethod
    def _create_console() -> Console | None:
        """Create a Rich console when running interactively."""
        if sys.stderr is None or not sys.stderr.isatty():
            return None
        from rich.console import Console as RichConsole

        return RichConsole(stderr=True)

    def _status(self, msg: str) -> None:
        """Print a status message to the interactive console (if available)."""
        if self._console:
            self._console.print(msg)

    async def setup(self) -> str | None:
        """Start or reuse the sandbox container. Returns name or ``None``.

        Serialised via a class-level lock so that in multi-agent mode only the
        first caller creates the container; subsequent callers reuse it.
        """
        async with self._setup_lock:
            return await self._setup_unlocked()

    async def _setup_unlocked(self) -> str | None:
        """Inner setup logic, always called under ``_setup_lock``."""
        if not which("docker"):
            self._status(
                "[bold red]Docker binary not found, falling back to host execution.[/bold red]"
            )
            logger.warning("Docker binary not found, falling back to host execution")
            return None

        self._status("[dim]Checking Docker daemon...[/dim]")
        if not await self._daemon_available():
            self._status("[bold red]Docker daemon not responding.[/bold red]")
            logger.warning("Docker daemon not responding, falling back to host execution")
            return None
        self._status("[green]Docker daemon OK.[/green]")

        image = self._config.image_name
        if not await self._image_exists(image):
            if not self._config.auto_build:
                self._status(
                    f"[bold red]Docker image '{image}' not found and auto_build disabled.[/bold red]"
                )
                logger.warning("Docker image '%s' not found and auto_build disabled", image)
                return None
            extras_msg = ""
            if self._config.extras:
                from ductor_bot.infra.docker_extras import DOCKER_EXTRAS_BY_ID

                names = [
                    DOCKER_EXTRAS_BY_ID[e].name
                    for e in self._config.extras
                    if e in DOCKER_EXTRAS_BY_ID
                ]
                if names:
                    extras_msg = f"\n  Installing extras: {', '.join(names)}"
            self._status(
                f"[bold cyan]Building sandbox image '{image}'...[/bold cyan]\n"
                "[dim]  Downloading Debian bookworm + Node.js 22 base image\n"
                "  Installing Python, build tools, Git\n"
                f"  Installing Claude, Codex, Gemini, and Grok CLIs{extras_msg}\n"
                "  This may take a few minutes on first run...[/dim]"
            )
            if not await self._build_image(image):
                self._status("[bold red]Docker image build failed.[/bold red]")
                logger.warning("Docker image build failed, falling back to host execution")
                return None
            self._status(f"[bold green]Image '{image}' built successfully.[/bold green]")
        else:
            self._status(f"[dim]Image '{image}' found.[/dim]")

        container = self._config.container_name
        if await self._container_running(container):
            self._status(f"[dim]Reusing running container '{container}'.[/dim]")
            logger.info("Reusing running Docker container '%s'", container)
        else:
            self._status(f"[dim]Starting container '{container}'...[/dim]")
            await self._remove_container(container)
            if not await self._start_container(container, image):
                self._status("[bold red]Failed to start container.[/bold red]")
                logger.warning("Failed to start container, falling back to host execution")
                return None
            self._status(f"[bold green]Container '{container}' ready.[/bold green]")

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
        dockerfile = self._paths.dockerfile_sandbox_path
        if not dockerfile.exists():
            logger.error("Dockerfile.sandbox not found at %s", dockerfile)
            return False

        base_content = dockerfile.read_text(encoding="utf-8")

        from ductor_bot.infra.docker_extras import (
            calculate_build_timeout,
            generate_dockerfile_extras,
            resolve_extras,
        )

        extras = resolve_extras(self._config.extras)
        if extras:
            dockerfile_content = generate_dockerfile_extras(base_content, extras)
        else:
            dockerfile_content = base_content

        timeout = calculate_build_timeout(extras)

        logger.info("Building Docker image '%s'...", image)
        with tempfile.TemporaryDirectory() as ctx:
            ctx_dockerfile = Path(ctx) / "Dockerfile"
            ctx_dockerfile.write_text(dockerfile_content, encoding="utf-8")
            rc, output = await self._exec_stream(
                "docker",
                "build",
                "-t",
                image,
                "-f",
                str(ctx_dockerfile),
                ctx,
                deadline_seconds=timeout,
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
        # Always mount the root ductor home, even when called from a sub-agent.
        # Sub-agent homes live at <root>/agents/<name>/; the container must see
        # the full tree so every agent can access its own workspace via paths
        # like /ductor/agents/<name>/workspace.
        ductor_home = self._paths.ductor_home
        if ductor_home.parent.name == "agents":
            ductor_home = ductor_home.parent.parent

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
            # Allow inter-agent communication from inside the container back
            # to the host's InternalAgentAPI (127.0.0.1:8799).
            "--add-host=host.docker.internal:host-gateway",
        ]

        # User-defined published ports (e.g. exposing in-container web UIs to
        # the host or LAN). Each entry is passed verbatim to ``docker run -p``.
        cmd += _build_published_port_flags(self._config.published_ports)

        # Linux (incl. WSL) needs explicit UID/GID so files created inside the
        # container are owned by the host user, not root.
        # macOS and Windows Docker Desktop handle this transparently.
        if _needs_uid_mapping():
            uid = os.getuid()
            gid = os.getgid()
            cmd += ["--user", f"{uid}:{gid}"]
            # Explicit HOME so CLIs find their config dirs (~/.claude, ~/.codex,
            # ~/.gemini) even when the host UID has no passwd entry inside the
            # container.
            cmd += ["-e", "HOME=/home/node"]

        # Auth directories -- mount only if they exist on the host.
        home = Path.home()
        container_home = "/home/node"
        auth_dirs: list[tuple[Path, str, str]] = [
            (home / ".claude", f"{container_home}/.claude", "rw"),
            (home / ".codex", f"{container_home}/.codex", "rw"),
            (home / ".gemini", f"{container_home}/.gemini", "rw"),
            (home / ".grok", f"{container_home}/.grok", "rw"),
        ]

        # Optional: mount host cache dir for browser profiles & binaries.
        # Disabled by default -- exposes host cache to the sandbox.
        if self._config.mount_host_cache:
            host_cache = _host_cache_dir()
            if host_cache and host_cache.is_dir():
                auth_dirs.append(
                    (host_cache, f"{container_home}/.cache", "rw"),
                )

        for auth_dir, target, mode in auth_dirs:
            if auth_dir.is_dir():
                cmd += ["-v", f"{auth_dir}:{target}:{mode}"]

        # Auth config files at the home root (e.g. ~/.claude.json).
        for auth_file, target in [
            (home / ".claude.json", f"{container_home}/.claude.json"),
        ]:
            if auth_file.is_file():
                cmd += ["-v", f"{auth_file}:{target}:rw"]

        # User-defined project mounts.
        cmd += _build_user_mount_flags(self._config.mounts)

        # User secrets from .env (never override existing host vars).
        cmd += self._env_secret_flags()

        cmd.append(image)

        logger.info("Starting Docker container '%s' from image '%s'", name, image)
        logger.debug("docker run cmd: %s", " ".join(redact_cmd_for_log(cmd)))
        rc, output = await self._exec(*cmd)
        if rc != 0:
            logger.error("docker run failed:\n%s", output[-2000:])
            return False

        logger.info("Container '%s' started successfully", name)
        return True

    def _env_secret_flags(self) -> list[str]:
        """Return ``-e`` flags for user secrets from ``~/.ductor/.env``."""
        from ductor_bot.infra.env_secrets import load_env_secrets

        flags: list[str] = []
        for key, value in load_env_secrets(self._paths.env_file).items():
            if key not in os.environ:
                flags += ["-e", f"{key}={value}"]
        return flags

    async def _exec_stream(
        self,
        *args: str,
        deadline_seconds: float = 30,
    ) -> tuple[int, str]:
        """Run a Docker command, streaming output to the console.

        Returns ``(returncode, full_output)`` just like ``_exec``, but prints
        each line to stderr in real-time so the user can follow progress.
        """
        proc: asyncio.subprocess.Process | None = None
        collected: list[str] = []
        try:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.STDOUT,
            )
            assert proc.stdout is not None
            async with asyncio.timeout(deadline_seconds):
                async for raw_line in proc.stdout:
                    line = raw_line.decode(errors="replace").rstrip()
                    collected.append(line)
                    if self._console:
                        self._console.print(f"  [dim]{line}[/dim]")
                    else:
                        logger.info("docker build: %s", line)
                await proc.wait()
            return proc.returncode or 0, "\n".join(collected)
        except TimeoutError:
            if proc is not None:
                proc.kill()
                await proc.wait()
            msg = f"Timed out after {deadline_seconds}s"
            if self._console:
                self._console.print(f"  [bold red]{msg}[/bold red]")
            logger.debug("Docker command timed out: %s", args[:3])
            return 1, "\n".join(collected) + f"\n{msg}"
        except OSError as exc:
            logger.debug("Docker command failed: %s -> %s", args[:3], exc)
            return 1, str(exc)

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
