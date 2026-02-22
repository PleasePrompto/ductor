"""Tests for the message hook system."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from ductor_bot.cli.types import AgentResponse
from ductor_bot.orchestrator.core import Orchestrator
from ductor_bot.orchestrator.flows import normal
from ductor_bot.orchestrator.hooks import (
    MAINMEMORY_REMINDER,
    HookContext,
    MessageHook,
    MessageHookRegistry,
    every_n_messages,
)

# ---------------------------------------------------------------------------
# Unit tests: HookContext, conditions, registry
# ---------------------------------------------------------------------------


def _ctx(*, message_count: int = 0, is_new: bool = False) -> HookContext:
    return HookContext(
        chat_id=1,
        message_count=message_count,
        is_new_session=is_new,
        provider="claude",
        model="opus",
    )


class TestEveryNMessages:
    def test_fires_on_nth_message(self) -> None:
        check = every_n_messages(6)
        # message_count is pre-increment: count=5 -> 6th message
        assert check(_ctx(message_count=5)) is True

    def test_fires_on_multiples(self) -> None:
        check = every_n_messages(6)
        assert check(_ctx(message_count=11)) is True  # 12th
        assert check(_ctx(message_count=17)) is True  # 18th

    def test_does_not_fire_on_first(self) -> None:
        check = every_n_messages(6)
        assert check(_ctx(message_count=0)) is False

    def test_does_not_fire_between_intervals(self) -> None:
        check = every_n_messages(6)
        for count in (1, 2, 3, 4, 6, 7, 8, 9, 10):
            assert check(_ctx(message_count=count)) is False

    def test_interval_of_1(self) -> None:
        check = every_n_messages(1)
        assert check(_ctx(message_count=0)) is True
        assert check(_ctx(message_count=1)) is True
        assert check(_ctx(message_count=99)) is True

    def test_interval_of_3(self) -> None:
        check = every_n_messages(3)
        assert check(_ctx(message_count=2)) is True  # 3rd
        assert check(_ctx(message_count=5)) is True  # 6th
        assert check(_ctx(message_count=1)) is False
        assert check(_ctx(message_count=3)) is False


class TestMessageHookRegistry:
    def test_no_hooks_returns_original(self) -> None:
        reg = MessageHookRegistry()
        assert reg.apply("hello", _ctx()) == "hello"

    def test_matching_hook_appends_suffix(self) -> None:
        reg = MessageHookRegistry()
        hook = MessageHook(name="test", condition=lambda _: True, suffix="## Reminder")
        reg.register(hook)
        result = reg.apply("hello", _ctx())
        assert result == "hello\n\n## Reminder"

    def test_non_matching_hook_ignored(self) -> None:
        reg = MessageHookRegistry()
        hook = MessageHook(name="test", condition=lambda _: False, suffix="## Reminder")
        reg.register(hook)
        assert reg.apply("hello", _ctx()) == "hello"

    def test_multiple_hooks_concatenated(self) -> None:
        reg = MessageHookRegistry()
        reg.register(MessageHook(name="a", condition=lambda _: True, suffix="A"))
        reg.register(MessageHook(name="b", condition=lambda _: True, suffix="B"))
        result = reg.apply("hello", _ctx())
        assert result == "hello\n\nA\n\nB"

    def test_mixed_matching(self) -> None:
        reg = MessageHookRegistry()
        reg.register(MessageHook(name="yes", condition=lambda _: True, suffix="YES"))
        reg.register(MessageHook(name="no", condition=lambda _: False, suffix="NO"))
        result = reg.apply("hello", _ctx())
        assert result == "hello\n\nYES"
        assert "NO" not in result


class TestMainmemoryReminder:
    def test_fires_on_6th(self) -> None:
        assert MAINMEMORY_REMINDER.condition(_ctx(message_count=5)) is True

    def test_does_not_fire_on_5th(self) -> None:
        assert MAINMEMORY_REMINDER.condition(_ctx(message_count=4)) is False

    def test_suffix_contains_key_phrases(self) -> None:
        assert "MAINMEMORY.md" in MAINMEMORY_REMINDER.suffix
        assert "MEMORY CHECK" in MAINMEMORY_REMINDER.suffix


# ---------------------------------------------------------------------------
# Integration: hook fires through the full flow
# ---------------------------------------------------------------------------


def _mock_response(**kwargs: object) -> AgentResponse:
    defaults: dict[str, object] = {
        "result": "OK",
        "session_id": "sess-123",
        "is_error": False,
        "cost_usd": 0.01,
        "total_tokens": 100,
    }
    defaults.update(kwargs)
    return AgentResponse(**defaults)  # type: ignore[arg-type]


@pytest.fixture
def orch(orch: Orchestrator) -> Orchestrator:
    return orch


async def test_hook_injects_into_prompt_on_6th_message(orch: Orchestrator) -> None:
    """After 5 successful messages, the 6th should carry the reminder."""
    resp = _mock_response()
    mock_execute = AsyncMock(return_value=resp)
    object.__setattr__(orch._cli_service, "execute", mock_execute)

    # Send 5 messages to build up the counter
    for _ in range(5):
        await normal(orch, 1, "msg")

    # 6th message should have the hook injected
    await normal(orch, 1, "sixth")

    sixth_call = mock_execute.call_args_list[5]
    request = sixth_call[0][0]
    assert "MEMORY CHECK" in request.prompt
    assert "memory_system/MAINMEMORY.md" in request.prompt


async def test_hook_not_injected_before_6th(orch: Orchestrator) -> None:
    """Messages 1-5 should not carry the reminder."""
    resp = _mock_response()
    mock_execute = AsyncMock(return_value=resp)
    object.__setattr__(orch._cli_service, "execute", mock_execute)

    for i in range(5):
        await normal(orch, 1, f"msg-{i}")
        request = mock_execute.call_args_list[i][0][0]
        assert "REMINDER" not in request.prompt


async def test_hook_resets_on_new_session(orch: Orchestrator) -> None:
    """After session reset, counter restarts -- 6th from reset triggers hook."""
    resp = _mock_response()
    mock_execute = AsyncMock(return_value=resp)
    object.__setattr__(orch._cli_service, "execute", mock_execute)

    # Send 5 messages
    for _ in range(5):
        await normal(orch, 1, "msg")

    # Reset session (simulates /new)
    await orch._sessions.reset_session(1)

    # Messages after reset should NOT carry the reminder (counter back to 0)
    await normal(orch, 1, "after-reset")
    last_request = mock_execute.call_args[0][0]
    assert "REMINDER" not in last_request.prompt
