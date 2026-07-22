"""Tests for multiagent/internal_api.py: InternalAgentAPI HTTP endpoints."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest
from aiohttp.test_utils import TestClient

from ductor_bot.multiagent.bus import InterAgentBus
from ductor_bot.multiagent.health import AgentHealth
from ductor_bot.multiagent.internal_api import InternalAgentAPI, _normalise_transport


@pytest.fixture
def bus() -> InterAgentBus:
    return InterAgentBus()


@pytest.fixture
def api(bus: InterAgentBus) -> InternalAgentAPI:
    return InternalAgentAPI(bus, port=0)


@pytest.fixture
async def client(api: InternalAgentAPI) -> TestClient:
    """Create aiohttp test client for the internal API."""
    from aiohttp.test_utils import TestServer

    server = TestServer(api._app)
    c = TestClient(server)
    await c.start_server()
    yield c
    await c.close()


class TestHandleSend:
    """Test POST /interagent/send."""

    async def test_send_success(self, client: TestClient, bus: InterAgentBus) -> None:
        stack = MagicMock()
        stack.bot.orchestrator = MagicMock()
        stack.bot.orchestrator.handle_interagent_message = AsyncMock(
            return_value=("OK", "ia-sender", "")
        )
        bus.register("target", stack)

        resp = await client.post(
            "/interagent/send",
            json={"from": "sender", "to": "target", "message": "Hello"},
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["success"] is True
        assert data["text"] == "OK"

    async def test_send_missing_fields(self, client: TestClient) -> None:
        resp = await client.post(
            "/interagent/send",
            json={"from": "sender"},
        )
        assert resp.status == 400
        data = await resp.json()
        assert data["success"] is False
        assert "Missing" in data["error"]

    async def test_send_invalid_json(self, client: TestClient) -> None:
        resp = await client.post(
            "/interagent/send",
            data=b"not json",
            headers={"Content-Type": "application/json"},
        )
        assert resp.status == 400

    async def test_send_unknown_recipient(self, client: TestClient) -> None:
        resp = await client.post(
            "/interagent/send",
            json={"from": "sender", "to": "nonexistent", "message": "Hello"},
        )
        data = await resp.json()
        assert data["success"] is False
        assert "not found" in data["error"]


class TestHandleSendAsync:
    """Test POST /interagent/send_async."""

    async def test_send_async_success(self, client: TestClient, bus: InterAgentBus) -> None:
        stack = MagicMock()
        stack.bot.orchestrator = MagicMock()
        stack.bot.orchestrator.handle_interagent_message = AsyncMock(
            return_value=("OK", "ia-sender", "")
        )
        bus.register("target", stack)

        resp = await client.post(
            "/interagent/send_async",
            json={"from": "sender", "to": "target", "message": "Hello"},
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["success"] is True
        assert "task_id" in data

    def test_normalise_transport_aliases(self) -> None:
        assert _normalise_transport("telegram") == "tg"
        assert _normalise_transport("matrix") == "mx"
        assert _normalise_transport("mx") == "mx"

    async def test_send_async_unknown_recipient(self, client: TestClient) -> None:
        resp = await client.post(
            "/interagent/send_async",
            json={"from": "sender", "to": "nonexistent", "message": "Hello"},
        )
        data = await resp.json()
        assert data["success"] is False
        assert "not found" in data["error"]

    async def test_send_async_missing_fields(self, client: TestClient) -> None:
        resp = await client.post(
            "/interagent/send_async",
            json={"from": "sender"},
        )
        assert resp.status == 400


class TestNewSessionFlag:
    """Test new_session flag in /interagent/send and /interagent/send_async."""

    async def test_send_passes_new_session_true(
        self, client: TestClient, bus: InterAgentBus
    ) -> None:
        stack = MagicMock()
        stack.bot.orchestrator = MagicMock()
        stack.bot.orchestrator.handle_interagent_message = AsyncMock(
            return_value=("OK", "ia-sender", "")
        )
        bus.register("target", stack)

        resp = await client.post(
            "/interagent/send",
            json={
                "from": "sender",
                "to": "target",
                "message": "Hello",
                "new_session": True,
                "chat_id": 777,
                "topic_id": 10,
            },
        )
        assert resp.status == 200
        stack.bot.orchestrator.handle_interagent_message.assert_awaited_once_with(
            "sender",
            "Hello",
            new_session=True,
            source_chat_id=777,
            source_topic_id=10,
        )

    async def test_send_defaults_new_session_false(
        self, client: TestClient, bus: InterAgentBus
    ) -> None:
        stack = MagicMock()
        stack.bot.orchestrator = MagicMock()
        stack.bot.orchestrator.handle_interagent_message = AsyncMock(
            return_value=("OK", "ia-sender", "")
        )
        bus.register("target", stack)

        resp = await client.post(
            "/interagent/send",
            json={"from": "sender", "to": "target", "message": "Hello"},
        )
        assert resp.status == 200
        stack.bot.orchestrator.handle_interagent_message.assert_awaited_once_with(
            "sender",
            "Hello",
            new_session=False,
        )

    async def test_send_async_passes_new_session(
        self, client: TestClient, bus: InterAgentBus
    ) -> None:
        stack = MagicMock()
        stack.bot.orchestrator = MagicMock()
        stack.bot.orchestrator.handle_interagent_message = AsyncMock(
            return_value=("OK", "ia-sender", "")
        )
        bus.register("target", stack)

        resp = await client.post(
            "/interagent/send_async",
            json={
                "from": "sender",
                "to": "target",
                "message": "Hello",
                "new_session": True,
            },
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["success"] is True


class TestHandleList:
    """Test GET /interagent/agents."""

    async def test_list_empty(self, client: TestClient) -> None:
        resp = await client.get("/interagent/agents")
        assert resp.status == 200
        data = await resp.json()
        assert data["agents"] == []

    async def test_list_with_agents(self, client: TestClient, bus: InterAgentBus) -> None:
        bus.register("main", MagicMock())
        bus.register("sub1", MagicMock())

        resp = await client.get("/interagent/agents")
        data = await resp.json()
        assert set(data["agents"]) == {"main", "sub1"}


class TestHandleHealth:
    """Test GET /interagent/health."""

    async def test_health_no_ref(self, client: TestClient) -> None:
        resp = await client.get("/interagent/health")
        data = await resp.json()
        assert data["agents"] == {}

    async def test_health_with_agents(self, client: TestClient, api: InternalAgentAPI) -> None:
        h = AgentHealth(name="main")
        h.mark_running()
        api.set_health_ref({"main": h})

        resp = await client.get("/interagent/health")
        data = await resp.json()
        assert "main" in data["agents"]
        assert data["agents"]["main"]["status"] == "running"
        assert data["agents"]["main"]["restart_count"] == 0

    async def test_health_crashed_agent(self, client: TestClient, api: InternalAgentAPI) -> None:
        h = AgentHealth(name="sub1")
        h.mark_crashed("OOM")
        api.set_health_ref({"sub1": h})

        resp = await client.get("/interagent/health")
        data = await resp.json()
        assert data["agents"]["sub1"]["status"] == "crashed"
        assert data["agents"]["sub1"]["last_crash_error"] == "OOM"
        assert data["agents"]["sub1"]["restart_count"] == 1


class TestHandleTaskCancel:
    async def test_cancel_logs_sender(
        self,
        client: TestClient,
        api: InternalAgentAPI,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        hub = MagicMock()
        hub.cancel = AsyncMock(return_value=True)
        hub.registry.get.return_value = MagicMock(parent_agent="main")
        api.set_task_hub(hub)
        caplog.set_level("INFO", logger="ductor_bot.multiagent.internal_api")

        resp = await client.post(
            "/tasks/cancel",
            json={"task_id": "task-123", "from": "main"},
        )

        assert resp.status == 200
        data = await resp.json()
        assert data["success"] is True
        hub.cancel.assert_awaited_once_with("task-123")

        messages = "\n".join(record.getMessage() for record in caplog.records)
        assert "Task cancel via API id=task-123 from=main success=True" in messages


class TestLifecycle:
    """Test InternalAgentAPI lifecycle return values."""

    async def test_start_returns_false_on_bind_error(self, api: InternalAgentAPI) -> None:
        from unittest.mock import patch

        with patch(
            "aiohttp.web.TCPSite.start",
            new_callable=AsyncMock,
            side_effect=OSError("bind failed"),
        ):
            started = await api.start()

        assert started is False
