"""Tests for task HTTP endpoints on InternalAgentAPI."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import AsyncMock, MagicMock

import pytest

from ductor_bot.multiagent.internal_api import InternalAgentAPI

if TYPE_CHECKING:
    from aiohttp.test_utils import TestClient


def _make_task_hub(
    *,
    submit_returns: str = "abc123",
    list_returns: list[object] | None = None,
    question_answer: str = "Yes, use HTML",
    cancel_returns: bool = True,
) -> MagicMock:
    hub = MagicMock()
    hub.submit = MagicMock(return_value=submit_returns)
    hub.forward_question = AsyncMock(return_value=question_answer)
    hub.cancel = AsyncMock(return_value=cancel_returns)

    reg = MagicMock()
    reg.list_all.return_value = list_returns or []
    hub.registry = reg
    return hub


@pytest.fixture
async def api_client(aiohttp_client: object) -> TestClient:
    """Create test client with task-only API (no bus)."""
    api = InternalAgentAPI(bus=None, port=0)
    hub = _make_task_hub()
    api.set_task_hub(hub)
    api._app["_test_hub"] = hub  # Stash for test access
    return await aiohttp_client(api._app)  # type: ignore[return-value]


class TestTaskCreate:
    async def test_creates_task(self, api_client: TestClient) -> None:
        resp = await api_client.post(
            "/tasks/create",
            json={"from": "main", "prompt": "build website", "name": "Website"},
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["success"] is True
        assert data["task_id"] == "abc123"

    async def test_missing_prompt(self, api_client: TestClient) -> None:
        resp = await api_client.post("/tasks/create", json={"from": "main"})
        assert resp.status == 400
        data = await resp.json()
        assert data["success"] is False

    async def test_rejects_combined_provider_and_model(self, api_client: TestClient) -> None:
        hub = api_client.app["_test_hub"]
        resp = await api_client.post(
            "/tasks/create",
            json={
                "from": "main",
                "prompt": "run the task",
                "provider": "codex/gpt-5.6-luna",
            },
        )
        assert resp.status == 400
        data = await resp.json()
        assert data["success"] is False
        assert "separate fields" in data["error"]
        hub.submit.assert_not_called()

    async def test_preserves_codex_provider_and_model_fields(self, api_client: TestClient) -> None:
        hub = api_client.app["_test_hub"]
        resp = await api_client.post(
            "/tasks/create",
            json={
                "from": "main",
                "prompt": "run the task",
                "provider": "codex",
                "model": "gpt-5.6-luna",
            },
        )
        assert resp.status == 200
        submit = hub.submit.call_args.args[0]
        assert submit.provider_override == "codex"
        assert submit.model_override == "gpt-5.6-luna"

    async def test_invalid_json(self, api_client: TestClient) -> None:
        resp = await api_client.post("/tasks/create", data=b"not json")
        assert resp.status == 400


class TestTaskAskParent:
    async def test_returns_answer(self, api_client: TestClient) -> None:
        resp = await api_client.post(
            "/tasks/ask_parent",
            json={"task_id": "abc", "question": "Which framework?"},
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["success"] is True
        assert data["answer"] == "Yes, use HTML"

    async def test_missing_fields(self, api_client: TestClient) -> None:
        resp = await api_client.post("/tasks/ask_parent", json={"task_id": "abc"})
        assert resp.status == 400


class TestTaskList:
    async def test_returns_empty(self, api_client: TestClient) -> None:
        resp = await api_client.get("/tasks/list")
        assert resp.status == 200
        data = await resp.json()
        assert data["tasks"] == []


class TestTaskCancel:
    async def test_cancels_task(self, api_client: TestClient) -> None:
        resp = await api_client.post("/tasks/cancel", json={"task_id": "abc"})
        assert resp.status == 200
        data = await resp.json()
        assert data["success"] is True

    async def test_missing_task_id(self, api_client: TestClient) -> None:
        resp = await api_client.post("/tasks/cancel", json={})
        assert resp.status == 400


class TestTaskDelete:
    async def test_deletes_finished_task(self, api_client: TestClient) -> None:
        hub = api_client.app["_test_hub"]
        entry = MagicMock()
        entry.parent_agent = "main"
        entry.status = "done"
        hub.registry.get.return_value = entry
        hub.registry.delete.return_value = True

        resp = await api_client.post(
            "/tasks/delete",
            json={"task_id": "abc", "from": "main"},
        )
        assert resp.status == 200
        data = await resp.json()
        assert data["success"] is True

    async def test_rejects_running_task(self, api_client: TestClient) -> None:
        hub = api_client.app["_test_hub"]
        entry = MagicMock()
        entry.parent_agent = "main"
        entry.status = "running"
        hub.registry.get.return_value = entry
        hub.registry.delete.return_value = False

        resp = await api_client.post(
            "/tasks/delete",
            json={"task_id": "abc", "from": "main"},
        )
        assert resp.status == 409

    async def test_not_found(self, api_client: TestClient) -> None:
        hub = api_client.app["_test_hub"]
        hub.registry.get.return_value = None

        resp = await api_client.post("/tasks/delete", json={"task_id": "nope"})
        assert resp.status == 404

    async def test_unauthorized(self, api_client: TestClient) -> None:
        hub = api_client.app["_test_hub"]
        entry = MagicMock()
        entry.parent_agent = "main"
        hub.registry.get.return_value = entry

        resp = await api_client.post(
            "/tasks/delete",
            json={"task_id": "abc", "from": "other_agent"},
        )
        assert resp.status == 403

    async def test_missing_task_id(self, api_client: TestClient) -> None:
        resp = await api_client.post("/tasks/delete", json={})
        assert resp.status == 400


class TestTaskOnlyMode:
    async def test_no_interagent_routes_without_bus(self, api_client: TestClient) -> None:
        """When bus is None, interagent routes should not exist."""
        resp = await api_client.post(
            "/interagent/send",
            json={"from": "a", "to": "b", "message": "hi"},
        )
        assert resp.status == 404

    async def test_task_routes_work_without_bus(self, api_client: TestClient) -> None:
        resp = await api_client.get("/tasks/list")
        assert resp.status == 200
