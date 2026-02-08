"""Bot command definitions shared across layers."""

from __future__ import annotations

BOT_COMMANDS: list[tuple[str, str]] = [
    ("new", "Start new session"),
    ("stop", "Stop the running agent"),
    ("status", "Show session info"),
    ("model", "Show/switch model"),
    ("memory", "Show main memory"),
    ("cron", "Show scheduled cron jobs"),
    ("upgrade", "Check for updates"),
    ("restart", "Restart bot"),
    ("diagnose", "Show system diagnostics"),
    ("help", "Show all commands"),
]
