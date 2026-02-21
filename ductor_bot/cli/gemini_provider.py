"""Async wrapper around the Google Gemini CLI (Aligned with Claude-style logic)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import tempfile
from collections.abc import AsyncGenerator, Iterator
from contextlib import contextmanager
from pathlib import Path

from ductor_bot.cli.base import BaseCLI, CLIConfig, docker_wrap
from ductor_bot.cli.gemini_events import parse_gemini_stream_line
from ductor_bot.cli.stream_events import (
    ResultEvent,
    StreamEvent,
    SystemInitEvent,
)
from ductor_bot.cli.types import CLIResponse

logger = logging.getLogger(__name__)


_DEFAULT_TIMEOUT = 300.0


class GeminiCLI(BaseCLI):
    """Async wrapper around the Google Gemini CLI."""

    def __init__(self, config: CLIConfig) -> None:
        self._config = config
        self._working_dir = Path(config.working_dir).resolve()
        self._cli_js = self._find_cli_js()
        logger.info(
            "Gemini CLI wrapper (Claude-style): cwd=%s, model=%s", self._working_dir, config.model
        )
        self._trust_workspace()

    def _trust_workspace(self) -> None:
        """Programmatically trust the ductor workspace in Gemini CLI config."""
        gemini_home = Path.home() / ".gemini"
        trust_file = gemini_home / "trustedFolders.json"
        workspace_path = str(self._working_dir)

        # Normalize for Windows if applicable
        if os.name == "nt":
            workspace_path = workspace_path.replace("/", "\\")

        try:
            data: dict[str, str] = {}
            if trust_file.is_file():
                try:
                    data = json.loads(trust_file.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    logger.warning("Corrupt Gemini trust file, starting fresh")

            if workspace_path not in data:
                data[workspace_path] = "TRUST_FOLDER"
                gemini_home.mkdir(parents=True, exist_ok=True)
                trust_file.write_text(
                    json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8"
                )
                logger.info("Trusted workspace in Gemini CLI: %s", workspace_path)
        except Exception:
            logger.warning("Failed to update Gemini trusted folders", exc_info=True)

    def _find_cli_js(self) -> str | None:
        """Find the absolute path to the Gemini CLI's index.js via npm."""
        import subprocess
        from shutil import which

        npm_path = which("npm")
        if npm_path:
            try:
                root = subprocess.check_output(
                    [npm_path, "root", "-g"], text=True, encoding="utf-8"
                ).strip()
                candidate = Path(root) / "@google" / "gemini-cli" / "dist" / "index.js"
                if candidate.is_file():
                    return str(candidate)
            except (subprocess.SubprocessError, OSError):
                pass

        return None

    def _build_command(
        self,
        resume_session: str | None = None,
        continue_session: bool = False,
        streaming: bool = False,
    ) -> list[str]:
        from shutil import which

        cfg = self._config
        cmd = ["node", self._cli_js] if self._cli_js else [which("gemini") or "gemini"]

        # Aligned with Claude: stream-json for real-time events
        cmd += ["--output-format", "stream-json" if streaming else "json"]
        cmd += ["--include-directories", "."]

        if cfg.model:
            cmd += ["--model", cfg.model]
        if cfg.permission_mode == "bypassPermissions":
            cmd += ["--approval-mode", "yolo"]
        if resume_session:
            cmd += ["--resume", resume_session]
        elif continue_session:
            cmd += ["--resume", "latest"]

        if cfg.allowed_tools:
            cmd += ["--allowed-tools", *cfg.allowed_tools]
        if cfg.cli_parameters:
            cmd.extend(cfg.cli_parameters)

        return cmd

    def _prepare_env(self, system_prompt_path: str | None = None) -> dict[str, str]:
        env = os.environ.copy()
        env["GEMINI_IDE_ENABLED"] = "false"
        if system_prompt_path:
            env["GEMINI_SYSTEM_MD"] = system_prompt_path
        return env

    @contextmanager
    def _system_prompt_file(self) -> Iterator[str | None]:
        sys_p = self._config.system_prompt or ""
        app_p = self._config.append_system_prompt or ""

        with tempfile.NamedTemporaryFile(
            mode="w", suffix=".md", delete=False, encoding="utf-8"
        ) as tf:
            path = tf.name
            tf.write(sys_p + "\n\n" + app_p)

        try:
            yield path
        finally:
            if Path(path).exists():
                Path(path).unlink()

    async def send(
        self,
        prompt: str,
        resume_session: str | None = None,
        continue_session: bool = False,
        timeout_seconds: float | None = None,
    ) -> CLIResponse:
        """Simplified send (non-streaming)."""
        cmd = self._build_command(resume_session, continue_session, streaming=False)
        exec_cmd, use_cwd = docker_wrap(
            cmd, self._config.docker_container, self._config.chat_id, self._working_dir
        )

        process = await asyncio.create_subprocess_exec(
            *exec_cmd,
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            cwd=use_cwd,
        )

        reg = self._config.process_registry
        tracked = (
            reg.register(self._config.chat_id, process, self._config.process_label) if reg else None
        )
        try:
            stdout, stderr = await process.communicate(input=prompt.encode())
        finally:
            if tracked and reg:
                reg.unregister(tracked)
        return _parse_response(stdout, stderr, process.returncode)

    async def send_streaming(  # noqa: C901, PLR0912, PLR0915
        self,
        prompt: str,
        resume_session: str | None = None,
        continue_session: bool = False,
        timeout_seconds: float | None = None,
    ) -> AsyncGenerator[StreamEvent, None]:
        """Claude-style streaming: Single process with robust ask_user handling."""
        cmd = self._build_command(resume_session, continue_session, streaming=True)
        _log_cmd(cmd, streaming=True)

        exec_cmd, use_cwd = docker_wrap(
            cmd, self._config.docker_container, self._config.chat_id, self._working_dir
        )

        with self._system_prompt_file() as sys_path:
            env = self._prepare_env(sys_path)
            process = await asyncio.create_subprocess_exec(
                *exec_cmd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                cwd=use_cwd,
                env=env,
            )

            stderr_task = None
            if process.stderr:
                stderr_task = asyncio.create_task(process.stderr.read())

            reg = self._config.process_registry
            tracked = (
                reg.register(self._config.chat_id, process, self._config.process_label)
                if reg
                else None
            )

            if process.stdin:
                process.stdin.write(prompt.encode())
                await process.stdin.drain()
                process.stdin.close()

            last_session_id = resume_session

            try:
                async with asyncio.timeout(timeout_seconds or _DEFAULT_TIMEOUT):
                    while True:
                        if not process.stdout:
                            break
                        line_b = await process.stdout.readline()
                        if not line_b:
                            break
                        line = line_b.decode(errors="replace").rstrip()
                        if not line:
                            continue

                        logger.info("Gemini raw line: %s", line)

                        for event in parse_gemini_stream_line(line):
                            if (
                                isinstance(event, (ResultEvent, SystemInitEvent))
                                and event.session_id
                            ):
                                last_session_id = event.session_id

                            # Inject session_id if missing (critical for Gemini results)
                            if isinstance(event, ResultEvent) and not event.session_id:
                                event.session_id = last_session_id

                            yield event

            except TimeoutError:
                with contextlib.suppress(OSError):
                    process.kill()
                yield ResultEvent(
                    type="result", result="Timeout", is_error=True, session_id=last_session_id
                )
            finally:
                # Ensure process is terminated if we're exiting
                if process.returncode is None:
                    with contextlib.suppress(OSError):
                        process.kill()

                await process.wait()
                if stderr_task:
                    await stderr_task

                if tracked and reg:
                    reg.unregister(tracked)

                if reg and reg.was_aborted(self._config.chat_id):
                    yield ResultEvent(
                        type="result",
                        result="Process aborted by user.",
                        is_error=True,
                        session_id=last_session_id,
                    )
                elif process.returncode != 0:
                    yield ResultEvent(
                        type="result",
                        result=f"Process exited with code {process.returncode}",
                        is_error=True,
                        session_id=last_session_id,
                    )


def _log_cmd(cmd: list[str], *, streaming: bool = False) -> None:
    safe = [
        (c[:80] + "...") if len(c) > 80 and i > 0 and cmd[i - 1].startswith("--") else c
        for i, c in enumerate(cmd)
    ]
    logger.info("%s: %s", "CLI stream cmd" if streaming else "CLI cmd", " ".join(safe))


def _parse_response(stdout: bytes, stderr: bytes, returncode: int | None) -> CLIResponse:
    stderr_text = stderr.decode(errors="replace")[:2000] if stderr else ""
    raw = stdout.decode().strip()
    if not raw:
        return CLIResponse(result="", is_error=True, returncode=returncode, stderr=stderr_text)
    try:
        data = json.loads(raw)
        res = data.get("response") or data.get("content") or data.get("output") or raw
        sid, stats = data.get("session_id"), data.get("stats", {})
        usage = {
            "input_tokens": stats.get("input_tokens", 0),
            "output_tokens": stats.get("output_tokens", 0),
        }
    except json.JSONDecodeError:
        res, sid, usage = raw, None, {}
    return CLIResponse(
        session_id=sid,
        result=res,
        is_error=returncode != 0,
        returncode=returncode,
        stderr=stderr_text,
        usage=usage,
    )
