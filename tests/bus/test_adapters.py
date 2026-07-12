"""Tests for the Envelope adapter functions."""

from __future__ import annotations

from dataclasses import dataclass

from ductor_bot.bus.adapters import (
    from_background_result,
    from_cron_result,
    from_heartbeat,
    from_interagent_result,
    from_task_question,
    from_task_result,
    from_user_message,
    from_webhook_cron_result,
    from_webhook_wake,
)
from ductor_bot.bus.envelope import DeliveryMode, LockMode, Origin

# -- Fake result types (avoid importing real models with heavy deps) -----------


@dataclass
class _FakeBackgroundResult:
    task_id: str = "bg1"
    chat_id: int = 100
    message_id: int = 42
    thread_id: int | None = None
    transport: str = "tg"
    prompt_preview: str = "do something"
    result_text: str = "done"
    status: str = "success"
    elapsed_seconds: float = 1.5
    provider: str = "claude"
    model: str = "opus"
    session_name: str = "my-session"
    session_id: str = "sid1"


@dataclass
class _FakeWebhookResult:
    hook_id: str = "wh1"
    hook_title: str = "Deploy"
    result_text: str = "deployed"
    status: str = "success"


@dataclass
class _FakeInterAgentResult:
    task_id: str = "ia1"
    sender: str = "agent-a"
    recipient: str = "agent-b"
    message_preview: str = "please do X"
    result_text: str = "X is done"
    success: bool = True
    error: str | None = None
    elapsed_seconds: float = 2.0
    session_name: str = "ia-agent-a"
    provider_switch_notice: str = ""
    original_message: str = "full message"
    chat_id: int = 0
    topic_id: int | None = None


@dataclass
class _FakeTaskResult:
    task_id: str = "t1"
    chat_id: int = 100
    parent_agent: str = "main"
    name: str = "research"
    prompt_preview: str = "find info"
    result_text: str = "found it"
    status: str = "done"
    elapsed_seconds: float = 5.0
    provider: str = "claude"
    model: str = "sonnet"
    session_id: str = "tsid1"
    error: str = ""
    task_folder: str = "/tmp/tasks/t1"
    original_prompt: str = "find info about X"
    thread_id: int | None = None


# -- Tests ---------------------------------------------------------------------


def test_from_background_result() -> None:
    env = from_background_result(_FakeBackgroundResult())
    assert env.origin == Origin.BACKGROUND
    assert env.chat_id == 100
    assert env.topic_id is None
    assert env.transport == "tg"
    assert env.delivery == DeliveryMode.UNICAST
    assert env.lock_mode == LockMode.NONE
    assert not env.needs_injection
    assert env.reply_to_message_id == 42
    assert env.session_name == "my-session"
    assert env.provider == "claude"
    assert not env.is_error


def test_from_background_result_error() -> None:
    env = from_background_result(_FakeBackgroundResult(status="error:timeout"))
    assert env.is_error


def test_from_background_result_preserves_transport_and_thread_target() -> None:
    env = from_background_result(_FakeBackgroundResult(thread_id=9, transport="sl"))
    assert env.topic_id == 9
    assert env.thread_id == 9
    assert env.transport == "sl"


def test_from_cron_result() -> None:
    env = from_cron_result("Daily Report", "all good", "success")
    assert env.origin == Origin.CRON
    assert env.chat_id == 0
    assert env.delivery == DeliveryMode.BROADCAST
    assert env.lock_mode == LockMode.NONE
    assert env.metadata["title"] == "Daily Report"
    assert env.result_text == "all good"


def test_from_cron_result_with_chat_id_creates_unicast() -> None:
    env = from_cron_result("Title", "Result", "success", chat_id=12345, topic_id=99)
    assert env.chat_id == 12345
    assert env.topic_id == 99
    assert env.delivery == DeliveryMode.UNICAST


def test_from_cron_result_without_chat_id_broadcasts() -> None:
    env = from_cron_result("Title", "Result", "success")
    assert env.chat_id == 0
    assert env.delivery == DeliveryMode.BROADCAST


def test_from_heartbeat() -> None:
    env = from_heartbeat(200, "alert text")
    assert env.origin == Origin.HEARTBEAT
    assert env.chat_id == 200
    assert env.topic_id is None
    assert env.delivery == DeliveryMode.UNICAST
    assert env.lock_mode == LockMode.NONE
    assert env.result_text == "alert text"


def test_from_heartbeat_with_topic_id() -> None:
    env = from_heartbeat(-1001, "group alert", topic_id=42)
    assert env.origin == Origin.HEARTBEAT
    assert env.chat_id == -1001
    assert env.topic_id == 42
    assert env.delivery == DeliveryMode.UNICAST
    assert env.result_text == "group alert"


def test_from_heartbeat_preserves_transport() -> None:
    env = from_heartbeat(200, "alert text", topic_id=7, transport="sl")
    assert env.chat_id == 200
    assert env.topic_id == 7
    assert env.transport == "sl"


def test_from_webhook_cron_result() -> None:
    env = from_webhook_cron_result(_FakeWebhookResult())
    assert env.origin == Origin.WEBHOOK_CRON
    assert env.delivery == DeliveryMode.BROADCAST
    assert env.lock_mode == LockMode.NONE
    assert env.metadata["hook_title"] == "Deploy"


def test_from_webhook_wake() -> None:
    env = from_webhook_wake(300, "wake up")
    assert env.origin == Origin.WEBHOOK_WAKE
    assert env.chat_id == 300
    assert env.prompt == "wake up"
    assert env.delivery == DeliveryMode.UNICAST
    assert env.lock_mode == LockMode.REQUIRED


def test_from_interagent_success_without_prompt_no_injection() -> None:
    """rr#18: without injection_prompt, needs_injection must be False (raw deliver)."""
    env = from_interagent_result(_FakeInterAgentResult(), chat_id=100)
    assert env.origin == Origin.INTERAGENT
    assert env.chat_id == 100
    assert env.status == "success"
    assert env.delivery == DeliveryMode.UNICAST
    assert env.lock_mode == LockMode.REQUIRED
    assert not env.needs_injection
    assert env.prompt == ""
    assert env.metadata["sender"] == "agent-a"


def test_from_interagent_success_with_prompt_enables_injection() -> None:
    """rr#18: injection_prompt wires needs_injection=True and sets envelope.prompt."""
    prompt = "[ASYNC INTER-AGENT RESPONSE from 'dev' (task t1)]\nresult\n[END]"
    env = from_interagent_result(_FakeInterAgentResult(), chat_id=100, injection_prompt=prompt)
    assert env.origin == Origin.INTERAGENT
    assert env.needs_injection
    assert env.prompt == prompt
    assert env.lock_mode == LockMode.REQUIRED


def test_from_interagent_matrix_transport() -> None:
    """Matrix callers must produce mx envelopes for session injection and delivery."""
    prompt = "[ASYNC INTER-AGENT RESPONSE from 'dev' (task t1)]\nresult\n[END]"
    env = from_interagent_result(
        _FakeInterAgentResult(),
        chat_id=100,
        injection_prompt=prompt,
        transport="mx",
    )
    assert env.transport == "mx"
    assert env.metadata["transport"] == "mx"


def test_from_interagent_error() -> None:
    env = from_interagent_result(_FakeInterAgentResult(success=False, error="timeout"), chat_id=100)
    assert env.status == "error"
    assert env.is_error
    assert env.lock_mode == LockMode.NONE
    assert not env.needs_injection
    assert env.metadata["error"] == "timeout"


def test_from_interagent_result_uses_result_chat_id() -> None:
    """When result carries chat_id, it overrides the fallback chat_id."""
    env = from_interagent_result(
        _FakeInterAgentResult(chat_id=777, topic_id=42),
        chat_id=100,
    )
    assert env.chat_id == 777
    assert env.topic_id == 42


def test_from_interagent_result_falls_back_to_default_chat_id() -> None:
    """When result has no chat_id (0), falls back to the provided default."""
    env = from_interagent_result(
        _FakeInterAgentResult(chat_id=0, topic_id=None),
        chat_id=100,
    )
    assert env.chat_id == 100
    assert env.topic_id is None


def test_from_interagent_error_preserves_topic_id() -> None:
    """topic_id is preserved on error results too."""
    env = from_interagent_result(
        _FakeInterAgentResult(success=False, error="fail", chat_id=555, topic_id=99),
        chat_id=100,
    )
    assert env.chat_id == 555
    assert env.topic_id == 99
    assert env.is_error


def test_from_task_result_done() -> None:
    env = from_task_result(_FakeTaskResult())
    assert env.origin == Origin.TASK_RESULT
    assert env.chat_id == 100
    assert env.topic_id is None
    assert env.status == "done"
    assert env.lock_mode == LockMode.REQUIRED
    assert env.needs_injection
    assert not env.is_error
    assert env.metadata["name"] == "research"
    assert "BACKGROUND TASK COMPLETED" in env.prompt
    assert "task_id='t1'" in env.prompt
    assert "found it" in env.prompt
    assert "Review this result critically" in env.prompt


def test_from_task_result_with_topic() -> None:
    env = from_task_result(_FakeTaskResult(thread_id=42))
    assert env.chat_id == 100
    assert env.topic_id == 42


def test_from_task_result_failed() -> None:
    env = from_task_result(_FakeTaskResult(status="failed", error="crash"))
    assert env.lock_mode == LockMode.REQUIRED
    assert env.needs_injection
    assert env.is_error
    assert env.metadata["error"] == "crash"
    assert "BACKGROUND TASK FAILED" in env.prompt
    assert "crash" in env.prompt


def test_from_task_result_cancelled() -> None:
    env = from_task_result(_FakeTaskResult(status="cancelled"))
    assert env.lock_mode == LockMode.NONE
    assert not env.needs_injection


def test_from_task_result_preserves_parent_agent() -> None:
    """#73: parent_agent must round-trip through Envelope.metadata so the
    per-agent transport adapter can route the result back to the originating
    sub-agent's bot (not the main agent's)."""
    env = from_task_result(_FakeTaskResult(parent_agent="sonic"))
    assert env.metadata["parent_agent"] == "sonic"


def test_from_task_question() -> None:
    env = from_task_question("t1", "what color?", "what co...", 100)
    assert env.origin == Origin.TASK_QUESTION
    assert env.chat_id == 100
    assert env.topic_id is None
    assert env.prompt == "what color?"
    assert env.lock_mode == LockMode.REQUIRED
    assert env.needs_injection
    assert env.metadata["task_id"] == "t1"


def test_from_task_question_with_topic() -> None:
    env = from_task_question("t1", "what color?", "what co...", 100, topic_id=42)
    assert env.chat_id == 100
    assert env.topic_id == 42


# -- User / API messages -------------------------------------------------------


def test_from_user_message_default_origin() -> None:
    env = from_user_message(100, "hello world")
    assert env.origin == Origin.USER
    assert env.chat_id == 100
    assert env.prompt == "hello world"
    assert env.prompt_preview == "hello world"
    assert env.delivery == DeliveryMode.UNICAST
    assert env.lock_mode == LockMode.NONE
    assert env.topic_id is None


def test_from_user_message_api_source() -> None:
    env = from_user_message(200, "api request", source=Origin.API)
    assert env.origin == Origin.API
    assert env.chat_id == 200
    assert env.prompt == "api request"


def test_from_user_message_with_topic() -> None:
    env = from_user_message(300, "topic msg", topic_id=42)
    assert env.chat_id == 300
    assert env.topic_id == 42


def test_from_user_message_truncates_preview() -> None:
    long_text = "x" * 200
    env = from_user_message(100, long_text)
    assert len(env.prompt_preview) == 80
    assert env.prompt == long_text


def test_from_user_message_empty_text() -> None:
    env = from_user_message(100, "")
    assert env.prompt == ""
    assert env.prompt_preview == ""
