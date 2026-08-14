"""Unit tests for Telegram message dispatch helpers."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ductor_bot.messenger.telegram.message_dispatch import (
    _REACTION_DEFAULT,
    _REACTION_SYSTEM,
    _REACTION_THINKING,
    NonStreamingDispatch,
    ReactionTracker,
    StreamingDispatch,
    run_non_streaming_message,
    run_streaming_message,
)
from ductor_bot.orchestrator.registry import OrchestratorResult
from ductor_bot.orchestrator.selectors.models import Button, ButtonGrid
from ductor_bot.session.key import SessionKey


def _make_bot() -> MagicMock:
    bot = MagicMock()
    bot.set_message_reaction = AsyncMock()
    return bot


def _emitted_emojis(bot: MagicMock) -> list[str | None]:
    """Extract the emoji arg from each set_message_reaction call.

    Returns None for "clear" calls (empty reaction list) and the emoji
    string otherwise. Assumes every call had exactly one ReactionTypeEmoji.
    """
    out: list[str | None] = []
    for call in bot.set_message_reaction.call_args_list:
        reactions = call.kwargs.get("reaction", [])
        if not reactions:
            out.append(None)
        else:
            out.append(reactions[0].emoji)
    return out


async def test_reaction_tracker_disabled_is_noop() -> None:
    bot = _make_bot()
    tracker = ReactionTracker(bot, chat_id=1, message_id=42, enabled=False)

    await tracker.set_thinking()
    await tracker.set_tool("Read")
    await tracker.set_system()
    await tracker.clear()

    bot.set_message_reaction.assert_not_awaited()


async def test_reaction_tracker_stages_map_to_emoji() -> None:
    bot = _make_bot()
    tracker = ReactionTracker(bot, chat_id=1, message_id=42, enabled=True)

    await tracker.set_thinking()
    await tracker.set_tool("Read")  # 👀
    await tracker.set_tool("Edit")  # ✍️
    await tracker.set_tool("Bash")  # 👨‍💻
    await tracker.set_tool("UnknownTool")  # fallback → default (🤔)
    await tracker.set_system()
    await tracker.clear()

    emitted = _emitted_emojis(bot)
    assert emitted == [
        _REACTION_THINKING,
        "\U0001f440",
        "✍️",
        "\U0001f468‍\U0001f4bb",
        _REACTION_DEFAULT,
        _REACTION_SYSTEM,
        None,  # clear emits empty reaction list
    ]


async def test_reaction_tracker_dedups_consecutive_same_stage() -> None:
    bot = _make_bot()
    tracker = ReactionTracker(bot, chat_id=1, message_id=42, enabled=True)

    await tracker.set_thinking()
    await tracker.set_thinking()  # dedup: no second call
    await tracker.set_thinking()  # dedup: no third call

    assert bot.set_message_reaction.await_count == 1


async def test_reaction_tracker_swallows_errors() -> None:
    bot = _make_bot()
    bot.set_message_reaction.side_effect = RuntimeError("bad request")
    tracker = ReactionTracker(bot, chat_id=1, message_id=42, enabled=True)

    # Must not raise despite the underlying call raising.
    await tracker.set_thinking()
    await tracker.set_tool("Edit")
    await tracker.clear()

    # Every call still attempted the bot API — it just did not propagate.
    assert bot.set_message_reaction.await_count >= 1


async def test_lifecycle_reactions_replace_with_success_or_warning() -> None:
    bot = _make_bot()
    tracker = ReactionTracker(bot, chat_id=1, message_id=42, enabled=True, lifecycle=True)

    await tracker.set_thinking()
    await tracker.set_success()
    await tracker.set_warning()
    await tracker.clear()

    assert _emitted_emojis(bot) == ["🤔", "🎉", "😨", None]


async def test_lifecycle_system_progression_keeps_the_original_message_reaction() -> None:
    bot = _make_bot()
    tracker = ReactionTracker(bot, chat_id=1, message_id=42, enabled=True, lifecycle=True)

    # The Telegram app has already placed 👀 on this same user message.
    await tracker.set_system()
    await tracker.set_success()

    assert _emitted_emojis(bot) == ["🤔", "🎉"]
    assert {call.kwargs["message_id"] for call in bot.set_message_reaction.call_args_list} == {42}


async def test_lifecycle_api_failure_does_not_block_later_valid_stage() -> None:
    bot = _make_bot()
    bot.set_message_reaction.side_effect = [RuntimeError("invalid reaction"), None]
    tracker = ReactionTracker(bot, chat_id=1, message_id=42, enabled=True, lifecycle=True)

    await tracker.set_thinking()
    await tracker.set_warning()

    assert _emitted_emojis(bot) == ["🤔", "😨"]
    assert bot.set_message_reaction.await_count == 2


async def test_reaction_retries_identical_stage_after_failure() -> None:
    bot = _make_bot()
    bot.set_message_reaction.side_effect = [RuntimeError("temporary"), None]
    tracker = ReactionTracker(bot, chat_id=1, message_id=42, enabled=True)

    await tracker.set_thinking()
    await tracker.set_thinking()

    assert _emitted_emojis(bot) == [_REACTION_THINKING, _REACTION_THINKING]
    assert tracker._current == _REACTION_THINKING


async def test_non_streaming_reacts_on_trigger_message_not_reply_to() -> None:
    """MED #10: reaction anchors on the user's current trigger, not reply_to.

    Previously ``run_non_streaming_message`` used ``reply_to.message_id``
    for the tracker. When ``reply_to`` pointed at a prior bot message
    (e.g., the message quoted in a user reply) the reaction landed on the
    wrong message, diverging from the streaming path which always uses
    the current trigger.
    """
    bot = _make_bot()

    trigger = MagicMock()
    trigger.message_id = 777  # user's current message

    replied_to = MagicMock()
    replied_to.message_id = 123  # prior bot message the user replied to

    scene = MagicMock()
    scene.status_reaction = True
    scene.technical_footer = False

    orchestrator = MagicMock()
    result = MagicMock()
    result.text = "reply"
    result.model_name = None
    orchestrator.handle_message = AsyncMock(return_value=result)

    dispatch = NonStreamingDispatch(
        bot=bot,
        orchestrator=orchestrator,
        key=SessionKey(chat_id=1),
        text="hello",
        allowed_roots=[Path("/tmp")],
        message=trigger,
        reply_to=replied_to,
        scene_config=scene,
    )

    with (
        patch(
            "ductor_bot.messenger.telegram.message_dispatch.send_rich",
            new_callable=AsyncMock,
        ),
        patch("ductor_bot.messenger.telegram.message_dispatch.TypingContext") as typing_ctx,
    ):
        typing_ctx.return_value.__aenter__ = AsyncMock()
        typing_ctx.return_value.__aexit__ = AsyncMock()
        await run_non_streaming_message(dispatch)

    # Every reaction call must target the trigger message, never reply_to.
    assert bot.set_message_reaction.await_count >= 1
    for call in bot.set_message_reaction.call_args_list:
        assert call.kwargs["message_id"] == 777, (
            f"expected reaction on trigger (777), got {call.kwargs['message_id']}"
        )


async def test_non_streaming_lifecycle_marks_success_after_delivery() -> None:
    bot = _make_bot()
    trigger = MagicMock(message_id=777)
    scene = MagicMock(status_reaction=False, seen_reaction=True, technical_footer=False)
    orchestrator = MagicMock()
    orchestrator.handle_message = AsyncMock(return_value=OrchestratorResult(text="reply"))
    dispatch = NonStreamingDispatch(
        bot=bot, orchestrator=orchestrator, key=SessionKey(chat_id=1), text="hello",
        allowed_roots=None, message=trigger, reply_to=trigger, scene_config=scene,
    )
    with patch("ductor_bot.messenger.telegram.message_dispatch.send_rich", new_callable=AsyncMock, return_value=True):
        await run_non_streaming_message(dispatch)

    assert _emitted_emojis(bot) == ["🤔", "🎉"]


async def test_non_streaming_immediate_selector_delivers_its_keyboard() -> None:
    """Ordinary text routes must retain selector controls, not just commands."""
    bot = _make_bot()
    trigger = MagicMock(message_id=55)
    orch = MagicMock()
    orch.handle_message = AsyncMock(
        return_value=OrchestratorResult(
            text="search results",
            buttons=ButtonGrid(rows=[[Button("📎 Attach & use #1", "nsc:cxsn:0:0:0:0:0")]]),
        )
    )
    dispatch = NonStreamingDispatch(
        bot=bot, orchestrator=orch, key=SessionKey(chat_id=1), text="PicoClaw",
        allowed_roots=None, message=trigger,
    )
    with (
        patch("ductor_bot.messenger.telegram.message_dispatch.send_rich", new_callable=AsyncMock) as send,
        patch("ductor_bot.messenger.telegram.message_dispatch.TypingContext") as typing_ctx,
    ):
        typing_ctx.return_value.__aenter__ = AsyncMock()
        typing_ctx.return_value.__aexit__ = AsyncMock()
        await run_non_streaming_message(dispatch)

    send.assert_awaited_once()
    markup = send.await_args.args[3].reply_markup
    assert markup is not None
    assert markup.inline_keyboard[0][0].callback_data == "nsc:cxsn:0:0:0:0:0"


async def test_streaming_immediate_selector_fallback_delivers_keyboard_once() -> None:
    """Streaming-enabled search has no deltas, so its fallback owns the keyboard."""
    from ductor_bot.config import StreamingConfig

    bot = _make_bot()
    message = MagicMock(message_id=55)
    editor = MagicMock(has_content=False)
    editor.append_text = AsyncMock()
    editor.append_tool = AsyncMock()
    editor.append_system = AsyncMock()
    editor.finalize = AsyncMock()
    orch = MagicMock()
    orch.handle_message_streaming = AsyncMock(
        return_value=OrchestratorResult(
            text="search results",
            buttons=ButtonGrid(rows=[[Button("📎 Attach & use #1", "nsc:cxsn:0:0:0:0:0")]]),
        )
    )
    dispatch = StreamingDispatch(
        bot=bot, orchestrator=orch, message=message, key=SessionKey(chat_id=1), text="PicoClaw",
        streaming_cfg=StreamingConfig(), allowed_roots=None,
    )
    with (
        patch("ductor_bot.messenger.telegram.message_dispatch.create_stream_editor", return_value=editor),
        patch("ductor_bot.messenger.telegram.message_dispatch.send_rich", new_callable=AsyncMock) as send,
        patch("ductor_bot.messenger.telegram.message_dispatch.TypingContext") as typing_ctx,
    ):
        typing_ctx.return_value.__aenter__ = AsyncMock()
        typing_ctx.return_value.__aexit__ = AsyncMock()
        await run_streaming_message(dispatch)

    send.assert_awaited_once()
    markup = send.await_args.args[3].reply_markup
    assert markup is not None
    assert markup.inline_keyboard[0][0].callback_data == "nsc:cxsn:0:0:0:0:0"


async def test_streamed_provider_reply_does_not_send_selector_keyboard_or_duplicate() -> None:
    """A real streamed reply remains editor-owned even if metadata has buttons."""
    from ductor_bot.config import StreamingConfig

    bot = _make_bot()
    message = MagicMock(message_id=55)
    editor = MagicMock(has_content=True)
    editor.append_text = AsyncMock()
    editor.append_tool = AsyncMock()
    editor.append_system = AsyncMock()
    editor.finalize = AsyncMock()
    orch = MagicMock()

    async def stream(*_args, **kwargs):
        await kwargs["on_text_delta"]("provider reply")
        return OrchestratorResult(
            text="provider reply",
            buttons=ButtonGrid(rows=[[Button("unexpected", "nsc:r")]]),
        )

    orch.handle_message_streaming = AsyncMock(side_effect=stream)
    dispatch = StreamingDispatch(
        bot=bot, orchestrator=orch, message=message, key=SessionKey(chat_id=1), text="hello",
        streaming_cfg=StreamingConfig(min_chars=1), allowed_roots=None,
    )
    with (
        patch("ductor_bot.messenger.telegram.message_dispatch.create_stream_editor", return_value=editor),
        patch("ductor_bot.messenger.telegram.message_dispatch.send_rich", new_callable=AsyncMock) as send,
        patch(
            "ductor_bot.messenger.telegram.message_dispatch.send_files_from_text", new_callable=AsyncMock
        ) as send_files,
        patch("ductor_bot.messenger.telegram.message_dispatch.TypingContext") as typing_ctx,
    ):
        typing_ctx.return_value.__aenter__ = AsyncMock()
        typing_ctx.return_value.__aexit__ = AsyncMock()
        await run_streaming_message(dispatch)

    send.assert_not_awaited()
    send_files.assert_awaited_once()


async def test_streaming_lifecycle_marks_warning_when_fallback_delivery_has_no_content() -> None:
    from ductor_bot.config import StreamingConfig

    bot = _make_bot()
    message = MagicMock(message_id=55)
    editor = MagicMock(has_content=False)
    editor.append_text = AsyncMock()
    editor.append_tool = AsyncMock()
    editor.append_system = AsyncMock()
    editor.finalize = AsyncMock()
    orch = MagicMock()
    orch.handle_message_streaming = AsyncMock(return_value=OrchestratorResult(text="fallback"))
    scene = MagicMock(status_reaction=False, seen_reaction=True, technical_footer=False)
    dispatch = StreamingDispatch(
        bot=bot,
        orchestrator=orch,
        message=message,
        key=SessionKey(chat_id=1),
        text="hello",
        streaming_cfg=StreamingConfig(),
        allowed_roots=None,
        scene_config=scene,
    )
    with (
        patch("ductor_bot.messenger.telegram.message_dispatch.create_stream_editor", return_value=editor),
        patch(
            "ductor_bot.messenger.telegram.message_dispatch.send_rich",
            new_callable=AsyncMock,
            return_value=False,
        ),
        patch("ductor_bot.messenger.telegram.message_dispatch.TypingContext") as typing_ctx,
    ):
        typing_ctx.return_value.__aenter__ = AsyncMock()
        typing_ctx.return_value.__aexit__ = AsyncMock()
        await run_streaming_message(dispatch)

    assert _emitted_emojis(bot) == ["🤔", "😨"]


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))


async def test_streaming_reasoning_only_still_sends_final_answer() -> None:
    """Reasoning text must never suppress the final Telegram answer."""
    from ductor_bot.config import StreamingConfig

    bot = _make_bot()
    bot.send_message = AsyncMock(return_value=MagicMock())
    message = MagicMock()
    message.message_id = 55

    editor = MagicMock()
    editor.has_content = False
    editor.append_text = AsyncMock()
    editor.append_tool = AsyncMock()
    editor.append_system = AsyncMock()
    editor.finalize = AsyncMock()

    orch = MagicMock()

    async def _handle_streaming(*_args, **kwargs):
        await kwargs["on_reasoning_delta"]("I am thinking through the patch")
        return OrchestratorResult(text="Final answer delivered")

    orch.handle_message_streaming = AsyncMock(side_effect=_handle_streaming)

    dispatch = StreamingDispatch(
        bot=bot,
        orchestrator=orch,
        message=message,
        key=SessionKey(chat_id=1),
        text="hello",
        streaming_cfg=StreamingConfig(
            show_reasoning_stream=True,
            show_tool_progress=True,
            show_thinking_indicator=False,
        ),
        allowed_roots=[Path("/tmp")],
    )

    with (
        patch(
            "ductor_bot.messenger.telegram.message_dispatch.create_stream_editor",
            return_value=editor,
        ),
        patch(
            "ductor_bot.messenger.telegram.message_dispatch.send_rich",
            new_callable=AsyncMock,
        ) as send_rich_mock,
        patch(
            "ductor_bot.messenger.telegram.message_dispatch.send_files_from_text",
            new_callable=AsyncMock,
        ) as send_files_mock,
        patch("ductor_bot.messenger.telegram.message_dispatch.TypingContext") as typing_ctx,
    ):
        typing_ctx.return_value.__aenter__ = AsyncMock()
        typing_ctx.return_value.__aexit__ = AsyncMock()

        result = await run_streaming_message(dispatch)

    assert result == "Final answer delivered"
    send_rich_mock.assert_awaited_once()
    assert send_rich_mock.await_args.args[2] == "Final answer delivered"
    send_files_mock.assert_not_awaited()


async def test_streaming_tool_progress_can_be_disabled() -> None:
    from ductor_bot.config import StreamingConfig

    bot = _make_bot()
    message = MagicMock()
    message.message_id = 77

    editor = MagicMock()
    editor.has_content = False
    editor.append_text = AsyncMock()
    editor.append_tool = AsyncMock()
    editor.append_system = AsyncMock()
    editor.finalize = AsyncMock()

    orch = MagicMock()

    async def _handle_streaming(*_args, **kwargs):
        await kwargs["on_tool_activity"]("Read")
        return OrchestratorResult(text="done", stream_fallback=True)

    orch.handle_message_streaming = AsyncMock(side_effect=_handle_streaming)

    dispatch = StreamingDispatch(
        bot=bot,
        orchestrator=orch,
        message=message,
        key=SessionKey(chat_id=1),
        text="hello",
        streaming_cfg=StreamingConfig(show_tool_progress=False),
        allowed_roots=[Path("/tmp")],
    )

    with (
        patch(
            "ductor_bot.messenger.telegram.message_dispatch.create_stream_editor",
            return_value=editor,
        ),
        patch(
            "ductor_bot.messenger.telegram.message_dispatch.send_rich",
            new_callable=AsyncMock,
        ),
        patch(
            "ductor_bot.messenger.telegram.message_dispatch.send_files_from_text",
            new_callable=AsyncMock,
        ),
        patch("ductor_bot.messenger.telegram.message_dispatch.TypingContext") as typing_ctx,
    ):
        typing_ctx.return_value.__aenter__ = AsyncMock()
        typing_ctx.return_value.__aexit__ = AsyncMock()
        await run_streaming_message(dispatch)

    editor.append_tool.assert_not_awaited()
