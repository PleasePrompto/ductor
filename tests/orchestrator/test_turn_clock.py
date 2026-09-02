"""A turn's clock measures silence, not duration.

Work that used to be pushed into a background task now runs in the
conversation's own session. That only holds if a turn is allowed to last as
long as the work does: a wall-clock cap would kill the overnight run this
change exists to enable, and it would kill it after the tokens were spent.
"""

from __future__ import annotations

from types import SimpleNamespace

from ductor_bot.config import AgentConfig
from ductor_bot.orchestrator.flows import _make_timeout_controller


def _orch(**overrides: object) -> SimpleNamespace:
    config = AgentConfig(**overrides)  # type: ignore[arg-type]
    return SimpleNamespace(_config=config)


def test_a_normal_turn_is_never_capped_by_duration() -> None:
    controller = _make_timeout_controller(_orch(), "normal")

    assert controller is not None
    assert controller._cfg.max_extensions == 0  # 0 = unlimited
    assert controller._cfg.extend_on_activity is True


def test_the_window_renews_by_its_full_length_while_working() -> None:
    """A 120s extension on a 30-minute window would still kill a long tool."""
    controller = _make_timeout_controller(_orch(), "normal")

    assert controller is not None
    assert controller._cfg.activity_extension == controller._cfg.timeout_seconds


def test_a_silent_turn_still_has_a_deadline() -> None:
    """Unlimited duration is not unlimited silence — a hung CLI must still die."""
    controller = _make_timeout_controller(_orch(), "normal")

    assert controller is not None
    assert controller._cfg.timeout_seconds > 0


def test_other_execution_paths_keep_their_configured_limits() -> None:
    """Only the conversation's own turn changes; sub-agents stay bounded."""
    controller = _make_timeout_controller(_orch(), "subagent")

    assert controller is not None
    assert controller._cfg.max_extensions == AgentConfig().timeouts.max_extensions
    assert controller._cfg.activity_extension == AgentConfig().timeouts.activity_extension
