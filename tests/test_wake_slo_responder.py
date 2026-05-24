"""Tests for the recipient-side Wake SLO responder.

Network calls are mocked; the tests exercise the regex gates, the
fall-through behavior, and the JSON-RPC payload shaping.
"""

from __future__ import annotations

import json
from typing import Any
from unittest import mock

import pytest

from ductor_bot import wake_slo_responder


def _probe_payload(topic: str, body: str, **extra: Any) -> dict[str, Any]:
    return {
        "event_type": "agentcomm_wake",
        "from_agent": "corsair-main",
        "to_agent": "corsair-main",
        "session_id": "01KSBN258GN4PSMA0679ST6CZV",
        "message_id": "01KSBN258H6CM8Z92FWBY4D9AA",
        "topic": topic,
        "body": body,
        "created_at": "2026-05-24T00:30:00Z",
        **extra,
    }


def test_is_probe_payload_matches_canonical() -> None:
    p = _probe_payload(
        "WAKE_SLO_PROBE_C2L_1779582600000",
        "WAKE_SLO_PING 2026-05-24T00:30:00.000Z",
    )
    assert wake_slo_responder.is_probe_payload(p) is True


def test_is_probe_payload_rejects_non_wake_event_type() -> None:
    p = _probe_payload(
        "WAKE_SLO_PROBE_C2L_1779582600000",
        "WAKE_SLO_PING 2026-05-24T00:30:00.000Z",
    )
    p["event_type"] = "something_else"
    assert wake_slo_responder.is_probe_payload(p) is False


def test_is_probe_payload_rejects_non_probe_topic() -> None:
    p = _probe_payload(
        "SMOKE_unrelated",
        "WAKE_SLO_PING 2026-05-24T00:30:00.000Z",
    )
    assert wake_slo_responder.is_probe_payload(p) is False


def test_is_probe_payload_rejects_non_probe_body() -> None:
    p = _probe_payload(
        "WAKE_SLO_PROBE_C2L_1779582600000",
        "HELLO",
    )
    assert wake_slo_responder.is_probe_payload(p) is False


def test_respond_no_api_key_returns_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wake_slo_responder, "_read_api_key", lambda: None)
    out = wake_slo_responder.respond(
        _probe_payload(
            "WAKE_SLO_PROBE_C2L_1779582600000",
            "WAKE_SLO_PING 2026-05-24T00:30:00.000Z",
        )
    )
    assert out == {"status": "error:no_api_key"}


def test_respond_calls_ack_then_reply(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wake_slo_responder, "_read_api_key", lambda: "fake-key")

    calls: list[tuple[str, str, dict[str, Any], str]] = []

    def fake_mcp_call(
        method: str, name: str, arguments: dict[str, Any], api_key: str
    ) -> dict[str, Any]:
        calls.append((method, name, arguments, api_key))
        if name == "agent_ack":
            return {"jsonrpc": "2.0", "id": 1, "result": {"acked_at": "..."}}
        if name == "agent_reply":
            return {"jsonrpc": "2.0", "id": 1, "result": {"closed": True}}
        raise AssertionError(f"unexpected tool call: {name}")

    monkeypatch.setattr(wake_slo_responder, "_mcp_call", fake_mcp_call)

    out = wake_slo_responder.respond(
        _probe_payload(
            "WAKE_SLO_PROBE_L2C_1779582600000",
            "WAKE_SLO_PING 2026-05-24T00:30:00.000Z",
        )
    )
    assert out["status"] == "ok"
    assert out["pong_ts"] == "2026-05-24T00:30:00.000Z"
    assert len(calls) == 2
    assert calls[0][1] == "agent_ack"
    assert calls[0][2] == {
        "message_id": "01KSBN258H6CM8Z92FWBY4D9AA",
        "status": "auto_pong",
    }
    assert calls[1][1] == "agent_reply"
    assert calls[1][2] == {
        "session_id": "01KSBN258GN4PSMA0679ST6CZV",
        "body": "WAKE_SLO_PONG 2026-05-24T00:30:00.000Z",
        "close": True,
    }


def test_respond_ack_failure_short_circuits(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(wake_slo_responder, "_read_api_key", lambda: "fake-key")

    def fake_mcp_call(
        method: str, name: str, arguments: dict[str, Any], api_key: str
    ) -> dict[str, Any]:
        if name == "agent_ack":
            return {"error": {"code": -32603, "message": "boom"}}
        raise AssertionError(f"should not reach: {name}")

    monkeypatch.setattr(wake_slo_responder, "_mcp_call", fake_mcp_call)

    out = wake_slo_responder.respond(
        _probe_payload(
            "WAKE_SLO_PROBE_C2L_1779582600000",
            "WAKE_SLO_PING 2026-05-24T00:30:00.000Z",
        )
    )
    assert out["status"] == "error:ack_failed"


def test_respond_network_error_returns_error_status(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(wake_slo_responder, "_read_api_key", lambda: "fake-key")

    def fake_mcp_call(*args: Any, **kwargs: Any) -> dict[str, Any]:
        from urllib import error as urllib_error

        raise urllib_error.URLError("connection refused")

    monkeypatch.setattr(wake_slo_responder, "_mcp_call", fake_mcp_call)

    out = wake_slo_responder.respond(
        _probe_payload(
            "WAKE_SLO_PROBE_C2L_1779582600000",
            "WAKE_SLO_PING 2026-05-24T00:30:00.000Z",
        )
    )
    assert out["status"] == "error:network"
    assert "connection refused" in out["detail"]


def test_mcp_call_parses_sse_framed_response(monkeypatch: pytest.MonkeyPatch) -> None:
    sse_body = (
        b"event: message\n"
        b'data: {"jsonrpc":"2.0","id":1,"result":{"ok":true}}\n'
        b"\n"
    )

    class FakeResponse:
        def __init__(self, body: bytes) -> None:
            self._body = body

        def read(self) -> bytes:
            return self._body

        def __enter__(self) -> "FakeResponse":
            return self

        def __exit__(self, *args: Any) -> None:
            pass

    fake_urlopen = mock.Mock(return_value=FakeResponse(sse_body))
    monkeypatch.setattr(wake_slo_responder.urllib_request, "urlopen", fake_urlopen)

    parsed = wake_slo_responder._mcp_call(
        "tools/call", "agent_status", {}, "fake-key"
    )
    assert parsed["result"] == {"ok": True}
    fake_urlopen.assert_called_once()
    req = fake_urlopen.call_args.args[0]
    assert req.method == "POST"
    sent = json.loads(req.data.decode())
    assert sent["method"] == "tools/call"
    assert sent["params"]["name"] == "agent_status"
