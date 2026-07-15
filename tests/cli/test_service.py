"""Tests for CLIService gateway."""

from __future__ import annotations

import sys
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import AsyncMock, MagicMock, patch

from ductor_bot.cli.interactive.repl_pool import ReplFatalError
from ductor_bot.cli.process_registry import ProcessRegistry
from ductor_bot.cli.service import CLIService, CLIServiceConfig, _use_interactive
from ductor_bot.cli.stream_events import StreamEvent, ToolUseEvent
from ductor_bot.cli.types import AgentRequest, CLIResponse, Origin
from ductor_bot.config import ModelRegistry

if TYPE_CHECKING:
    import pytest


def _make_service(**overrides: Any) -> CLIService:
    config = CLIServiceConfig(
        working_dir=overrides.pop("working_dir", "/tmp"),
        default_model=overrides.pop("default_model", "opus"),
        provider=overrides.pop("provider", "claude"),
        max_turns=overrides.pop("max_turns", None),
        max_budget_usd=overrides.pop("max_budget_usd", None),
        permission_mode=overrides.pop("permission_mode", "bypassPermissions"),
    )
    models = ModelRegistry()

    return CLIService(
        config=config,
        models=models,
        available_providers=frozenset({"claude"}),
        process_registry=ProcessRegistry(),
    )


def test_use_interactive_fail_closed() -> None:
    cfg_on = CLIServiceConfig(
        working_dir="/tmp",
        default_model="opus",
        provider="claude",
        max_turns=None,
        max_budget_usd=None,
        permission_mode="bypassPermissions",
        claude_interactive=True,
    )
    cfg_off = CLIServiceConfig(
        working_dir="/tmp",
        default_model="opus",
        provider="claude",
        max_turns=None,
        max_budget_usd=None,
        permission_mode="bypassPermissions",
        claude_interactive=False,
    )

    assert _use_interactive(Origin.HUMAN_CHAT, "claude", cfg_on) is True
    assert _use_interactive(Origin.HUMAN_CHAT, "claude", cfg_off) is False
    for origin in (Origin.CRON, Origin.HEARTBEAT, Origin.MEMORY_FLUSH, Origin.COMPACT):
        assert _use_interactive(origin, "claude", cfg_on) is False
    assert _use_interactive(Origin.HUMAN_CHAT, "codex", cfg_on) is False
    assert _use_interactive("human_chat", "claude", cfg_on) is False


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

        resp = await svc.execute(AgentRequest(origin=Origin.HUMAN_CHAT, prompt="hello", chat_id=1))

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

        resp = await svc.execute(AgentRequest(origin=Origin.HUMAN_CHAT, prompt="fail", chat_id=1))

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
            AgentRequest(origin=Origin.HUMAN_CHAT, prompt="hello", chat_id=1),
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

        resp = await svc.execute_streaming(
            AgentRequest(origin=Origin.HUMAN_CHAT, prompt="hello", chat_id=1)
        )

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


async def test_interactive_stream_error_does_not_fallback_to_print() -> None:
    cfg = CLIServiceConfig(
        working_dir="/tmp",
        default_model="opus",
        provider="claude",
        max_turns=None,
        max_budget_usd=None,
        permission_mode="bypassPermissions",
        claude_interactive=True,
    )
    svc = CLIService(
        config=cfg,
        models=ModelRegistry(),
        available_providers=frozenset({"claude"}),
        process_registry=ProcessRegistry(),
    )

    async def broken_stream(*_args: object, **_kwargs: object) -> AsyncGenerator[StreamEvent, None]:
        if _kwargs.get("never"):
            yield StreamEvent(type="system")
        raise RuntimeError("repl timeout")

    with (
        patch.object(svc, "_get_repl_pool", return_value=MagicMock()),
        patch("ductor_bot.cli.service.create_cli") as mock_create,
    ):
        mock_cli = MagicMock()
        mock_cli.send_streaming = broken_stream
        mock_cli.send = AsyncMock()
        mock_create.return_value = mock_cli

        resp = await svc.execute_streaming(
            AgentRequest(origin=Origin.HUMAN_CHAT, prompt="hello", chat_id=1)
        )

    assert resp.is_error is True
    assert resp.stream_fallback is False
    mock_cli.send.assert_not_called()


def test_nonhuman_origin_does_not_get_interactive_pool() -> None:
    cfg = CLIServiceConfig(
        working_dir="/tmp",
        default_model="opus",
        provider="claude",
        max_turns=None,
        max_budget_usd=None,
        permission_mode="bypassPermissions",
        claude_interactive=True,
    )
    svc = CLIService(
        config=cfg,
        models=ModelRegistry(),
        available_providers=frozenset({"claude"}),
        process_registry=ProcessRegistry(),
    )
    with patch("ductor_bot.cli.service.create_cli") as mock_create:
        mock_create.return_value = MagicMock()
        svc._make_cli(AgentRequest(origin=Origin.HEARTBEAT, prompt="hb", chat_id=1))

    call_args = mock_create.call_args[0][0]
    assert call_args.interactive_enabled is False
    assert call_args.interactive_repl_pool is None


def test_prepare_interactive_runtime_registers_hook_and_sweeps(tmp_path: Path) -> None:
    cfg = CLIServiceConfig(
        working_dir=str(tmp_path / "workspace"),
        default_model="opus",
        provider="claude",
        max_turns=None,
        max_budget_usd=None,
        permission_mode="bypassPermissions",
        claude_interactive=True,
        agent_name="dev",
    )
    svc = CLIService(
        config=cfg,
        models=ModelRegistry(),
        available_providers=frozenset({"claude"}),
        process_registry=ProcessRegistry(),
    )
    pool = MagicMock()
    with (
        patch("ductor_bot.cli.service.ReplPool", return_value=pool),
        patch("ductor_bot.cli.service.merge_stop_hook_settings") as mock_merge,
    ):
        svc.prepare_interactive_runtime()

    pool.startup_sweep.assert_called_once_with("dev")
    settings_path = mock_merge.call_args[0][0]
    command = mock_merge.call_args.kwargs["command"]
    assert str(settings_path).endswith(".claude/settings.json")
    assert command.startswith(f"{sys.executable} -m ductor_bot.cli.interactive.stop_hook ")
    assert "--agent dev" in command


def test_update_config_disables_interactive_immediately() -> None:
    cfg_on = CLIServiceConfig(
        working_dir="/tmp",
        default_model="opus",
        provider="claude",
        max_turns=None,
        max_budget_usd=None,
        permission_mode="bypassPermissions",
        claude_interactive=True,
    )
    svc = CLIService(
        config=cfg_on,
        models=ModelRegistry(),
        available_providers=frozenset({"claude"}),
        process_registry=ProcessRegistry(),
    )
    with (
        patch.object(svc, "_get_repl_pool", return_value=MagicMock()),
        patch("ductor_bot.cli.service.create_cli") as mock_create_on,
    ):
        mock_create_on.return_value = MagicMock()
        svc._make_cli(AgentRequest(origin=Origin.HUMAN_CHAT, prompt="hi", chat_id=1))
    assert mock_create_on.call_args[0][0].interactive_enabled is True

    cfg_off = CLIServiceConfig(
        working_dir="/tmp",
        default_model="opus",
        provider="claude",
        max_turns=None,
        max_budget_usd=None,
        permission_mode="bypassPermissions",
        claude_interactive=False,
    )
    old_pool = MagicMock()
    object.__setattr__(svc, "_repl_pool", old_pool)
    svc.update_config(cfg_off)
    old_pool.kill_all.assert_called_once_with("main")
    with patch("ductor_bot.cli.service.create_cli") as mock_create:
        mock_create.return_value = MagicMock()
        svc._make_cli(AgentRequest(origin=Origin.HUMAN_CHAT, prompt="hi", chat_id=1))

    call_args = mock_create.call_args[0][0]
    assert call_args.interactive_enabled is False
    assert call_args.interactive_repl_pool is None


def test_update_config_enables_interactive_prepares_runtime() -> None:
    cfg_off = CLIServiceConfig(
        working_dir="/tmp",
        default_model="opus",
        provider="claude",
        max_turns=None,
        max_budget_usd=None,
        permission_mode="bypassPermissions",
        claude_interactive=False,
    )
    svc = CLIService(
        config=cfg_off,
        models=ModelRegistry(),
        available_providers=frozenset({"claude"}),
        process_registry=ProcessRegistry(),
    )
    cfg_on = CLIServiceConfig(
        working_dir="/tmp",
        default_model="opus",
        provider="claude",
        max_turns=None,
        max_budget_usd=None,
        permission_mode="plan",
        claude_interactive=True,
        claude_interactive_tool_denylist=("Bash",),
    )
    with patch.object(svc, "prepare_interactive_runtime") as mock_prepare:
        svc.update_config(cfg_on)

    mock_prepare.assert_called_once_with()


def test_repl_pool_config_uses_docker_paths_and_security_options(tmp_path: Path) -> None:
    cfg = CLIServiceConfig(
        working_dir=str(tmp_path / "home" / "workspace"),
        default_model="opus",
        provider="claude",
        max_turns=None,
        max_budget_usd=None,
        permission_mode="plan",
        docker_container="ductor-sandbox",
        claude_interactive=True,
        claude_interactive_tool_denylist=("Bash",),
    )
    svc = CLIService(
        config=cfg,
        models=ModelRegistry(),
        available_providers=frozenset({"claude"}),
        process_registry=ProcessRegistry(),
    )

    pool = svc._get_repl_pool()
    repl_cfg = pool._config

    assert repl_cfg.docker_container == "ductor-sandbox"
    assert repl_cfg.working_dir == tmp_path / "home" / "workspace"
    assert repl_cfg.container_working_dir == Path("/ductor/workspace")
    assert repl_cfg.container_claude_home == Path("/ductor/.claude")
    assert repl_cfg.container_signal_dir == Path("/ductor/tmp/repl-signals")
    assert repl_cfg.permission_mode == "plan"
    assert repl_cfg.tool_denylist == ("Bash",)


def test_prepare_interactive_runtime_uses_container_signal_dir_for_docker(tmp_path: Path) -> None:
    cfg = CLIServiceConfig(
        working_dir=str(tmp_path / "home" / "workspace"),
        default_model="opus",
        provider="claude",
        max_turns=None,
        max_budget_usd=None,
        permission_mode="bypassPermissions",
        docker_container="ductor-sandbox",
        claude_interactive=True,
        agent_name="dev",
    )
    svc = CLIService(
        config=cfg,
        models=ModelRegistry(),
        available_providers=frozenset({"claude"}),
        process_registry=ProcessRegistry(),
    )
    pool = MagicMock()
    with (
        patch("ductor_bot.cli.service.ReplPool", return_value=pool),
        patch("ductor_bot.cli.service.merge_stop_hook_settings") as mock_merge,
    ):
        svc.prepare_interactive_runtime()

    settings_path = mock_merge.call_args[0][0]
    command = mock_merge.call_args.kwargs["command"]
    assert settings_path == tmp_path / "home" / ".claude" / "settings.json"
    assert sys.executable not in command
    assert command.startswith("python3 /ductor/tmp/interactive-stop-hook.py ")
    assert "--signal-dir /ductor/tmp/repl-signals" in command
    copied_hook = tmp_path / "home" / "tmp" / "interactive-stop-hook.py"
    assert copied_hook.exists()
    assert "TOKEN_RE" in copied_hook.read_text(encoding="utf-8")


def test_interactive_claude_home_host_mode_matches_headless_p_path(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Host-mode: interactive uses the same dir `-p` does ($CLAUDE_CONFIG_DIR / $HOME)."""
    svc = _make_service()  # no docker_container -> host-mode

    fake_home = Path("/fake/home")
    monkeypatch.delenv("CLAUDE_CONFIG_DIR", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda _cls: fake_home))
    assert svc._interactive_claude_home() == fake_home / ".claude"

    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/custom/claude")
    assert svc._interactive_claude_home() == Path("/custom/claude")


def test_interactive_claude_home_docker_mode_maps_to_mount(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Docker-mode: host claude_home must map onto the container's /ductor/.claude.

    Regression guard: the host Stop-hook write and host transcript lookup use
    this dir, and the in-container REPL reads CLAUDE_CONFIG_DIR=/ductor/.claude.
    They must line up, so docker-mode ignores $CLAUDE_CONFIG_DIR and uses
    <ductor_home>/.claude.
    """
    # $CLAUDE_CONFIG_DIR must NOT leak into docker-mode resolution.
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/should/be/ignored")
    cfg = CLIServiceConfig(
        working_dir=str(tmp_path / "home" / "workspace"),
        default_model="opus",
        provider="claude",
        max_turns=None,
        max_budget_usd=None,
        permission_mode="bypassPermissions",
        docker_container="ductor-sandbox",
        claude_interactive=True,
    )
    svc = CLIService(
        config=cfg,
        models=ModelRegistry(),
        available_providers=frozenset({"claude"}),
        process_registry=ProcessRegistry(),
    )

    assert svc._interactive_claude_home() == tmp_path / "home" / ".claude"
    repl_cfg = svc._get_repl_pool()._config
    assert repl_cfg.claude_home == tmp_path / "home" / ".claude"
    assert repl_cfg.container_claude_home == Path("/ductor/.claude")


async def test_interactive_fatal_error_surfaces_reason() -> None:
    """A ReplFatalError (auth/login failure) must reach the user as an error."""
    cfg = CLIServiceConfig(
        working_dir="/tmp",
        default_model="opus",
        provider="claude",
        max_turns=None,
        max_budget_usd=None,
        permission_mode="bypassPermissions",
        claude_interactive=True,
    )
    svc = CLIService(
        config=cfg,
        models=ModelRegistry(),
        available_providers=frozenset({"claude"}),
        process_registry=ProcessRegistry(),
    )

    async def fatal_stream(*_a: object, **_k: object) -> AsyncGenerator[StreamEvent, None]:
        if _k.get("never"):
            yield StreamEvent(type="system")
        raise ReplFatalError("REPL turn failed: not logged in (run /login)")

    with (
        patch.object(svc, "_get_repl_pool", return_value=MagicMock()),
        patch("ductor_bot.cli.service.create_cli") as mock_create,
    ):
        mock_cli = MagicMock()
        mock_cli.send_streaming = fatal_stream
        mock_cli.send = AsyncMock()
        mock_create.return_value = mock_cli

        resp = await svc.execute_streaming(
            AgentRequest(origin=Origin.HUMAN_CHAT, prompt="hi", chat_id=1)
        )

    assert resp.is_error is True
    assert "run /login" in resp.result
    mock_cli.send.assert_not_called()


async def test_execute_surfaces_repl_fatal_error() -> None:
    """The non-streaming execute() path must also surface a ReplFatalError."""
    cfg = CLIServiceConfig(
        working_dir="/tmp",
        default_model="opus",
        provider="claude",
        max_turns=None,
        max_budget_usd=None,
        permission_mode="bypassPermissions",
        claude_interactive=True,
    )
    svc = CLIService(
        config=cfg,
        models=ModelRegistry(),
        available_providers=frozenset({"claude"}),
        process_registry=ProcessRegistry(),
    )

    with (
        patch.object(svc, "_get_repl_pool", return_value=MagicMock()),
        patch("ductor_bot.cli.service.create_cli") as mock_create,
    ):
        mock_cli = MagicMock()
        mock_cli.send = AsyncMock(
            side_effect=ReplFatalError("REPL turn failed: API error 401 (auth)")
        )
        mock_create.return_value = mock_cli

        resp = await svc.execute(AgentRequest(origin=Origin.HUMAN_CHAT, prompt="hi", chat_id=1))

    assert resp.is_error is True
    assert "401" in resp.result
