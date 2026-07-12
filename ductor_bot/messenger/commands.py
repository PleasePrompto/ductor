"""Shared command classification for messenger transports.

Defines which commands are handled directly by the transport (DIRECT)
and which are routed to the orchestrator (ORCHESTRATOR).
"""

from __future__ import annotations

# Commands handled directly by each transport's bot class.
# Each transport implements these with its own UI patterns.
DIRECT_COMMANDS: frozenset[str] = frozenset(
    {
        "stop",
        "interrupt",
        "stop_all",
        "restart",
        "new",
        "help",
        "start",
        "info",
        "agent_commands",
        "showfiles",
        "session",
    }
)

# Commands routed to the Orchestrator's CommandRegistry.
ORCHESTRATOR_COMMANDS: frozenset[str] = frozenset(
    {
        "status",
        "model",
        "effort",
        "memory",
        "cron",
        "diagnose",
        "upgrade",
        "sessions",
        "tasks",
        "reset",
    }
)

# Multi-agent commands (only registered for main agent).
MULTIAGENT_COMMANDS: frozenset[str] = frozenset(
    {
        "agents",
        "agent_start",
        "agent_stop",
        "agent_restart",
    }
)


def classify_command(cmd: str) -> str:
    """Classify a command name.

    Returns "direct", "orchestrator", "multiagent", or "unknown".
    """
    if cmd in DIRECT_COMMANDS:
        return "direct"
    if cmd in ORCHESTRATOR_COMMANDS:
        return "orchestrator"
    if cmd in MULTIAGENT_COMMANDS:
        return "multiagent"
    return "unknown"
