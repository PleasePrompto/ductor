"""Integration tests for E2E encrypted WebSocket protocol.

Tests the real handshake, key exchange, and encrypted message flow
using an actual aiohttp server + WebSocket client.  No mocking of
crypto primitives -- all encryption/decryption is real.
"""

from __future__ import annotations

import base64
import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

pytest.importorskip("nacl", reason="PyNaCl not installed (optional: pip install ductor[api])")

from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer
from nacl.exceptions import CryptoError

from ductor_bot.api.crypto import E2ESession
from ductor_bot.api.server import ApiServer
from ductor_bot.cli.stream_events import ToolUseEvent
from ductor_bot.config import ApiConfig

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_DEFAULT_TOKEN = "test-token"


def _make_server(
    tmp_path: Path,
    *,
    token: str = _DEFAULT_TOKEN,
    default_chat_id: int = 42,
    message_handler: AsyncMock | None = None,
    abort_handler: AsyncMock | None = None,
) -> ApiServer:
    config = ApiConfig(
        enabled=True,
        host="127.0.0.1",
        port=0,
        token=token,
        allow_public=True,
    )
    server = ApiServer(config, default_chat_id=default_chat_id)
    server.set_message_handler(
        message_handler
        or AsyncMock(return_value=SimpleNamespace(text="ok", stream_fallback=False)),
    )
    server.set_abort_handler(abort_handler or AsyncMock(return_value=0))
    upload = tmp_path / "uploads"
    upload.mkdir()
    ws_dir = tmp_path / "workspace"
    ws_dir.mkdir()
    server.set_file_context(allowed_roots=[tmp_path], upload_dir=upload, workspace=ws_dir)
    return server


def _build_app(server: ApiServer) -> web.Application:
    app = web.Application(client_max_size=50 * 1024 * 1024)
    app.router.add_get("/ws", server._handle_websocket)
    app.router.add_get("/health", server._handle_health)
    return app


async def _do_handshake(
    ws: Any,
    token: str = _DEFAULT_TOKEN,
    chat_id: int | None = None,
) -> tuple[E2ESession, dict[str, Any]]:
    """Perform E2E handshake.  Returns (client_e2e, auth_ok_data)."""
    client = E2ESession()
    auth_msg: dict[str, Any] = {"type": "auth", "token": token, "e2e_pk": client.local_pk_b64}
    if chat_id is not None:
        auth_msg["chat_id"] = chat_id
    await ws.send_json(auth_msg)
    resp = await ws.receive_json()
    assert resp["type"] == "auth_ok"
    client.set_remote_key(resp["e2e_pk"])
    return client, resp


async def _send_encrypted(ws: Any, e2e: E2ESession, data: dict[str, Any]) -> None:
    await ws.send_str(e2e.encrypt(data))


async def _recv_encrypted(ws: Any, e2e: E2ESession) -> dict[str, Any]:
    msg = await ws.receive()
    assert msg.type == WSMsgType.TEXT
    return e2e.decrypt(msg.data)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
async def api_ws(tmp_path: Path):
    """Yield (aiohttp_client, api_server) for WebSocket tests."""
    server = _make_server(tmp_path)
    app = _build_app(server)
    srv = TestServer(app)
    client = TestClient(srv)
    await client.start_server()
    yield client, server
    await client.close()


# ---------------------------------------------------------------------------
# Auth + E2E handshake tests
# ---------------------------------------------------------------------------


class TestE2EHandshake:
    async def test_successful_handshake(self, api_ws: tuple[TestClient, ApiServer]) -> None:
        client, _ = api_ws
        ws = await client.ws_connect("/ws")
        _e2e, resp = await _do_handshake(ws)
        assert resp["chat_id"] == 42
        assert "e2e_pk" in resp
        pk_bytes = base64.b64decode(resp["e2e_pk"])
        assert len(pk_bytes) == 32
        await ws.close()

    async def test_auth_ok_includes_providers(
        self,
        api_ws: tuple[TestClient, ApiServer],
    ) -> None:
        client, server = api_ws
        server.set_provider_info(
            [
                {
                    "id": "claude",
                    "name": "Claude Code",
                    "color": "#F97316",
                    "models": ["haiku", "sonnet", "opus"],
                },
            ]
        )
        server.set_active_state_getter(lambda: ("sonnet", "claude"))
        ws = await client.ws_connect("/ws")
        _e2e, resp = await _do_handshake(ws)
        assert resp["providers"] == [
            {
                "id": "claude",
                "name": "Claude Code",
                "color": "#F97316",
                "models": ["haiku", "sonnet", "opus"],
            },
        ]
        assert resp["active_provider"] == "sonnet"
        assert resp["active_model"] == "claude"
        await ws.close()

    async def test_auth_ok_without_providers_has_empty_list(
        self,
        api_ws: tuple[TestClient, ApiServer],
    ) -> None:
        client, _ = api_ws
        ws = await client.ws_connect("/ws")
        _e2e, resp = await _do_handshake(ws)
        assert resp["providers"] == []
        assert "active_provider" not in resp
        await ws.close()

    async def test_custom_chat_id(self, api_ws: tuple[TestClient, ApiServer]) -> None:
        client, _ = api_ws
        ws = await client.ws_connect("/ws")
        _, resp = await _do_handshake(ws, chat_id=999)
        assert resp["chat_id"] == 999
        await ws.close()

    async def test_missing_e2e_pk_rejected(self, api_ws: tuple[TestClient, ApiServer]) -> None:
        client, _ = api_ws
        ws = await client.ws_connect("/ws")
        await ws.send_json({"type": "auth", "token": _DEFAULT_TOKEN})
        resp = await ws.receive_json()
        assert resp["type"] == "error"
        assert resp["code"] == "auth_failed"
        assert "e2e_pk" in resp["message"]

    async def test_invalid_e2e_pk_rejected(self, api_ws: tuple[TestClient, ApiServer]) -> None:
        client, _ = api_ws
        ws = await client.ws_connect("/ws")
        await ws.send_json(
            {"type": "auth", "token": _DEFAULT_TOKEN, "e2e_pk": "not-valid-base64!!"},
        )
        resp = await ws.receive_json()
        assert resp["type"] == "error"
        assert resp["code"] == "auth_failed"

    async def test_wrong_token_rejected(self, api_ws: tuple[TestClient, ApiServer]) -> None:
        client, _ = api_ws
        ws = await client.ws_connect("/ws")
        e2e = E2ESession()
        await ws.send_json({"type": "auth", "token": "wrong", "e2e_pk": e2e.local_pk_b64})
        resp = await ws.receive_json()
        assert resp["type"] == "error"
        assert resp["code"] == "auth_failed"

    async def test_auth_timeout(self, tmp_path: Path) -> None:
        """If client sends nothing within timeout, server closes with auth_timeout."""
        import asyncio

        config = ApiConfig(
            enabled=True,
            host="127.0.0.1",
            port=0,
            token="tok",
            allow_public=True,
        )
        server = ApiServer(config, default_chat_id=1)
        server.set_message_handler(AsyncMock())
        server.set_abort_handler(AsyncMock(return_value=0))

        original_wait_for = asyncio.wait_for

        async def _patched_wait_for(coro: Any, *, timeout: float) -> Any:  # noqa: ARG001, ASYNC109
            return await original_wait_for(coro, timeout=0.1)

        app = _build_app(server)
        srv = TestServer(app)
        tc = TestClient(srv)
        await tc.start_server()
        with patch("ductor_bot.api.server.asyncio.wait_for", side_effect=_patched_wait_for):
            ws = await tc.ws_connect("/ws")
            resp = await ws.receive_json()
            assert resp["type"] == "error"
            assert resp["code"] == "auth_timeout"
        await tc.close()


# ---------------------------------------------------------------------------
# Encrypted message flow tests
# ---------------------------------------------------------------------------


class TestEncryptedMessages:
    async def test_send_message_and_receive_result(
        self,
        api_ws: tuple[TestClient, ApiServer],
    ) -> None:
        client, _ = api_ws
        ws = await client.ws_connect("/ws")
        e2e, _ = await _do_handshake(ws)

        await _send_encrypted(ws, e2e, {"type": "message", "text": "hello"})
        result = await _recv_encrypted(ws, e2e)

        assert result["type"] == "result"
        assert result["text"] == "ok"
        assert result["stream_fallback"] is False
        await ws.close()

    async def test_streaming_callbacks_encrypted(self, tmp_path: Path) -> None:
        """Verify text_delta, tool_activity, system_status are all encrypted."""
        events: list[dict[str, Any]] = []

        async def fake_handler(
            _chat_id: int,
            _text: str,
            *,
            on_text_delta: Any,
            on_tool_activity: Any,
            on_system_status: Any,
        ) -> SimpleNamespace:
            await on_system_status("Thinking")
            await on_tool_activity(
                ToolUseEvent(
                    type="assistant",
                    tool_name="WebFetch",
                    parameters={"url": "https://slack.dev/slack-thinking-steps-ai-agents/"},
                )
            )
            await on_text_delta("chunk1")
            await on_text_delta("chunk2")
            return SimpleNamespace(text="chunk1chunk2", stream_fallback=False)

        server = _make_server(tmp_path, message_handler=AsyncMock(side_effect=fake_handler))
        app = _build_app(server)
        srv = TestServer(app)
        tc = TestClient(srv)
        await tc.start_server()

        ws = await tc.ws_connect("/ws")
        e2e, _ = await _do_handshake(ws)

        await _send_encrypted(ws, e2e, {"type": "message", "text": "test"})

        # Collect all events until result
        while True:
            msg = await _recv_encrypted(ws, e2e)
            events.append(msg)
            if msg["type"] == "result":
                break

        types = [e["type"] for e in events]
        assert "system_status" in types
        assert "tool_activity" in types
        assert "text_delta" in types
        assert types[-1] == "result"
        tool_events = [e["data"] for e in events if e["type"] == "tool_activity"]
        assert tool_events == ["Web fetch"]

        deltas = [e["data"] for e in events if e["type"] == "text_delta"]
        assert deltas == ["chunk1", "chunk2"]
        await ws.close()
        await tc.close()

    async def test_abort_encrypted(self, api_ws: tuple[TestClient, ApiServer]) -> None:
        client, _ = api_ws
        ws = await client.ws_connect("/ws")
        e2e, _ = await _do_handshake(ws)

        await _send_encrypted(ws, e2e, {"type": "abort"})
        resp = await _recv_encrypted(ws, e2e)
        assert resp["type"] == "abort_ok"
        assert resp["killed"] == 0
        await ws.close()

    async def test_stop_command_triggers_abort(
        self,
        api_ws: tuple[TestClient, ApiServer],
    ) -> None:
        client, _ = api_ws
        ws = await client.ws_connect("/ws")
        e2e, _ = await _do_handshake(ws)

        await _send_encrypted(ws, e2e, {"type": "message", "text": "/stop"})
        resp = await _recv_encrypted(ws, e2e)
        assert resp["type"] == "abort_ok"
        await ws.close()

    async def test_empty_message_returns_encrypted_error(
        self,
        api_ws: tuple[TestClient, ApiServer],
    ) -> None:
        client, _ = api_ws
        ws = await client.ws_connect("/ws")
        e2e, _ = await _do_handshake(ws)

        await _send_encrypted(ws, e2e, {"type": "message", "text": ""})
        resp = await _recv_encrypted(ws, e2e)
        assert resp["type"] == "error"
        assert resp["code"] == "empty"
        await ws.close()

    async def test_unknown_type_returns_encrypted_error(
        self,
        api_ws: tuple[TestClient, ApiServer],
    ) -> None:
        client, _ = api_ws
        ws = await client.ws_connect("/ws")
        e2e, _ = await _do_handshake(ws)

        await _send_encrypted(ws, e2e, {"type": "unknown_cmd"})
        resp = await _recv_encrypted(ws, e2e)
        assert resp["type"] == "error"
        assert resp["code"] == "unknown_type"
        await ws.close()

    async def test_bad_ciphertext_returns_decrypt_error(
        self,
        api_ws: tuple[TestClient, ApiServer],
    ) -> None:
        client, _ = api_ws
        ws = await client.ws_connect("/ws")
        e2e, _ = await _do_handshake(ws)

        # Send garbage base64 that won't decrypt
        await ws.send_str(base64.b64encode(b"not-real-ciphertext-at-all-" * 3).decode())
        resp = await _recv_encrypted(ws, e2e)
        assert resp["type"] == "error"
        assert resp["code"] == "decrypt_failed"
        await ws.close()

    async def test_plaintext_json_rejected_after_auth(
        self,
        api_ws: tuple[TestClient, ApiServer],
    ) -> None:
        """After auth, plaintext JSON should fail decryption."""
        client, _ = api_ws
        ws = await client.ws_connect("/ws")
        e2e, _ = await _do_handshake(ws)

        await ws.send_str(json.dumps({"type": "message", "text": "hi"}))
        resp = await _recv_encrypted(ws, e2e)
        assert resp["type"] == "error"
        assert resp["code"] == "decrypt_failed"
        await ws.close()


# ---------------------------------------------------------------------------
# Cross-session isolation
# ---------------------------------------------------------------------------


class TestSessionIsolation:
    async def test_different_sessions_different_keys(self, tmp_path: Path) -> None:
        """Two clients get independent E2E sessions with different keys."""
        server = _make_server(tmp_path)
        app = _build_app(server)
        srv = TestServer(app)
        tc = TestClient(srv)
        await tc.start_server()

        ws1 = await tc.ws_connect("/ws")
        e2e1, resp1 = await _do_handshake(ws1, chat_id=1)

        ws2 = await tc.ws_connect("/ws")
        e2e2, resp2 = await _do_handshake(ws2, chat_id=2)

        # Server generates different keypairs per connection
        assert resp1["e2e_pk"] != resp2["e2e_pk"]

        # Each session works independently
        await _send_encrypted(ws1, e2e1, {"type": "message", "text": "from 1"})
        r1 = await _recv_encrypted(ws1, e2e1)
        assert r1["type"] == "result"

        await _send_encrypted(ws2, e2e2, {"type": "message", "text": "from 2"})
        r2 = await _recv_encrypted(ws2, e2e2)
        assert r2["type"] == "result"

        # Cross-session decryption must fail: client2 cannot read client1's traffic
        await _send_encrypted(ws1, e2e1, {"type": "abort"})
        raw_msg = await ws1.receive()
        with pytest.raises(CryptoError):
            e2e2.decrypt(raw_msg.data)

        await ws1.close()
        await ws2.close()
        await tc.close()


# ---------------------------------------------------------------------------
# File reference tests (encrypted result with file refs)
# ---------------------------------------------------------------------------


class TestEncryptedFileRefs:
    async def test_file_refs_in_encrypted_result(self, tmp_path: Path) -> None:
        handler = AsyncMock(
            return_value=SimpleNamespace(
                text="Here is the file <file:/tmp/chart.png>",
                stream_fallback=False,
            ),
        )
        server = _make_server(tmp_path, message_handler=handler)
        app = _build_app(server)
        srv = TestServer(app)
        tc = TestClient(srv)
        await tc.start_server()

        ws = await tc.ws_connect("/ws")
        e2e, _ = await _do_handshake(ws)
        await _send_encrypted(ws, e2e, {"type": "message", "text": "make chart"})
        result = await _recv_encrypted(ws, e2e)

        assert result["type"] == "result"
        assert len(result["files"]) == 1
        assert result["files"][0]["name"] == "chart.png"
        assert result["files"][0]["is_image"] is True
        await ws.close()
        await tc.close()
