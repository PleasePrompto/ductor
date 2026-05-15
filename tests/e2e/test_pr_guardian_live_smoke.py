from __future__ import annotations

from ductor_bot.pr_guardian_live_smoke import normalize_live_agent_marker


def test_normalize_live_agent_marker() -> None:
    assert normalize_live_agent_marker("  PR Guardian   Live  Agent  ") == "pr-guardian-live-agent"
