"""Tests for webhook wake via TelegramBot (per-chat lock pipeline).

The webhook wake flow was moved from ``orchestrator/flows.py`` into
``bot/app.py::_handle_webhook_wake``.  Integration tests live in
``tests/bot/test_app.py::TestWebhookWake``.
"""
