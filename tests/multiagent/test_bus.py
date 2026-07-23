"""Tests for multiagent/bus.py: InterAgentBus message passing."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

from ductor_bot.multiagent.bus import AsyncSendOptions, InterAgentBus
from ductor_bot.session.named import interagent_session_name


def _make_stack(
    orch_result: str = "response",
    session_name: str = "ia-sender",
    provider_notice: str = "",
) -> MagicMock:
    """Create a mock AgentStack with a working orchestrator."""
    stack = MagicMock()
    orch = MagicMock()
    orch.handle_interagent_message = AsyncMock(
        return_value=(orch_result, session_name, provider_notice),
    )
    stack.bot.orchestrator = orch
    return stack


class TestBusRegistration:
    """Test agent registration and listing."""

    def test_register_and_list(self) -> None:
        bus = InterAgentBus()
        stack = _make_stack()
        bus.register("agent1", stack)
        assert "agent1" in bus.list_agents()

    def test_unregister(self) -> None:
        bus = InterAgentBus()
        bus.register("agent1", _make_stack())
        bus.unregister("agent1")
        assert "agent1" not in bus.list_agents()

    def test_unregister_nonexistent_is_noop(self) -> None:
        bus = InterAgentBus()
        bus.unregister("nonexistent")  # no error

    def test_list_multiple_agents(self) -> None:
        bus = InterAgentBus()
        bus.register("a", _make_stack())
        bus.register("b", _make_stack())
        bus.register("c", _make_stack())
        agents = bus.list_agents()
        assert set(agents) == {"a", "b", "c"}


class TestBusSyncSend:
    """Test synchronous send()."""

    async def test_send_success(self) -> None:
        bus = InterAgentBus()
        bus.register("recipient", _make_stack("Hello back"))
        result = await bus.send("sender", "recipient", "Hello")
        assert result.success is True
        assert result.text == "Hello back"
        assert result.sender == "recipient"

    async def test_send_to_unknown_agent(self) -> None:
        bus = InterAgentBus()
        result = await bus.send("sender", "unknown", "Hello")
        assert result.success is False
        assert "not found" in result.error

    async def test_send_lists_available_agents_in_error(self) -> None:
        bus = InterAgentBus()
        bus.register("agent1", _make_stack())
        result = await bus.send("sender", "unknown", "Hello")
        assert "agent1" in result.error

    async def test_send_to_agent_without_orchestrator(self) -> None:
        bus = InterAgentBus()
        stack = MagicMock()
        stack.bot.orchestrator = None
        bus.register("target", stack)
        result = await bus.send("sender", "target", "Hello")
        assert result.success is False
        assert "not initialized" in result.error

    async def test_send_timeout(self) -> None:
        bus = InterAgentBus()
        stack = _make_stack()

        async def slow_handler(_sender: str, _msg: str, **_kw: object) -> tuple[str, str, str]:
            await asyncio.sleep(10)
            return "too late", "ia-sender", ""

        stack.bot.orchestrator.handle_interagent_message = slow_handler
        bus.register("slow", stack)

        result = await bus.send("sender", "slow", "Hello", send_timeout=0.01)
        assert result.success is False
        assert "Timeout" in result.error

    async def test_send_exception_handling(self) -> None:
        bus = InterAgentBus()
        stack = _make_stack()
        stack.bot.orchestrator.handle_interagent_message = AsyncMock(
            side_effect=RuntimeError("crash")
        )
        bus.register("target", stack)

        result = await bus.send("sender", "target", "Hello")
        assert result.success is False
        assert "RuntimeError" in result.error


class TestBusAsyncSend:
    """Test fire-and-forget send_async()."""

    async def test_send_async_returns_task_id(self) -> None:
        bus = InterAgentBus()
        bus.register("target", _make_stack("async result"))
        task_id = bus.send_async("sender", "target", "Hello")
        assert task_id is not None
        assert isinstance(task_id, str)

    async def test_send_async_unknown_agent_returns_none(self) -> None:
        bus = InterAgentBus()
        task_id = bus.send_async("sender", "unknown", "Hello")
        assert task_id is None

    async def test_send_async_delivers_result(self) -> None:
        bus = InterAgentBus()
        bus.register("target", _make_stack("async response"))

        delivered: list[object] = []
        handler = AsyncMock(side_effect=delivered.append)
        bus.set_async_result_handler("sender", handler)

        task_id = bus.send_async("sender", "target", "Hello")
        assert task_id is not None

        # Wait for the async task to complete
        await asyncio.sleep(0.1)

        assert len(delivered) == 1
        result = delivered[0]
        assert result.success is True
        assert result.result_text == "async response"
        assert result.task_id == task_id

    async def test_send_async_without_handler_does_not_crash(self) -> None:
        """If no result handler is registered, result is silently dropped."""
        bus = InterAgentBus()
        bus.register("target", _make_stack())
        task_id = bus.send_async("sender", "target", "Hello")
        assert task_id is not None
        await asyncio.sleep(0.1)  # let task finish

    async def test_send_async_timeout(self) -> None:
        bus = InterAgentBus()
        stack = _make_stack()

        async def slow_handler(_sender: str, _msg: str, **_kw: object) -> tuple[str, str, str]:
            await asyncio.sleep(999)
            return "never", "ia-sender", ""

        stack.bot.orchestrator.handle_interagent_message = slow_handler
        bus.register("slow", stack)

        delivered: list[object] = []
        bus.set_async_result_handler("sender", AsyncMock(side_effect=delivered.append))

        # The default timeout is 300s, but we can test cancel instead
        task_id = bus.send_async("sender", "slow", "Hello")
        assert task_id is not None

        cancelled = await bus.cancel_all_async()
        assert cancelled == 1


class TestBusCancelAllAsync:
    """Test cancel_all_async()."""

    async def test_cancel_all_returns_count(self) -> None:
        bus = InterAgentBus()
        stack = _make_stack()

        async def slow(_s: str, _m: str, **_kw: object) -> tuple[str, str, str]:
            await asyncio.sleep(999)
            return "", "ia-sender", ""

        stack.bot.orchestrator.handle_interagent_message = slow
        bus.register("target", stack)

        bus.send_async("a", "target", "1")
        bus.send_async("b", "target", "2")
        await asyncio.sleep(0.01)

        cancelled = await bus.cancel_all_async()
        assert cancelled == 2

    async def test_cancel_all_when_empty(self) -> None:
        bus = InterAgentBus()
        cancelled = await bus.cancel_all_async()
        assert cancelled == 0


class TestBusNewSessionFlag:
    """Test new_session flag propagation through sync and async paths."""

    async def test_sync_send_passes_new_session_false(self) -> None:
        bus = InterAgentBus()
        stack = _make_stack("ok")
        bus.register("target", stack)
        await bus.send("sender", "target", "Hello")
        stack.bot.orchestrator.handle_interagent_message.assert_awaited_once_with(
            "sender",
            "Hello",
            new_session=False,
        )

    async def test_sync_send_passes_new_session_true(self) -> None:
        bus = InterAgentBus()
        stack = _make_stack("ok")
        bus.register("target", stack)
        await bus.send("sender", "target", "Hello", new_session=True)
        stack.bot.orchestrator.handle_interagent_message.assert_awaited_once_with(
            "sender",
            "Hello",
            new_session=True,
        )

    async def test_async_send_passes_new_session_to_handler(self) -> None:
        """new_session=True is forwarded through _run_async to the orchestrator."""
        bus = InterAgentBus()
        call_kwargs: list[dict[str, object]] = []

        async def capturing_handler(sender: str, msg: str, **kw: object) -> tuple[str, str, str]:
            call_kwargs.append({"sender": sender, "msg": msg, **kw})
            return "done", "ia-sender", ""

        stack = _make_stack()
        stack.bot.orchestrator.handle_interagent_message = capturing_handler
        bus.register("target", stack)

        bus.send_async(
            "sender",
            "target",
            "Hello",
            opts=AsyncSendOptions(new_session=True, chat_id=777, topic_id=10),
        )
        await asyncio.sleep(0.1)

        assert len(call_kwargs) == 1
        assert call_kwargs[0]["new_session"] is True
        assert call_kwargs[0]["source_chat_id"] == 777
        assert call_kwargs[0]["source_topic_id"] == 10

    async def test_async_send_new_session_false_by_default(self) -> None:
        bus = InterAgentBus()
        call_kwargs: list[dict[str, object]] = []

        async def capturing_handler(sender: str, msg: str, **kw: object) -> tuple[str, str, str]:
            call_kwargs.append({"sender": sender, "msg": msg, **kw})
            return "done", "ia-sender", ""

        stack = _make_stack()
        stack.bot.orchestrator.handle_interagent_message = capturing_handler
        bus.register("target", stack)

        bus.send_async("sender", "target", "Hello")
        await asyncio.sleep(0.1)

        assert len(call_kwargs) == 1
        assert call_kwargs[0]["new_session"] is False


class TestBusSessionNameInResult:
    """Test that session_name is propagated through async results."""

    async def test_async_result_contains_session_name(self) -> None:
        bus = InterAgentBus()
        stack = _make_stack("response", session_name="ia-main")
        bus.register("target", stack)

        delivered: list[object] = []
        bus.set_async_result_handler("sender", AsyncMock(side_effect=delivered.append))

        bus.send_async("sender", "target", "Hello")
        await asyncio.sleep(0.1)

        assert len(delivered) == 1
        assert delivered[0].session_name == "ia-main"

    async def test_async_result_default_session_name(self) -> None:
        """Default session_name from _make_stack is 'ia-sender'."""
        bus = InterAgentBus()
        stack = _make_stack("ok")
        bus.register("target", stack)

        delivered: list[object] = []
        bus.set_async_result_handler("caller", AsyncMock(side_effect=delivered.append))

        bus.send_async("caller", "target", "Hello")
        await asyncio.sleep(0.1)

        assert len(delivered) == 1
        assert delivered[0].session_name == "ia-sender"


class TestBusNotifyRecipient:
    """Test _notify_recipient behavior."""

    async def test_notify_sends_to_first_user(self) -> None:
        """Notification is sent via notification_service.notify() to the first user."""
        bus = InterAgentBus()
        stack = _make_stack()
        stack.config.allowed_user_ids = [12345]
        mock_ns = AsyncMock()
        stack.bot.notification_service = mock_ns
        bus.register("target", stack)

        from ductor_bot.multiagent.bus import AsyncInterAgentTask

        task = AsyncInterAgentTask(
            task_id="abc123",
            sender="main",
            recipient="target",
            message="Do something important",
        )

        await bus._notify_recipient(task)
        mock_ns.notify.assert_awaited_once()
        call_args = mock_ns.notify.call_args
        assert call_args[0][0] == 12345  # chat_id
        assert "main" in call_args[0][1]  # sender name in text
        assert "ia-main" in call_args[0][1]  # session name in text

    async def test_notify_uses_scoped_session_name_with_source_context(self) -> None:
        bus = InterAgentBus()
        stack = _make_stack()
        stack.config.allowed_user_ids = [12345]
        mock_ns = AsyncMock()
        stack.bot.notification_service = mock_ns
        bus.register("target", stack)

        from ductor_bot.multiagent.bus import AsyncInterAgentTask

        task = AsyncInterAgentTask(
            task_id="abc123",
            sender="main",
            recipient="target",
            message="Do something important",
            chat_id=777,
            topic_id=10,
        )

        await bus._notify_recipient(task)

        expected = interagent_session_name("main", 777, 10)
        text = mock_ns.notify.call_args[0][1]
        assert expected in text
        assert "ia-main" not in text

    async def test_notify_broadcasts_when_no_users(self) -> None:
        """When no allowed_user_ids, notify_all() is used instead."""
        bus = InterAgentBus()
        stack = _make_stack()
        stack.config.allowed_user_ids = []
        mock_ns = AsyncMock()
        stack.bot.notification_service = mock_ns
        bus.register("target", stack)

        from ductor_bot.multiagent.bus import AsyncInterAgentTask

        task = AsyncInterAgentTask(
            task_id="abc123",
            sender="main",
            recipient="target",
            message="Hello",
        )

        await bus._notify_recipient(task)
        mock_ns.notify.assert_not_awaited()
        mock_ns.notify_all.assert_awaited_once()

    async def test_notify_skipped_when_no_service(self) -> None:
        """Notification is silently skipped when notification_service is None."""
        bus = InterAgentBus()
        stack = _make_stack()
        stack.config.allowed_user_ids = [12345]
        stack.bot.notification_service = None
        bus.register("target", stack)

        from ductor_bot.multiagent.bus import AsyncInterAgentTask

        task = AsyncInterAgentTask(
            task_id="abc123",
            sender="main",
            recipient="target",
            message="Hello",
        )

        # No notification_service — should not raise
        await bus._notify_recipient(task)

    async def test_notify_failure_does_not_raise(self) -> None:
        """Notification errors are swallowed (best-effort)."""
        bus = InterAgentBus()
        stack = _make_stack()
        stack.config.allowed_user_ids = [99]
        mock_ns = AsyncMock()
        mock_ns.notify = AsyncMock(side_effect=RuntimeError("network error"))
        stack.bot.notification_service = mock_ns
        bus.register("target", stack)

        from ductor_bot.multiagent.bus import AsyncInterAgentTask

        task = AsyncInterAgentTask(
            task_id="x",
            sender="main",
            recipient="target",
            message="test",
        )

        # Should not raise
        await bus._notify_recipient(task)


class TestBusChatTopicPropagation:
    """Test that chat_id/topic_id are propagated through async results.

    Bug #31: results must route back to the originating group/topic,
    not the sender's DM.
    """

    async def test_async_result_carries_chat_and_topic_id(self) -> None:
        """chat_id and topic_id from send_async appear in the result."""
        bus = InterAgentBus()
        bus.register("target", _make_stack("response"))

        delivered: list[object] = []
        bus.set_async_result_handler("sender", AsyncMock(side_effect=delivered.append))

        bus.send_async(
            "sender",
            "target",
            "Hello",
            opts=AsyncSendOptions(chat_id=12345, topic_id=678),
        )
        await asyncio.sleep(0.1)

        assert len(delivered) == 1
        result = delivered[0]
        assert result.chat_id == 12345
        assert result.topic_id == 678
        assert result.transport == ""
        assert result.success is True

    async def test_async_result_carries_transport(self) -> None:
        """transport from send_async appears in the result for multi-transport routing."""
        bus = InterAgentBus()
        bus.register("target", _make_stack("response"))

        delivered: list[object] = []
        bus.set_async_result_handler("sender", AsyncMock(side_effect=delivered.append))

        bus.send_async(
            "sender",
            "target",
            "Hello",
            opts=AsyncSendOptions(chat_id=12345, topic_id=678, transport="mx"),
        )
        await asyncio.sleep(0.1)

        assert len(delivered) == 1
        result = delivered[0]
        assert result.transport == "mx"

    async def test_async_result_defaults_without_context(self) -> None:
        """When no chat_id/topic_id are provided, defaults are 0/None."""
        bus = InterAgentBus()
        bus.register("target", _make_stack("response"))

        delivered: list[object] = []
        bus.set_async_result_handler("sender", AsyncMock(side_effect=delivered.append))

        bus.send_async("sender", "target", "Hello")
        await asyncio.sleep(0.1)

        assert len(delivered) == 1
        result = delivered[0]
        assert result.chat_id == 0
        assert result.topic_id is None

    async def test_error_result_carries_chat_and_topic_id(self) -> None:
        """chat_id/topic_id are preserved even when the task fails."""
        bus = InterAgentBus()
        stack = _make_stack()
        stack.bot.orchestrator.handle_interagent_message = AsyncMock(
            side_effect=RuntimeError("crash"),
        )
        bus.register("target", stack)

        delivered: list[object] = []
        bus.set_async_result_handler("sender", AsyncMock(side_effect=delivered.append))

        bus.send_async(
            "sender",
            "target",
            "Hello",
            opts=AsyncSendOptions(chat_id=99999, topic_id=42),
        )
        await asyncio.sleep(0.1)

        assert len(delivered) == 1
        result = delivered[0]
        assert result.success is False
        assert result.chat_id == 99999
        assert result.topic_id == 42

    async def test_no_orchestrator_result_carries_context(self) -> None:
        """chat_id/topic_id are preserved when orchestrator is None."""
        bus = InterAgentBus()
        stack = MagicMock()
        stack.bot.orchestrator = None
        bus.register("target", stack)

        delivered: list[object] = []
        bus.set_async_result_handler("sender", AsyncMock(side_effect=delivered.append))

        bus.send_async(
            "sender",
            "target",
            "Hello",
            opts=AsyncSendOptions(chat_id=111, topic_id=222),
        )
        await asyncio.sleep(0.1)

        assert len(delivered) == 1
        result = delivered[0]
        assert result.success is False
        assert result.chat_id == 111
        assert result.topic_id == 222
