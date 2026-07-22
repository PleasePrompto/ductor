"""Tests for the webhook tool-boundary validation of wake execution overrides (#176)."""

from __future__ import annotations

import pytest

from ductor_bot._home_defaults.workspace.tools.webhook_tools._shared import (
    reject_wake_overrides,
)


@pytest.mark.parametrize(
    "override",
    [
        {"provider": "codex"},
        {"model": "gpt-5.5"},
        {"reasoning_effort": "high"},
        {"cli_parameters": '["--chrome"]'},
        {"cli_parameters": ["--chrome"]},
    ],
)
def test_wake_with_any_override_is_rejected(override: dict[str, object]) -> None:
    kwargs: dict[str, object] = {
        "provider": None,
        "model": None,
        "reasoning_effort": None,
        "cli_parameters": None,
    }
    kwargs.update(override)
    error = reject_wake_overrides("wake", **kwargs)  # type: ignore[arg-type]
    assert error is not None
    assert "wake" in error
    assert "cron_task" in error


def test_wake_without_overrides_passes() -> None:
    assert (
        reject_wake_overrides(
            "wake", provider=None, model=None, reasoning_effort=None, cli_parameters=None
        )
        is None
    )


def test_cron_task_with_overrides_passes() -> None:
    assert (
        reject_wake_overrides(
            "cron_task",
            provider="codex",
            model="gpt-5.5",
            reasoning_effort="high",
            cli_parameters=["--chrome"],
        )
        is None
    )


def test_error_names_the_offending_flags() -> None:
    error = reject_wake_overrides(
        "wake", provider="codex", model=None, reasoning_effort=None, cli_parameters=None
    )
    assert error is not None
    assert "--provider" in error
    assert "--model" not in error
