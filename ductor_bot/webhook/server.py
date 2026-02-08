"""Webhook HTTP server: aiohttp-based ingress for external triggers."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable
from typing import TYPE_CHECKING, Any

from aiohttp import web

from ductor_bot.log_context import set_log_context
from ductor_bot.webhook.auth import RateLimiter, validate_hook_auth

if TYPE_CHECKING:
    from ductor_bot.config import WebhookConfig
    from ductor_bot.webhook.manager import WebhookManager
    from ductor_bot.webhook.models import WebhookResult

logger = logging.getLogger(__name__)

WebhookDispatchCallback = Callable[[str, dict[str, Any]], Awaitable["WebhookResult"]]


class WebhookServer:
    """HTTP server accepting webhook payloads and dispatching them.

    Routes:
    - ``GET  /health``          -- Health check for tunnel/proxy monitoring.
    - ``POST /hooks/{hook_id}`` -- Catch-all webhook endpoint.
    """

    def __init__(
        self,
        config: WebhookConfig,
        manager: WebhookManager,
    ) -> None:
        self._config = config
        self._manager = manager
        self._rate_limiter = RateLimiter(config.rate_limit_per_minute)
        self._dispatch: WebhookDispatchCallback | None = None
        self._runner: web.AppRunner | None = None
        self._background_tasks: set[asyncio.Task[None]] = set()

    def set_dispatch_handler(self, handler: WebhookDispatchCallback) -> None:
        """Set the callback invoked for each valid webhook request."""
        self._dispatch = handler

    async def start(self) -> None:
        """Create the aiohttp app and start listening."""
        app = web.Application(client_max_size=self._config.max_body_bytes)
        app.router.add_get("/health", self._handle_health)
        app.router.add_post("/hooks/{hook_id}", self._handle_hook)
        self._runner = web.AppRunner(app, access_log=None)
        await self._runner.setup()
        site = web.TCPSite(self._runner, self._config.host, self._config.port)
        await site.start()
        logger.info(
            "Webhook server listening on %s:%d",
            self._config.host,
            self._config.port,
        )

    async def stop(self) -> None:
        """Shut down the server."""
        if self._runner:
            await self._runner.cleanup()
            self._runner = None
        logger.info("Webhook server stopped")

    # -- Handlers --

    async def _handle_health(self, _request: web.Request) -> web.Response:
        return web.json_response({"status": "ok"})

    async def _handle_hook(self, request: web.Request) -> web.Response:  # noqa: PLR0911
        hook_id = request.match_info["hook_id"]
        set_log_context(operation="wh")
        logger.info("Webhook request received hook=%s method=%s", hook_id, request.method)

        # 1. Rate limit
        if not self._rate_limiter.check():
            logger.warning("Webhook rejected: rate limited hook=%s", hook_id)
            return web.json_response({"error": "rate_limited"}, status=429)

        # 2. Content-Type
        if request.content_type != "application/json":
            logger.warning("Webhook rejected: bad content-type hook=%s", hook_id)
            return web.json_response({"error": "content_type_must_be_json"}, status=415)

        # 3. Read raw body (needed for both JSON parsing and HMAC validation)
        raw_body = await request.read()

        # 4. Parse JSON
        try:
            payload: Any = json.loads(raw_body)
        except (json.JSONDecodeError, ValueError):
            logger.warning("Webhook rejected: invalid JSON hook=%s", hook_id)
            return web.json_response({"error": "invalid_json"}, status=400)

        if not isinstance(payload, dict):
            logger.warning("Webhook rejected: body not object hook=%s", hook_id)
            return web.json_response({"error": "body_must_be_object"}, status=400)

        # 5. Look up hook
        hook = self._manager.get_hook(hook_id)
        if hook is None:
            logger.warning("Webhook rejected: not found hook=%s", hook_id)
            return web.json_response({"error": "hook_not_found"}, status=404)

        if not hook.enabled:
            logger.warning("Webhook rejected: disabled hook=%s", hook_id)
            return web.json_response({"error": "hook_disabled"}, status=403)

        # 6. Per-hook auth (bearer token or HMAC signature)
        auth_header = request.headers.get("Authorization", "")
        sig_value = request.headers.get(hook.hmac_header, "") if hook.hmac_header else ""
        if not validate_hook_auth(
            hook,
            authorization=auth_header,
            signature_header_value=sig_value,
            body=raw_body,
            global_token=self._config.token,
        ):
            logger.warning("Webhook rejected: unauthorized hook=%s", hook_id)
            return web.json_response({"error": "unauthorized"}, status=401)

        logger.debug("Webhook validation passed hook=%s", hook_id)

        # 7. Dispatch async (fire-and-forget to avoid HTTP timeout)
        if self._dispatch:
            task = asyncio.create_task(self._safe_dispatch(hook_id, payload))
            self._background_tasks.add(task)
            task.add_done_callback(self._background_tasks.discard)

        return web.json_response({"accepted": True, "hook_id": hook_id}, status=202)

    async def _safe_dispatch(self, hook_id: str, payload: dict[str, Any]) -> None:
        """Run dispatch in a task with exception protection."""
        if self._dispatch is None:
            return
        try:
            await self._dispatch(hook_id, payload)
        except Exception:
            logger.exception("Webhook dispatch error for hook=%s", hook_id)
