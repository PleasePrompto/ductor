"""Tests for CLIService gateway."""

from __future__ import annotations

from collections.abc import AsyncGenerator
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from ductor_bot.cli.process_registry import ProcessRegistry
from ductor_bot.cli.service import CLIService, CLIServiceConfig
from ductor_bot.cli.stream_events import StreamEvent, ToolUseEvent
from ductor_bot.cli.types import AgentRequest, CLIResponse
from ductor_bot.config import ModelRegistry


def _make_service(**overrides: Any) -> CLIService:
    config = CLIServiceConfig(
        working_dir=overrides.pop("working_dir", "/tmp"),
        default_model=overrides.pop("default_model", "opus"),
        provider=overrides.pop("provider", "claude"),
        max_turns=overrides.pop("max_turns", None),
        max_budget_usd=overrides.pop("max_budget_usd", None),
        permission_mode=overrides.pop("permission_mode", "bypassPermissions"),
        docker_container=overrides.pop("docker_container", ""),
    )
    models = ModelRegistry()

    return CLIService(
        config=config,
        models=models,
        available_providers=frozenset({"claude"}),
        process_registry=ProcessRegistry(),
    )


async def test_execute_returns_agent_response() -> None:
    svc = _make_service()
    mock_response = CLIResponse(
        result="Hello!",
        session_id="sess-1",
        total_cost_usd=0.05,
        usage={"input_tokens": 500, "output_tokens": 200},
    )
    with patch("ductor_bot.cli.service.create_cli") as mock_create:
        mock_cli = AsyncMock()
        mock_cli.send.return_value = mock_response
        mock_create.return_value = mock_cli

        resp = await svc.execute(AgentRequest(prompt="hello", chat_id=1))

    assert resp.result == "Hello!"
    assert resp.session_id == "sess-1"
    assert resp.cost_usd == 0.05
    assert resp.is_error is False


async def test_execute_error_response() -> None:
    svc = _make_service()
    mock_response = CLIResponse(result="Error occurred", is_error=True)
    with patch("ductor_bot.cli.service.create_cli") as mock_create:
        mock_cli = AsyncMock()
        mock_cli.send.return_value = mock_response
        mock_create.return_value = mock_cli

        resp = await svc.execute(AgentRequest(prompt="fail", chat_id=1))

    assert resp.is_error is True
    assert resp.result == "Error occurred"


async def test_execute_streaming_success() -> None:
    svc = _make_service()

    from ductor_bot.cli.stream_events import AssistantTextDelta, ResultEvent, ThinkingEvent

    async def fake_stream(*_args: Any, **_kwargs: Any) -> AsyncGenerator[StreamEvent, None]:
        yield ThinkingEvent(type="assistant", text="considering")
        yield AssistantTextDelta(type="assistant", text="Hello ")
        yield AssistantTextDelta(type="assistant", text="world!")
        yield ResultEvent(
            type="result",
            session_id="sess-1",
            result="Hello world!",
            total_cost_usd=0.03,
            usage={"input_tokens": 100, "output_tokens": 50},
        )

    deltas: list[str] = []
    thinking: list[str] = []

    async def on_delta(text: str) -> None:
        deltas.append(text)

    async def on_thinking(text: str) -> None:
        thinking.append(text)

    with patch("ductor_bot.cli.service.create_cli") as mock_create:
        mock_cli = MagicMock()
        mock_cli.send_streaming = fake_stream
        mock_create.return_value = mock_cli

        resp = await svc.execute_streaming(
            AgentRequest(prompt="hello", chat_id=1),
            on_text_delta=on_delta,
            on_thinking_delta=on_thinking,
        )

    assert resp.result == "Hello world!"
    assert resp.session_id == "sess-1"
    assert thinking == ["considering"]
    assert deltas == ["Hello ", "world!"]


async def test_execute_streaming_fallback_on_error() -> None:
    svc = _make_service()

    mock_response = CLIResponse(result="Fallback result", session_id="sess-2")
    with patch("ductor_bot.cli.service.create_cli") as mock_create:
        mock_cli = MagicMock()
        mock_cli.send_streaming = MagicMock(side_effect=RuntimeError("Stream broken"))
        mock_cli.send = AsyncMock(return_value=mock_response)
        mock_create.return_value = mock_cli

        resp = await svc.execute_streaming(AgentRequest(prompt="hello", chat_id=1))

    assert resp.stream_fallback is True
    assert resp.result == "Fallback result"


def test_update_default_model() -> None:
    svc = _make_service()
    svc.update_default_model("sonnet")
    assert svc._config.default_model == "sonnet"


def test_update_available_providers() -> None:
    svc = _make_service()
    svc.update_available_providers(frozenset({"claude", "codex"}))
    assert svc._available_providers == frozenset({"claude", "codex"})


def test_cli_parameters_for_antigravity() -> None:
    cfg = CLIServiceConfig(
        working_dir="/tmp",
        default_model="antigravity-default",
        provider="antigravity",
        max_turns=None,
        max_budget_usd=None,
        permission_mode="bypassPermissions",
        antigravity_cli_parameters=("--log-file", "agy.log"),
    )

    assert cfg.cli_parameters_for_provider("antigravity") == ["--log-file", "agy.log"]


async def test_stream_callbacks_dispatches_compact_boundary() -> None:
    """CompactBoundaryEvent fires on_compact_boundary and on_status(None), in order."""
    from ductor_bot.cli.service import _StreamCallbacks
    from ductor_bot.cli.stream_events import CompactBoundaryEvent

    order: list[str] = []

    async def on_boundary() -> None:
        order.append("boundary")

    async def on_status(status: str | None) -> None:
        order.append(f"status:{status}")

    cbs = _StreamCallbacks(
        on_text=None,
        on_thinking=None,
        on_tool=None,
        on_status=on_status,
        on_compact_boundary=on_boundary,
    )
    event = CompactBoundaryEvent(
        type="system", subtype="compact_boundary", trigger="auto", pre_tokens=12345
    )
    text, result = await cbs.dispatch(event)

    assert text == ""
    assert result is None
    assert order == ["boundary", "status:None"]


async def test_stream_callbacks_dispatches_thinking_text() -> None:
    from ductor_bot.cli.service import _StreamCallbacks
    from ductor_bot.cli.stream_events import ThinkingEvent

    seen: list[str] = []
    statuses: list[str | None] = []

    async def on_thinking(text: str) -> None:
        seen.append(text)

    async def on_status(status: str | None) -> None:
        statuses.append(status)

    cbs = _StreamCallbacks(
        on_text=None,
        on_thinking=on_thinking,
        on_tool=None,
        on_status=on_status,
    )
    text, result = await cbs.dispatch(ThinkingEvent(type="assistant", text="step 1"))

    assert text == ""
    assert result is None
    assert seen == ["step 1"]
    assert statuses == ["thinking"]


async def test_stream_callbacks_dispatch_tool_event() -> None:
    from ductor_bot.cli.service import _StreamCallbacks

    seen: list[ToolUseEvent] = []

    async def on_tool(event: ToolUseEvent) -> None:
        seen.append(event)

    cbs = _StreamCallbacks(
        on_text=None,
        on_thinking=None,
        on_tool=on_tool,
        on_status=None,
    )
    event = ToolUseEvent(
        type="assistant",
        tool_name="WebFetch",
        parameters={"url": "https://slack.dev/slack-thinking-steps-ai-agents/"},
    )
    text, result = await cbs.dispatch(event)

    assert text == ""
    assert result is None
    assert seen == [event]


def test_make_cli_uses_working_dir_resolver() -> None:
    svc = _make_service(working_dir="/default/workspace")
    svc.set_working_dir_resolver(lambda _req: "/projects/alpha")

    with patch("ductor_bot.cli.service.create_cli") as mock_create:
        svc._make_cli(AgentRequest(prompt="hi", chat_id=1, topic_id=5))

    cli_config = mock_create.call_args.args[0]
    assert cli_config.working_dir == "/projects/alpha"


def test_make_cli_resolver_none_keeps_default_working_dir() -> None:
    svc = _make_service(working_dir="/default/workspace")
    svc.set_working_dir_resolver(lambda _req: None)

    with patch("ductor_bot.cli.service.create_cli") as mock_create:
        svc._make_cli(AgentRequest(prompt="hi", chat_id=1, topic_id=5))

    cli_config = mock_create.call_args.args[0]
    assert cli_config.working_dir == "/default/workspace"


def test_make_cli_no_resolver_keeps_default_working_dir() -> None:
    svc = _make_service(working_dir="/default/workspace")

    with patch("ductor_bot.cli.service.create_cli") as mock_create:
        svc._make_cli(AgentRequest(prompt="hi", chat_id=1))

    cli_config = mock_create.call_args.args[0]
    assert cli_config.working_dir == "/default/workspace"


def test_make_cli_docker_mode_ignores_resolver() -> None:
    svc = _make_service(working_dir="/default/workspace", docker_container="ductor-sandbox")
    calls: list[AgentRequest] = []

    def resolver(req: AgentRequest) -> str:
        calls.append(req)
        return "/projects/alpha"

    svc.set_working_dir_resolver(resolver)

    with patch("ductor_bot.cli.service.create_cli") as mock_create:
        svc._make_cli(AgentRequest(prompt="hi", chat_id=1, topic_id=5))

    cli_config = mock_create.call_args.args[0]
    assert cli_config.working_dir == "/default/workspace"
    assert calls == []  # resolver must not even be consulted in docker mode


def test_make_cli_override_appends_absolute_memory_note() -> None:
    """With a cwd override the agent is told the absolute bot-memory path."""
    svc = _make_service(working_dir="/default/workspace")
    svc.set_working_dir_resolver(lambda _req: "/projects/alpha")

    with patch("ductor_bot.cli.service.create_cli") as mock_create:
        svc._make_cli(
            AgentRequest(prompt="hi", chat_id=1, topic_id=5, append_system_prompt="MEMORY DUMP")
        )

    cli_config = mock_create.call_args.args[0]
    assert cli_config.working_dir == "/projects/alpha"
    assert cli_config.append_system_prompt.startswith("MEMORY DUMP")
    assert "/default/workspace/memory_system/MAINMEMORY.md" in cli_config.append_system_prompt


def test_make_cli_memory_flush_in_project_root_keeps_workspace_memory_path() -> None:
    """Regression (#178): flush/compact/heartbeat resume in the project cwd and
    must keep writing bot memory into the workspace, not the user's repo."""
    svc = _make_service(working_dir="/default/workspace")
    svc.set_working_dir_resolver(lambda _req: "/projects/alpha")

    with patch("ductor_bot.cli.service.create_cli") as mock_create:
        svc._make_cli(
            AgentRequest(prompt="flush", chat_id=1, topic_id=5, process_label="memory_flush")
        )

    cli_config = mock_create.call_args.args[0]
    assert "/default/workspace/memory_system/MAINMEMORY.md" in cli_config.append_system_prompt


def test_make_cli_no_override_leaves_append_prompt_untouched() -> None:
    svc = _make_service(working_dir="/default/workspace")
    svc.set_working_dir_resolver(lambda _req: None)

    with patch("ductor_bot.cli.service.create_cli") as mock_create:
        svc._make_cli(AgentRequest(prompt="hi", chat_id=1, append_system_prompt="MEMORY DUMP"))

    cli_config = mock_create.call_args.args[0]
    assert cli_config.append_system_prompt == "MEMORY DUMP"


def test_docker_enabled_property() -> None:
    assert _make_service().docker_enabled is False
    assert _make_service(docker_container="ductor-sandbox").docker_enabled is True
