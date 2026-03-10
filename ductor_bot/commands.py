"""Bot command definitions shared across layers.

Commands are ordered by usage frequency (most used first).
Descriptions are kept ≤22 chars so mobile clients don't truncate.
"""

from __future__ import annotations

# -- Core commands (every agent, shown in Telegram popup) ------------------
# Sorted by typical usage: daily actions → power-user → rare maintenance.

BOT_COMMANDS: list[tuple[str, str]] = [
    # Daily
    ("new", "Start new chat"),
    ("stop", "Stop running agent"),
    ("interrupt", "Soft interrupt (ESC)"),
    ("model", "Show/switch model"),
    ("status", "Session info"),
    ("memory", "Show main memory"),
    # Automation & multi-agent
    ("session", "Background sessions"),
    ("tasks", "Background tasks"),
    ("cron", "Manage cron jobs"),
    ("agent_commands", "Multi-agent system"),
    # Browse & info
    ("showfiles", "Browse files"),
    ("info", "Docs, links & about"),
    ("help", "Show all commands"),
    # Maintenance (rare)
    ("diagnose", "System diagnostics"),
    ("upgrade", "Check for updates"),
    ("restart", "Restart bot"),
]

# Sub-commands registered as handlers but NOT shown in the Telegram popup.
# Users discover them via /agent_commands or /help.
MULTIAGENT_SUB_COMMANDS: list[tuple[str, str]] = [
    ("agents", "List all agents"),
    ("agent_start", "Start a sub-agent"),
    ("agent_stop", "Stop a sub-agent"),
    ("agent_restart", "Restart a sub-agent"),
    ("stop_all", "Stop all agents"),
]
