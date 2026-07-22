"""Internal localhost HTTP API bridging CLI subprocesses to the InterAgentBus and TaskHub.

CLI subprocesses (claude, codex, gemini, agy) run as separate OS processes and
cannot access in-memory objects directly. This lightweight aiohttp server
exposes endpoints on localhost only, so tool scripts like ``ask_agent.py``,
``ask_agent_async.py``, ``create_task.py``, and ``ask_parent.py`` can
communicate with the bus and task hub.

The server also starts in **task-only mode** (no multi-agent bus) when
``tasks.enabled`` is true but no sub-agents are configured.
"""

from __future__ import annotations

import logging
from dataclasses import asdict
from typing import TYPE_CHECKING

from aiohttp import web

if TYPE_CHECKING:
    from ductor_bot.multiagent.bus import InterAgentBus
    from ductor_bot.multiagent.health import AgentHealth
    from ductor_bot.tasks.hub import TaskHub

logger = logging.getLogger(__name__)

_TRANSPORT_ALIASES = {"telegram": "tg", "matrix": "mx"}


def _normalise_transport(value: str) -> str:
    """Return the short transport id used by envelopes/session keys."""
    stripped = value.strip().lower()
    return _TRANSPORT_ALIASES.get(stripped, stripped)


def _parse_origin(data: dict[str, object]) -> tuple[int, int | None]:
    """Extract the (chat_id, topic_id) origin fields from a request payload."""
    chat_id = int(str(data["chat_id"])) if data.get("chat_id") else 0
    topic_id = int(str(data["topic_id"])) if data.get("topic_id") else None
    return chat_id, topic_id


_DEFAULT_PORT = 8799
_BIND_ALL_HOST = ".".join(["0"] * 4)


class InternalAgentAPI:
    """HTTP server for CLI → Bus / TaskHub communication.

    Binds to ``127.0.0.1`` by default.  When *docker_mode* is ``True`` it
    binds to ``0.0.0.0`` so that CLI processes running inside a Docker
    container can reach the API via ``host.docker.internal``.

    The *bus* parameter is optional: when ``None`` only task endpoints are
    registered (task-only mode for single-agent setups).
    """

    def __init__(
        self,
        bus: InterAgentBus | None = None,
        port: int = _DEFAULT_PORT,
        *,
        docker_mode: bool = False,
    ) -> None:
        self._bus = bus
        self._port = port
        self._bind_host = _BIND_ALL_HOST if docker_mode else "127.0.0.1"
        self._health_ref: dict[str, AgentHealth] | None = None
        self._task_hub: TaskHub | None = None
        self._app = web.Application()

        # Inter-agent routes (only when bus is available)
        if bus is not None:
            self._app.router.add_post("/interagent/send", self._handle_send)
            self._app.router.add_post("/interagent/send_async", self._handle_send_async)
            self._app.router.add_get("/interagent/agents", self._handle_list)
        self._app.router.add_get("/interagent/health", self._handle_health)

        # Task routes (always registered)
        self._app.router.add_post("/tasks/create", self._handle_task_create)
        self._app.router.add_post("/tasks/resume", self._handle_task_resume)
        self._app.router.add_post("/tasks/ask_parent", self._handle_task_ask_parent)
        self._app.router.add_get("/tasks/list", self._handle_task_list)
        self._app.router.add_post("/tasks/cancel", self._handle_task_cancel)
        self._app.router.add_post("/tasks/delete", self._handle_task_delete)

        self._runner: web.AppRunner | None = None

    def set_health_ref(self, health: dict[str, AgentHealth]) -> None:
        """Set reference to supervisor health dict for the /health endpoint."""
        self._health_ref = health

    def set_task_hub(self, hub: TaskHub) -> None:
        """Set the TaskHub for handling /tasks/* endpoints."""
        self._task_hub = hub

    @property
    def port(self) -> int:
        return self._port

    async def start(self) -> bool:
        """Start the internal API server.

        Returns:
            True when the listener is active, False when bind/start fails.
        """
        self._runner = web.AppRunner(self._app, access_log=None)
        await self._runner.setup()
        try:
            site = web.TCPSite(self._runner, self._bind_host, self._port)
            await site.start()
        except OSError:
            logger.exception(
                "Failed to start internal agent API on port %d",
                self._port,
            )
            # Best effort cleanup so callers can safely retry/start-stop.
            await self._runner.cleanup()
            self._runner = None
            return False
        else:
            logger.info("Internal agent API listening on %s:%d", self._bind_host, self._port)
            return True

    async def stop(self) -> None:
        """Stop the internal API server."""
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
            logger.info("Internal agent API stopped")

    async def _handle_send(self, request: web.Request) -> web.Response:
        """POST /interagent/send — send a message to another agent.

        Expects JSON body: ``{"from": "agent_name", "to": "agent_name", "message": "..."}``
        Returns JSON: ``{"sender": "...", "text": "...", "success": true/false, "error": "..."}``
        """
        try:
            data = await request.json()
        except Exception:
            return web.json_response(
                {"success": False, "error": "Invalid JSON body"},
                status=400,
            )

        sender = data.get("from", "")
        recipient = data.get("to", "")
        message = data.get("message", "")
        new_session = bool(data.get("new_session", False))
        chat_id, topic_id = _parse_origin(data)

        if not recipient or not message:
            return web.json_response(
                {"success": False, "error": "Missing 'to' or 'message' field"},
                status=400,
            )

        assert self._bus is not None  # Routes only registered when bus is set
        result = await self._bus.send(
            sender=sender,
            recipient=recipient,
            message=message,
            new_session=new_session,
            chat_id=chat_id,
            topic_id=topic_id,
        )
        return web.json_response(asdict(result))

    async def _handle_send_async(self, request: web.Request) -> web.Response:
        """POST /interagent/send_async — fire-and-forget inter-agent message.

        Expects JSON body: ``{"from": "agent_name", "to": "agent_name", "message": "..."}``
        Returns immediately: ``{"success": true/false, "task_id": "...", "error": "..."}``
        """
        try:
            data = await request.json()
        except Exception:
            return web.json_response(
                {"success": False, "error": "Invalid JSON body"},
                status=400,
            )

        sender = data.get("from", "")
        recipient = data.get("to", "")
        message = data.get("message", "")
        new_session = bool(data.get("new_session", False))
        summary = str(data.get("summary", ""))
        chat_id, topic_id = _parse_origin(data)
        transport = _normalise_transport(str(data.get("transport", "")))
        reply_to = str(data.get("reply_to", ""))  # #86
        silent = bool(data.get("silent", False))  # #86

        if not recipient or not message:
            return web.json_response(
                {"success": False, "error": "Missing 'to' or 'message' field"},
                status=400,
            )

        assert self._bus is not None  # Routes only registered when bus is set
        available = self._bus.list_agents()
        if recipient not in available:
            names = ", ".join(available) or "(none)"
            return web.json_response(
                {"success": False, "error": f"Agent '{recipient}' not found. Available: {names}"},
            )

        from ductor_bot.multiagent.bus import AsyncSendOptions

        opts = AsyncSendOptions(
            new_session=new_session,
            summary=summary,
            chat_id=chat_id,
            topic_id=topic_id,
            transport=transport,
            reply_to=reply_to,
            silent=silent,
        )
        task_id = self._bus.send_async(
            sender=sender,
            recipient=recipient,
            message=message,
            opts=opts,
        )
        if task_id is None:
            return web.json_response(
                {"success": False, "error": "Failed to create async task"},
            )

        return web.json_response({"success": True, "task_id": task_id})

    async def _handle_list(self, request: web.Request) -> web.Response:
        """GET /interagent/agents — list all registered agents."""
        assert self._bus is not None  # Routes only registered when bus is set
        return web.json_response({"agents": self._bus.list_agents()})

    async def _handle_health(self, request: web.Request) -> web.Response:
        """GET /interagent/health — return live health for all agents."""
        if self._health_ref is None:
            return web.json_response({"agents": {}})

        agents: dict[str, dict[str, object]] = {}
        for name, health in self._health_ref.items():
            agents[name] = {
                "status": health.status,
                "uptime": health.uptime_human,
                "restart_count": health.restart_count,
                "last_crash_error": health.last_crash_error or None,
            }
        return web.json_response({"agents": agents})

    # -- Task endpoints ----------------------------------------------------------

    async def _handle_task_create(self, request: web.Request) -> web.Response:
        """POST /tasks/create — create a background task.

        Expects JSON: ``{"from": "agent", "prompt": "...", "name": "...",
        "provider": null, "model": null, "thinking": null}``
        """
        if self._task_hub is None:
            return web.json_response(
                {"success": False, "error": "Task system not available"},
                status=503,
            )

        try:
            data = await request.json()
        except Exception:
            return web.json_response(
                {"success": False, "error": "Invalid JSON body"},
                status=400,
            )

        prompt = data.get("prompt", "")
        sender = data.get("from", "main")
        if not prompt:
            return web.json_response(
                {"success": False, "error": "Missing 'prompt' field"},
                status=400,
            )

        from ductor_bot.tasks.models import TaskSubmit, normalise_priority

        submit = TaskSubmit(
            chat_id=data.get("chat_id", 0),
            prompt=prompt,
            message_id=0,
            thread_id=data.get("topic_id") or None,
            parent_agent=sender,
            name=data.get("name", ""),
            provider_override=data.get("provider") or "",
            model_override=data.get("model") or "",
            thinking_override=data.get("thinking") or "",
            priority=normalise_priority(data.get("priority")),
        )

        try:
            task_id = self._task_hub.submit(submit)
        except ValueError as exc:
            return web.json_response({"success": False, "error": str(exc)})

        return web.json_response({"success": True, "task_id": task_id})

    async def _handle_task_resume(self, request: web.Request) -> web.Response:
        """POST /tasks/resume — resume a completed task with a follow-up.

        Expects JSON: ``{"task_id": "...", "prompt": "...", "from": "agent"}``
        """
        if self._task_hub is None:
            return web.json_response(
                {"success": False, "error": "Task system not available"},
                status=503,
            )

        try:
            data = await request.json()
        except Exception:
            return web.json_response(
                {"success": False, "error": "Invalid JSON body"},
                status=400,
            )

        task_id = data.get("task_id", "")
        prompt = data.get("prompt", "")
        sender = data.get("from", "")
        if not task_id or not prompt:
            return web.json_response(
                {"success": False, "error": "Missing 'task_id' or 'prompt' field"},
                status=400,
            )

        # Verify the requester owns this task
        if sender:
            entry = self._task_hub.registry.get(task_id)
            if entry is not None and entry.parent_agent != sender:
                return web.json_response(
                    {"success": False, "error": "Not authorized to resume this task"},
                    status=403,
                )

        try:
            resumed_id = self._task_hub.resume(task_id, prompt, parent_agent=sender)
        except ValueError as exc:
            return web.json_response({"success": False, "error": str(exc)})

        return web.json_response({"success": True, "task_id": resumed_id})

    async def _handle_task_ask_parent(self, request: web.Request) -> web.Response:
        """POST /tasks/ask_parent — task agent forwards a question to the parent.

        Expects JSON: ``{"task_id": "...", "question": "..."}``
        Returns immediately. The parent agent will resume the task with the answer.
        """
        if self._task_hub is None:
            return web.json_response(
                {"success": False, "error": "Task system not available"},
                status=503,
            )

        try:
            data = await request.json()
        except Exception:
            return web.json_response(
                {"success": False, "error": "Invalid JSON body"},
                status=400,
            )

        task_id = data.get("task_id", "")
        question = data.get("question", "")
        if not task_id or not question:
            return web.json_response(
                {"success": False, "error": "Missing 'task_id' or 'question' field"},
                status=400,
            )

        result = await self._task_hub.forward_question(task_id, question)
        is_error = result.startswith("Error:")
        return web.json_response(
            {
                "success": not is_error,
                "answer": result,
                **({"error": result} if is_error else {}),
            }
        )

    async def _handle_task_list(self, request: web.Request) -> web.Response:
        """GET /tasks/list — list tasks, filtered by parent_agent if provided."""
        if self._task_hub is None:
            return web.json_response({"tasks": []})

        parent_agent = request.query.get("from") or None
        entries = self._task_hub.registry.list_all(parent_agent=parent_agent)
        return web.json_response(
            {
                "tasks": [e.to_dict() for e in entries],
            }
        )

    async def _handle_task_cancel(self, request: web.Request) -> web.Response:
        """POST /tasks/cancel — cancel a running task."""
        if self._task_hub is None:
            return web.json_response(
                {"success": False, "error": "Task system not available"},
                status=503,
            )

        try:
            data = await request.json()
        except Exception:
            return web.json_response(
                {"success": False, "error": "Invalid JSON body"},
                status=400,
            )

        task_id = data.get("task_id", "")
        sender = data.get("from", "")
        if not task_id:
            return web.json_response(
                {"success": False, "error": "Missing 'task_id' field"},
                status=400,
            )

        # Verify the requester owns this task
        if sender:
            entry = self._task_hub.registry.get(task_id)
            if entry is not None and entry.parent_agent != sender:
                return web.json_response(
                    {"success": False, "error": "Not authorized to cancel this task"},
                    status=403,
                )

        cancelled = await self._task_hub.cancel(task_id)
        logger.info(
            "Task cancel via API id=%s from=%s success=%s",
            task_id,
            sender or "?",
            cancelled,
        )
        return web.json_response({"success": cancelled})

    async def _handle_task_delete(  # noqa: PLR0911
        self, request: web.Request
    ) -> web.Response:
        """POST /tasks/delete — permanently delete a finished task (entry + folder)."""
        if self._task_hub is None:
            return web.json_response(
                {"success": False, "error": "Task system not available"},
                status=503,
            )

        try:
            data = await request.json()
        except Exception:
            return web.json_response(
                {"success": False, "error": "Invalid JSON body"},
                status=400,
            )

        task_id = data.get("task_id", "")
        sender = data.get("from", "")
        if not task_id:
            return web.json_response(
                {"success": False, "error": "Missing 'task_id' field"},
                status=400,
            )

        entry = self._task_hub.registry.get(task_id)
        if entry is None:
            return web.json_response(
                {"success": False, "error": f"Task '{task_id}' not found"},
                status=404,
            )
        if sender and entry.parent_agent != sender:
            return web.json_response(
                {"success": False, "error": "Not authorized to delete this task"},
                status=403,
            )

        if not self._task_hub.registry.delete(task_id):
            return web.json_response(
                {"success": False, "error": "Task is still running or waiting"},
                status=409,
            )
        return web.json_response({"success": True})
