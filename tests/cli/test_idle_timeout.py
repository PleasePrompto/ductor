"""An unlimited turn's deadline follows the last sign of life.

The old rule only extended when output had appeared within the previous 30
seconds *of the moment the deadline fired*. An agent that had been working
steadily for hours but happened to be inside a two-minute quiet tool call at
exactly the wrong second was killed as if it had hung. What we want is the
plain reading: die after N hours of silence, and nothing else.
"""

from __future__ import annotations

import time

from ductor_bot.cli.timeout_controller import TimeoutConfig, TimeoutController

WINDOW = 10800.0  # three hours


def _unlimited() -> TimeoutController:
    controller = TimeoutController(
        TimeoutConfig(
            timeout_seconds=WINDOW,
            extend_on_activity=True,
            activity_extension=WINDOW,
            max_extensions=0,
        )
    )
    controller.begin()
    return controller


def test_a_quiet_patch_does_not_kill_a_working_agent() -> None:
    """Five minutes between tool calls is normal; it is not a hang."""
    controller = _unlimited()
    controller.record_activity()
    controller._last_activity = time.monotonic() - 300  # last output 5 min ago

    assert controller.try_extend() is True


def test_the_new_deadline_is_measured_from_the_last_output() -> None:
    """Not from now — otherwise silence quietly earns a full fresh window."""
    controller = _unlimited()
    last = time.monotonic() - 600
    controller._last_activity = last

    controller.try_extend()

    assert abs(controller._deadline - (last + WINDOW)) < 1.0


def test_real_silence_still_ends_the_turn() -> None:
    """The whole point of keeping a deadline: a wedged CLI must not hold its topic."""
    controller = _unlimited()
    controller._last_activity = time.monotonic() - (WINDOW + 60)

    assert controller.try_extend() is False


def test_a_finished_step_keeps_the_next_one_alive() -> None:
    """Ali's case: step one ends after hours, step two starts, and must not
    inherit a deadline that is about to fire."""
    controller = _unlimited()
    controller._last_activity = time.monotonic() - 7200  # two hours of quiet work
    controller.try_extend()

    controller.record_activity()  # step one reports in
    controller.try_extend()

    assert controller.remaining > WINDOW - 60


def test_a_bounded_path_keeps_its_old_behaviour() -> None:
    """Sub-agents and other bounded paths are not part of this change."""
    controller = TimeoutController(
        TimeoutConfig(
            timeout_seconds=60.0,
            extend_on_activity=True,
            activity_extension=30.0,
            max_extensions=2,
        )
    )
    controller.begin()
    controller._last_activity = time.monotonic() - 120  # stale by the old rule

    assert controller.try_extend() is False


def test_a_bounded_path_still_extends_on_fresh_activity() -> None:
    controller = TimeoutController(
        TimeoutConfig(
            timeout_seconds=60.0,
            extend_on_activity=True,
            activity_extension=30.0,
            max_extensions=2,
        )
    )
    controller.begin()
    controller.record_activity()

    assert controller.try_extend() is True
    assert controller.try_extend() is True
    assert controller.try_extend() is False  # budget spent
