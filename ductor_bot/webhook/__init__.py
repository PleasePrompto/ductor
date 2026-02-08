"""Webhook system: HTTP ingress for external event triggers."""

from ductor_bot.webhook.manager import WebhookManager
from ductor_bot.webhook.models import WebhookEntry, WebhookResult

__all__ = ["WebhookEntry", "WebhookManager", "WebhookResult"]
