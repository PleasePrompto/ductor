"""Direct API server: WebSocket interface for app connections.

Runs alongside the Telegram bot without affecting it.  Designed for use
over Tailscale (default) or other private networks so that no traffic
passes through third-party servers.

WebSocket protocol (E2E encrypted)
-----------------------------------
1. Client connects to ``ws://<host>:<port>/ws``
2. Client sends ``{"type": "auth", "token": "...", "e2e_pk": "<b64>"}``
   (optional ``"chat_id": N`` to override default session)
3. Server responds ``{"type": "auth_ok", "chat_id": N, "e2e_pk": "<b64>", "providers": [...], "active_provider": "...", "active_model": "..."}``
4. All subsequent frames are E2E encrypted: ``base64(nonce_24 + ciphertext)``
5. Encrypted message: ``{"type": "message", "text": "..."}``
   Server streams back encrypted events:
     - ``{"type": "text_delta",     "data": "..."}``
     - ``{"type": "tool_activity",  "data": "..."}``
     - ``{"type": "system_status",  "data": "..."}``
     - ``{"type": "result", "text": "...", "stream_fallback": bool, "files": [...]}``
6. Encrypted abort: ``{"type": "abort"}`` (or ``/stop`` as message text)
   Server responds ``{"type": "abort_ok", "killed": N}``

HTTP endpoints
--------------
- ``GET /health``              -- health check
- ``GET /files?path=<abs>``    -- download a file (Bearer token auth)
- ``POST /upload``             -- upload a file (Bearer token auth, multipart)
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import shutil
from collections.abc import Awaitable, Callable, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

import aiohttp
from aiohttp import BodyPartReader, WSMsgType, web

from ductor_bot.api.crypto import E2ESession
from ductor_bot.bus.lock_pool import LockPool
from ductor_bot.cli.stream_events import ToolUseEvent
from ductor_bot.files.image_processor import process_image
from ductor_bot.files.prompt import MediaInfo, build_media_prompt
from ductor_bot.files.storage import prepare_destination, sanitize_filename
from ductor_bot.files.tags import (
    classify_mime,
    extract_file_paths,
    guess_mime,
    is_image_path,
    path_from_file_tag,
)
from ductor_bot.log_context import set_log_context
from ductor_bot.security.paths import is_path_safe
from ductor_bot.session.key import SessionKey
from ductor_bot.text.response_format import normalize_tool_name

if TYPE_CHECKING:
    from ductor_bot.config import ApiConfig

logger = logging.getLogger(__name__)

# Callback types matching Orchestrator.handle_message_streaming / abort
StreamingMessageHandler = Callable[..., Awaitable[Any]]
AbortHandler = Callable[[int], Awaitable[int]]

_MAX_UPLOAD_BYTES = 50 * 1024 * 1024  # 50 MB


def _detect_tailscale() -> bool:
    """Return True if the ``tailscale`` binary is found in PATH."""
    return shutil.which("tailscale") is not None


async def _ws_send(ws: web.WebSocketResponse, data: dict[str, object]) -> bool:
    """Send plaintext JSON to a WebSocket (auth phase only).  Returns False on disconnect."""
    if ws.closed:
        return False
    try:
        await ws.send_json(data)
    except (ConnectionResetError, ConnectionError):
        return False
    return True


async def _ws_reject(ws: web.WebSocketResponse, code: str, message: str) -> None:
    """Send an error response and close the WebSocket."""
    await _ws_send(ws, {"type": "error", "code": code, "message": message})
    await ws.close()


class _SecureChannel:
    """Encrypted WebSocket channel for post-auth communication."""

    __slots__ = ("_e2e", "ws")

    def __init__(self, ws: web.WebSocketResponse, e2e: E2ESession) -> None:
        self.ws = ws
        self._e2e = e2e

    @property
    def closed(self) -> bool:
        return bool(self.ws.closed)

    async def send(self, data: dict[str, object]) -> bool:
        """Encrypt and send.  Returns False if connection lost."""
        if self.ws.closed:
            return False
        try:
            await self.ws.send_str(self._e2e.encrypt(data))
        except (ConnectionResetError, ConnectionError):
            return False
        return True

    def decrypt(self, frame: str) -> dict[str, Any]:
        """Decrypt an incoming encrypted frame."""
        return self._e2e.decrypt(frame)


class _StreamCallbacks:
    """Streaming callbacks that forward encrypted orchestrator events to a WebSocket."""

    __slots__ = ("channel", "disconnected")

    def __init__(self, channel: _SecureChannel) -> None:
        self.channel = channel
        self.disconnected = False

    async def on_text(self, delta: str) -> None:
        if self.disconnected:
            return
        if not await self.channel.send({"type": "text_delta", "data": delta}):
            self.disconnected = True

    async def on_tool(self, tool: ToolUseEvent | str) -> None:
        if self.disconnected:
            return
        name = normalize_tool_name(str(getattr(tool, "tool_name", tool)))
        if not await self.channel.send({"type": "tool_activity", "data": name}):
            self.disconnected = True

    async def on_system(self, label: str | None) -> None:
        if self.disconnected:
            return
        if not await self.channel.send({"type": "system_status", "data": label}):
            self.disconnected = True


def _parse_file_refs(text: str) -> list[dict[str, object]]:
    """Extract ``<file:...>`` tags and return metadata for the app."""
    refs: list[dict[str, object]] = []
    for fp in extract_file_paths(text):
        p = path_from_file_tag(fp)
        refs.append(
            {
                "path": str(p),
                "name": p.name,
                "is_image": is_image_path(str(p)),
            }
        )
    return refs


class ApiServer:
    """WebSocket API server for direct app connections.

    Provides the same orchestrator access as Telegram, without Telegram.
    All handler wiring is done via setter methods so the server module
    has zero imports from the orchestrator (no coupling).
    """

    def __init__(
        self,
        config: ApiConfig,
        *,
        default_chat_id: int = 0,
        lock_pool: LockPool | None = None,
    ) -> None:
        self._config = config
        self._default_chat_id = default_chat_id
        self._handle_message: StreamingMessageHandler | None = None
        self._handle_abort: AbortHandler | None = None
        self._runner: web.AppRunner | None = None
        self._lock_pool = lock_pool if lock_pool is not None else LockPool()
        self._active_ws: set[web.WebSocketResponse] = set()
        # File context (set via set_file_context)
        self._allowed_roots: Sequence[Path] | None = None
        self._upload_dir: Path | None = None
        self._workspace: Path | None = None
        self._provider_info: list[dict[str, object]] = []
        self._active_state_getter: Callable[[], tuple[str, str]] | None = None

    # -- Handler wiring --------------------------------------------------------

    def set_message_handler(self, handler: StreamingMessageHandler) -> None:
        """Orchestrator.handle_message_streaming (bound method)."""
        self._handle_message = handler

    def set_abort_handler(self, handler: AbortHandler) -> None:
        """Orchestrator.abort (bound method)."""
        self._handle_abort = handler

    def set_file_context(
        self,
        *,
        allowed_roots: Sequence[Path] | None,
        upload_dir: Path,
        workspace: Path,
    ) -> None:
        """Configure file download/upload paths."""
        self._allowed_roots = allowed_roots
        self._upload_dir = upload_dir
        self._workspace = workspace

    def set_provider_info(self, providers: list[dict[str, object]]) -> None:
        """Set the list of authenticated providers for auth_ok responses."""
        self._provider_info = providers

    def set_active_state_getter(self, getter: Callable[[], tuple[str, str]]) -> None:
        """Set a callback that returns (active_provider, active_model)."""
        self._active_state_getter = getter

    # -- Lifecycle -------------------------------------------------------------

    async def start(self) -> None:
        """Create the aiohttp app and start listening."""
        if not _detect_tailscale() and not self._config.allow_public:
            logger.warning(
                "API server: Tailscale NOT detected. Your API may be exposed "
                "to the public internet on %s:%d. Install Tailscale for secure "
                "private networking, or set api.allow_public=true in config to "
                "acknowledge this risk.",
                self._config.host,
                self._config.port,
            )

        app = web.Application(client_max_size=_MAX_UPLOAD_BYTES)
        app.router.add_get("/health", self._handle_health)
        app.router.add_get("/ws", self._handle_websocket)
        app.router.add_get("/files", self._handle_file_download)
        app.router.add_post("/upload", self._handle_file_upload)

        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._config.host, self._config.port)
        await site.start()
        logger.info(
            "API server listening on %s:%d",
            self._config.host,
            self._config.port,
        )

    async def stop(self) -> None:
        """Close all connections and shut down the server."""
        for ws in list(self._active_ws):
            await ws.close(
                code=aiohttp.WSCloseCode.GOING_AWAY,
                message=b"server shutdown",
            )
        self._active_ws.clear()
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        logger.info("API server stopped")

    # -- Bearer token auth for HTTP endpoints ----------------------------------

    def _verify_bearer(self, request: web.Request) -> bool:
        """Check ``Authorization: Bearer <token>`` header."""
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return False
        return hmac.compare_digest(auth[7:], self._config.token)

    # -- HTTP handlers ---------------------------------------------------------

    async def _handle_health(self, _request: web.Request) -> web.Response:
        return web.json_response(
            {
                "status": "ok",
                "connections": len(self._active_ws),
            }
        )

    async def _handle_file_download(self, request: web.Request) -> web.StreamResponse:
        """Serve a file from the filesystem (Bearer token auth, path validation)."""
        if not self._verify_bearer(request):
            return web.json_response({"error": "unauthorized"}, status=401)

        raw_path = request.query.get("path", "")
        if not raw_path:
            return web.json_response({"error": "missing 'path' query parameter"}, status=400)

        file_path = Path(raw_path)
        if self._allowed_roots is not None and not is_path_safe(file_path, self._allowed_roots):
            return web.json_response({"error": "path outside allowed roots"}, status=403)

        if not await asyncio.to_thread(file_path.is_file):
            return web.json_response({"error": "file not found"}, status=404)

        mime = guess_mime(file_path)
        return web.FileResponse(file_path, headers={"Content-Type": mime})

    async def _handle_file_upload(self, request: web.Request) -> web.Response:
        """Accept a multipart file upload and return the saved path + prompt."""
        if not self._verify_bearer(request):
            return web.json_response({"error": "unauthorized"}, status=401)

        if self._upload_dir is None or self._workspace is None:
            return web.json_response({"error": "file uploads not configured"}, status=503)

        try:
            reader = await request.multipart()
        except ValueError:
            return web.json_response({"error": "multipart body required"}, status=400)

        field = await reader.next()
        if not isinstance(field, BodyPartReader) or field.name != "file":
            return web.json_response({"error": "expected a 'file' field"}, status=400)

        raw_name = field.filename or "upload"
        safe_name = sanitize_filename(raw_name)

        dest = await asyncio.to_thread(prepare_destination, self._upload_dir, safe_name)

        total = 0
        with dest.open("wb") as f:
            while True:
                chunk = await field.read_chunk(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > _MAX_UPLOAD_BYTES:
                    dest.unlink(missing_ok=True)
                    return web.json_response(
                        {"error": f"file exceeds {_MAX_UPLOAD_BYTES // (1024 * 1024)} MB limit"},
                        status=413,
                    )
                f.write(chunk)

        # Detect actual MIME from saved file content (magic bytes + extension fallback)
        mime = await asyncio.to_thread(guess_mime, dest)

        dest = await asyncio.to_thread(process_image, dest)
        if dest.suffix == ".webp":
            mime = "image/webp"

        # Read optional caption from a second multipart field
        caption: str | None = None
        next_field = await reader.next()
        if isinstance(next_field, BodyPartReader) and next_field.name == "caption":
            caption = (await next_field.read(decode=True)).decode("utf-8", errors="replace")

        info = MediaInfo(
            path=dest,
            media_type=mime,
            file_name=dest.name,
            caption=caption,
            original_type=classify_mime(mime),
        )
        prompt = build_media_prompt(info, self._workspace, transport="API")

        logger.info("API upload: %s (%s, %d bytes)", dest.name, mime, total)
        return web.json_response(
            {
                "path": str(dest),
                "name": dest.name,
                "mime": mime,
                "size": total,
                "prompt": prompt,
            }
        )

    # -- WebSocket handlers ----------------------------------------------------

    async def _handle_websocket(
        self,
        request: web.Request,
    ) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=30.0)
        await ws.prepare(request)
        logger.info("API WebSocket opened from %s", request.remote)

        auth_result = await self._authenticate(ws)
        if auth_result is None:
            return ws

        key, e2e = auth_result
        channel = _SecureChannel(ws, e2e)

        self._active_ws.add(ws)
        try:
            await self._session_loop(channel, key)
        except asyncio.CancelledError:
            pass
        finally:
            self._active_ws.discard(ws)
            logger.info("API WebSocket closed key=%s", key.storage_key)

        return ws

    # -- Authentication --------------------------------------------------------

    async def _read_auth_message(self, ws: web.WebSocketResponse) -> dict[str, object] | None:
        """Read and validate the initial auth message. Returns parsed data or None."""
        try:
            raw = await asyncio.wait_for(ws.receive(), timeout=10.0)
        except (TimeoutError, asyncio.CancelledError):
            await _ws_reject(ws, "auth_timeout", "No auth message within 10 s")
            return None

        if raw.type != WSMsgType.TEXT:
            await _ws_reject(ws, "auth_required", "First message must be JSON text")
            return None

        try:
            data = json.loads(raw.data)
        except (json.JSONDecodeError, ValueError):
            data = None
        if not isinstance(data, dict) or data.get("type") != "auth":
            await _ws_reject(ws, "auth_required", "First message must be auth JSON")
            return None

        return data

    async def _authenticate(
        self,
        ws: web.WebSocketResponse,
    ) -> tuple[SessionKey, E2ESession] | None:
        """Wait for auth + E2E key exchange.  Returns (key, e2e) or None."""
        data = await self._read_auth_message(ws)
        if data is None:
            return None

        token = str(data.get("token", ""))
        if not hmac.compare_digest(token, self._config.token):
            logger.warning("API auth failed (invalid token)")
            await _ws_reject(ws, "auth_failed", "Invalid token")
            return None

        # E2E key exchange (mandatory)
        e2e = E2ESession()
        e2e_pk = data.get("e2e_pk")
        e2e_valid = isinstance(e2e_pk, str) and bool(e2e_pk)
        if e2e_valid:
            try:
                assert isinstance(e2e_pk, str)
                e2e.set_remote_key(e2e_pk)
            except Exception:
                e2e_valid = False
        if not e2e_valid:
            await _ws_reject(ws, "auth_failed", "e2e_pk required or invalid")
            return None

        chat_id = data.get("chat_id", self._default_chat_id)
        if not isinstance(chat_id, int) or chat_id <= 0:
            chat_id = self._default_chat_id

        # Optional channel_id for per-channel session isolation (maps to topic_id)
        channel_id = data.get("channel_id")
        if not isinstance(channel_id, int) or channel_id <= 0:
            channel_id = None

        key = SessionKey(chat_id=chat_id, topic_id=channel_id)

        # Last plaintext message -- everything after this is E2E encrypted
        auth_ok_payload: dict[str, object] = {
            "type": "auth_ok",
            "chat_id": chat_id,
            "e2e_pk": e2e.local_pk_b64,
            "providers": self._provider_info,
        }
        if channel_id is not None:
            auth_ok_payload["channel_id"] = channel_id
        if self._active_state_getter:
            active_provider, active_model = self._active_state_getter()
            auth_ok_payload["active_provider"] = active_provider
            auth_ok_payload["active_model"] = active_model
        await _ws_send(ws, auth_ok_payload)
        logger.info("API client authenticated key=%s (E2E)", key.storage_key)
        return key, e2e

    # -- Session loop ----------------------------------------------------------

    async def _session_loop(
        self,
        channel: _SecureChannel,
        key: SessionKey,
    ) -> None:
        """Read encrypted messages from the client and dispatch them sequentially."""
        lock = self._lock_pool.get(key.lock_key)
        set_log_context(operation="api", chat_id=key.chat_id)

        async for raw in channel.ws:
            if raw.type == WSMsgType.TEXT:
                await self._route_text_message(channel, raw.data, key, lock)
            elif raw.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                break

    async def _route_text_message(
        self,
        channel: _SecureChannel,
        raw_data: str,
        key: SessionKey,
        lock: asyncio.Lock,
    ) -> None:
        """Decrypt and route a single encrypted text frame."""
        try:
            data = channel.decrypt(raw_data)
        except Exception:
            logger.warning("E2E decryption failed key=%s", key.storage_key)
            await channel.send(
                {
                    "type": "error",
                    "code": "decrypt_failed",
                    "message": "Decryption failed",
                },
            )
            return

        msg_type = str(data.get("type", ""))

        if msg_type == "message":
            text = str(data.get("text", "")).strip()
            if not text:
                await channel.send(
                    {
                        "type": "error",
                        "code": "empty",
                        "message": "Empty message",
                    },
                )
                return
            # Intercept /stop since the orchestrator doesn't handle it
            if text.lower() == "/stop":
                await self._dispatch_abort(channel, key.chat_id)
                return
            async with lock:
                set_log_context(operation="api", chat_id=key.chat_id)
                await self._dispatch_message(channel, key, text)

        elif msg_type == "abort":
            await self._dispatch_abort(channel, key.chat_id)

        else:
            await channel.send(
                {
                    "type": "error",
                    "code": "unknown_type",
                    "message": f"Unknown message type: {msg_type}",
                },
            )

    # -- Dispatch --------------------------------------------------------------

    async def _dispatch_message(
        self,
        channel: _SecureChannel,
        key: SessionKey,
        text: str,
    ) -> None:
        """Route a message through the orchestrator with encrypted streaming callbacks."""
        if not self._handle_message:
            await channel.send(
                {
                    "type": "error",
                    "code": "no_handler",
                    "message": "Message handler not configured",
                },
            )
            return

        callbacks = _StreamCallbacks(channel)
        result = await self._execute_streaming(key, text, callbacks)
        if result is None:
            return

        if callbacks.disconnected:
            logger.info(
                "API client disconnected mid-stream, aborting key=%s",
                key.storage_key,
            )
            if self._handle_abort:
                await self._handle_abort(key.chat_id)
            return

        # Parse file references from the response for the app
        files = _parse_file_refs(result.text)

        await channel.send(
            {
                "type": "result",
                "text": result.text,
                "stream_fallback": result.stream_fallback,
                "files": files,
            },
        )

    async def _execute_streaming(
        self,
        key: SessionKey,
        text: str,
        callbacks: _StreamCallbacks,
    ) -> Any:
        """Call the orchestrator handler, return result or None on error."""
        assert self._handle_message is not None
        try:
            return await self._handle_message(
                key,
                text,
                on_text_delta=callbacks.on_text,
                on_tool_activity=callbacks.on_tool,
                on_system_status=callbacks.on_system,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("API dispatch error key=%s", key.storage_key)
            await callbacks.channel.send(
                {
                    "type": "error",
                    "code": "internal_error",
                    "message": "An internal error occurred",
                },
            )
            return None

    async def _dispatch_abort(
        self,
        channel: _SecureChannel,
        chat_id: int,
    ) -> None:
        """Abort running CLI processes for this chat."""
        killed = 0
        if self._handle_abort:
            killed = await self._handle_abort(chat_id)
        await channel.send({"type": "abort_ok", "killed": killed})
