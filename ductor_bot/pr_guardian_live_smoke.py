"""Small PR Guardian live-agent smoke helper.

This module exists only on the synthetic PR branch used to exercise the
external PR Guardian reviewer with a real Claude Code agent.
"""

from __future__ import annotations


def normalize_live_agent_marker(value: str) -> str:
    """Normalize a marker used by live PR Guardian smoke tests."""
    return "-".join(value.strip().lower().split())
