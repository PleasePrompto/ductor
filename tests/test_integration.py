"""Integration tests: real objects wired together, only CLI subprocess mocked."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ductor_bot.cli.types import AgentResponse, CLIResponse
from ductor_bot.config import AgentConfig
from ductor_bot.orchestrator.core import Orchestrator
from ductor_bot.orchestrator.hooks import MessageHook
from ductor_bot.orchestrator.registry import OrchestratorResult
from ductor_bot.session import SessionManager
from ductor_bot.workspace.init import init_workspace
from ductor_bot.workspace.paths import DuctorPaths

CHAT_ID = 12345


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _setup_framework(fw_root: Path) -> None:
    """Create minimal home-defaults template (mirrors conftest.setup_framework)."""
    ws = fw_root / "workspace"
    ws.mkdir(parents=True)
    (ws / "CLAUDE.md").write_text("# Ductor Home")

    config_dir = ws / "config"
    config_dir.mkdir()

    inner = ws / "workspace"
    inner.mkdir()
    (inner / "CLAUDE.md").write_text("# Framework CLAUDE.md")

    for subdir in ("memory_system", "cron_tasks", "output_to_user", "telegram_files"):
        d = inner / subdir
        d.mkdir()
        (d / "CLAUDE.md").write_text(f"# {subdir}")

    (inner / "memory_system" / "MAINMEMORY.md").write_text("# Main Memory\n")

    tools = inner / "tools"
    tools.mkdir()
    (tools / "CLAUDE.md").write_text("# Tools")

    (fw_root / "config.example.json").write_text('{"provider": "claude", "model": "opus"}')


@pytest.fixture
def workspace(tmp_path: Path) -> tuple[DuctorPaths, AgentConfig]:
    fw_root = tmp_path / "fw"
    _setup_framework(fw_root)
    paths = DuctorPaths(
        ductor_home=tmp_path / "home", home_defaults=fw_root / "workspace", framework_root=fw_root
    )
    init_workspace(paths)
    config = AgentConfig()
    return paths, config


def _make_cli_response(
    result: str = "Hello from the agent!",
    session_id: str = "sess-abc-123",
    *,
    is_error: bool = False,
    cost: float = 0.01,
    tokens: int = 500,
) -> CLIResponse:
    return CLIResponse(
        session_id=session_id,
        result=result,
        is_error=is_error,
        total_cost_usd=cost,
        usage={"input_tokens": tokens // 2, "output_tokens": tokens // 2},
    )


def _make_agent_response(
    result: str = "Hello from the agent!",
    session_id: str = "sess-abc-123",
    *,
    is_error: bool = False,
    cost: float = 0.01,
    tokens: int = 500,
) -> AgentResponse:
    return AgentResponse(
        result=result,
        session_id=session_id,
        is_error=is_error,
        cost_usd=cost,
        total_tokens=tokens,
    )


@pytest.fixture
def orch_with_mock_cli(
    workspace: tuple[DuctorPaths, AgentConfig],
) -> tuple[Orchestrator, AsyncMock]:
    """Real Orchestrator with the CLIService.execute/execute_streaming mocked.

    Returns (orchestrator, mock_execute) so tests can configure return values.
    """
    paths, config = workspace
    o = Orchestrator(config, paths)
    o._available_providers = frozenset({"claude"})
    o._cli_service.update_available_providers(frozenset({"claude"}))

    mock_execute = AsyncMock(return_value=_make_agent_response())
    mock_execute_streaming = AsyncMock(return_value=_make_agent_response())

    object.__setattr__(o._cli_service, "execute", mock_execute)
    object.__setattr__(o._cli_service, "execute_streaming", mock_execute_streaming)

    return o, mock_execute


# ---------------------------------------------------------------------------
# Test 1: Normal message flow (non-streaming)
# ---------------------------------------------------------------------------


class TestNormalMessageFlow:
    async def test_normal_message_returns_agent_text(
        self, orch_with_mock_cli: tuple[Orchestrator, AsyncMock]
    ) -> None:
        orch, mock_execute = orch_with_mock_cli

        result = await orch.handle_message(CHAT_ID, "What is the weather?")

        assert isinstance(result, OrchestratorResult)
        assert result.text == "Hello from the agent!"
        mock_execute.assert_awaited_once()

    async def test_normal_message_creates_session(
        self, orch_with_mock_cli: tuple[Orchestrator, AsyncMock]
    ) -> None:
        orch, _ = orch_with_mock_cli

        await orch.handle_message(CHAT_ID, "Hello")

        session = await orch._sessions.get_active(CHAT_ID)
        assert session is not None
        assert session.chat_id == CHAT_ID
        assert session.message_count == 1

    async def test_normal_message_resumes_existing_session(
        self, orch_with_mock_cli: tuple[Orchestrator, AsyncMock]
    ) -> None:
        orch, mock_execute = orch_with_mock_cli

        await orch.handle_message(CHAT_ID, "First message")

        mock_execute.reset_mock()
        mock_execute.return_value = _make_agent_response(result="Second reply")

        await orch.handle_message(CHAT_ID, "Second message")

        call_args = mock_execute.call_args
        request = call_args[0][0]
        assert request.resume_session == "sess-abc-123"

        session = await orch._sessions.get_active(CHAT_ID)
        assert session is not None
        assert session.message_count == 2

    async def test_session_tracks_cost_and_tokens(
        self, orch_with_mock_cli: tuple[Orchestrator, AsyncMock]
    ) -> None:
        orch, mock_execute = orch_with_mock_cli
        mock_execute.return_value = _make_agent_response(cost=0.05, tokens=1000)

        await orch.handle_message(CHAT_ID, "Expensive query")

        session = await orch._sessions.get_active(CHAT_ID)
        assert session is not None
        assert session.total_cost_usd == pytest.approx(0.05)
        assert session.total_tokens == 1000


# ---------------------------------------------------------------------------
# Test 2: Command routing
# ---------------------------------------------------------------------------


class TestCommandRouting:
    async def test_status_command(self, orch_with_mock_cli: tuple[Orchestrator, AsyncMock]) -> None:
        orch, mock_execute = orch_with_mock_cli

        with patch("ductor_bot.orchestrator.commands.check_all_auth", return_value={}):
            result = await orch.handle_message(CHAT_ID, "/status")

        assert "**Status**" in result.text
        mock_execute.assert_not_awaited()

    async def test_memory_command(self, orch_with_mock_cli: tuple[Orchestrator, AsyncMock]) -> None:
        orch, mock_execute = orch_with_mock_cli

        result = await orch.handle_message(CHAT_ID, "/memory")

        assert "**Main Memory**" in result.text
        mock_execute.assert_not_awaited()

    async def test_stop_command_with_no_processes(
        self, orch_with_mock_cli: tuple[Orchestrator, AsyncMock]
    ) -> None:
        orch, mock_execute = orch_with_mock_cli

        result = await orch.handle_message(CHAT_ID, "/stop")

        assert "Nothing running" in result.text
        mock_execute.assert_not_awaited()

    async def test_model_command_no_args_returns_selector(
        self, orch_with_mock_cli: tuple[Orchestrator, AsyncMock]
    ) -> None:
        orch, mock_execute = orch_with_mock_cli

        with patch(
            "ductor_bot.orchestrator.commands.model_selector_start",
            new_callable=AsyncMock,
            return_value=("Select a provider:", MagicMock()),
        ):
            result = await orch.handle_message(CHAT_ID, "/model")

        assert "Select a provider" in result.text
        mock_execute.assert_not_awaited()

    async def test_cron_command_empty(
        self, orch_with_mock_cli: tuple[Orchestrator, AsyncMock]
    ) -> None:
        orch, mock_execute = orch_with_mock_cli

        result = await orch.handle_message(CHAT_ID, "/cron")

        assert "No cron jobs" in result.text
        mock_execute.assert_not_awaited()

    async def test_unknown_command_goes_to_normal_flow(
        self, orch_with_mock_cli: tuple[Orchestrator, AsyncMock]
    ) -> None:
        orch, mock_execute = orch_with_mock_cli

        result = await orch.handle_message(CHAT_ID, "/nonexistent")

        mock_execute.assert_awaited_once()
        assert result.text == "Hello from the agent!"


# ---------------------------------------------------------------------------
# Test 3: New session flow (/new)
# ---------------------------------------------------------------------------


class TestNewSessionFlow:
    async def test_new_command_resets_session(
        self, orch_with_mock_cli: tuple[Orchestrator, AsyncMock]
    ) -> None:
        orch, _mock_execute = orch_with_mock_cli

        await orch.handle_message(CHAT_ID, "Build something")

        session_before = await orch._sessions.get_active(CHAT_ID)
        assert session_before is not None
        assert session_before.session_id == "sess-abc-123"

        result = await orch.handle_message(CHAT_ID, "/new")

        assert "Fresh session" in result.text

        session_after = await orch._sessions.get_active(CHAT_ID)
        assert session_after is not None
        assert session_after.session_id == ""
        assert session_after.message_count == 0

    async def test_new_session_sends_no_resume(
        self, orch_with_mock_cli: tuple[Orchestrator, AsyncMock]
    ) -> None:
        orch, mock_execute = orch_with_mock_cli

        await orch.handle_message(CHAT_ID, "/new")

        mock_execute.reset_mock()
        mock_execute.return_value = _make_agent_response(result="Fresh start!")

        await orch.handle_message(CHAT_ID, "After reset")

        request = mock_execute.call_args[0][0]
        assert request.resume_session is None


# ---------------------------------------------------------------------------
# Test 4: Directive parsing (@model)
# ---------------------------------------------------------------------------


class TestDirectiveParsing:
    async def test_at_model_overrides_model(
        self, orch_with_mock_cli: tuple[Orchestrator, AsyncMock]
    ) -> None:
        orch, mock_execute = orch_with_mock_cli

        await orch.handle_message(CHAT_ID, "@sonnet explain this code")

        request = mock_execute.call_args[0][0]
        assert request.model_override == "sonnet"
        assert request.prompt == "explain this code"

    async def test_at_model_only_returns_hint(
        self, orch_with_mock_cli: tuple[Orchestrator, AsyncMock]
    ) -> None:
        orch, mock_execute = orch_with_mock_cli

        result = await orch.handle_message(CHAT_ID, "@opus")

        assert "Next message will use" in result.text
        assert "opus" in result.text
        mock_execute.assert_not_awaited()

    async def test_unknown_at_directive_passes_through(
        self, orch_with_mock_cli: tuple[Orchestrator, AsyncMock]
    ) -> None:
        orch, mock_execute = orch_with_mock_cli

        await orch.handle_message(CHAT_ID, "@unknown hello")

        request = mock_execute.call_args[0][0]
        assert request.model_override is None or request.model_override == "opus"

    async def test_at_directive_mid_text_not_parsed(
        self, orch_with_mock_cli: tuple[Orchestrator, AsyncMock]
    ) -> None:
        orch, mock_execute = orch_with_mock_cli

        await orch.handle_message(CHAT_ID, "Send email to @sonnet please")

        request = mock_execute.call_args[0][0]
        assert request.model_override is None or request.model_override == "opus"


# ---------------------------------------------------------------------------
# Test 5: Hook application
# ---------------------------------------------------------------------------


class TestHookApplication:
    async def test_mainmemory_hook_fires_on_6th_message(
        self, orch_with_mock_cli: tuple[Orchestrator, AsyncMock]
    ) -> None:
        orch, mock_execute = orch_with_mock_cli

        for i in range(6):
            mock_execute.return_value = _make_agent_response(result=f"Reply {i}")
            await orch.handle_message(CHAT_ID, f"Message {i}")

        sixth_call = mock_execute.call_args_list[5]
        prompt = sixth_call[0][0].prompt
        assert "MEMORY CHECK" in prompt

    async def test_hook_does_not_fire_before_threshold(
        self, orch_with_mock_cli: tuple[Orchestrator, AsyncMock]
    ) -> None:
        orch, mock_execute = orch_with_mock_cli

        for i in range(5):
            mock_execute.return_value = _make_agent_response(result=f"Reply {i}")
            await orch.handle_message(CHAT_ID, f"Message {i}")

        for call in mock_execute.call_args_list:
            prompt = call[0][0].prompt
            assert "MEMORY CHECK" not in prompt

    async def test_custom_hook_modifies_prompt(
        self, orch_with_mock_cli: tuple[Orchestrator, AsyncMock]
    ) -> None:
        orch, mock_execute = orch_with_mock_cli

        custom_hook = MessageHook(
            name="always_fire",
            condition=lambda _ctx: True,
            suffix="[CUSTOM SUFFIX]",
        )
        orch._hook_registry.register(custom_hook)

        await orch.handle_message(CHAT_ID, "Test message")

        prompt = mock_execute.call_args[0][0].prompt
        assert "[CUSTOM SUFFIX]" in prompt
        assert "Test message" in prompt


# ---------------------------------------------------------------------------
# Test 6: Error recovery (retry + reset)
# ---------------------------------------------------------------------------


class TestErrorRecovery:
    async def test_single_retry_on_cli_error(
        self, orch_with_mock_cli: tuple[Orchestrator, AsyncMock]
    ) -> None:
        orch, mock_execute = orch_with_mock_cli

        # Establish a session first so resume_session is set (retry only works on resume)
        first_resp = _make_agent_response(result="Setup", session_id="sess-retry")
        error_resp = _make_agent_response(result="CLI failed", is_error=True)
        success_resp = _make_agent_response(result="Recovered!")

        mock_execute.side_effect = [first_resp, error_resp, success_resp]

        await orch.handle_message(CHAT_ID, "Setup message")
        result = await orch.handle_message(CHAT_ID, "Flaky request")

        assert result.text == "Recovered!"
        assert mock_execute.await_count == 3  # 1 setup + 1 fail + 1 retry

    async def test_double_failure_resets_session(
        self, orch_with_mock_cli: tuple[Orchestrator, AsyncMock]
    ) -> None:
        orch, mock_execute = orch_with_mock_cli

        error_resp = _make_agent_response(result="", is_error=True)
        mock_execute.return_value = error_resp

        result = await orch.handle_message(CHAT_ID, "Broken request")

        assert "Session error" in result.text

        session = await orch._sessions.get_active(CHAT_ID)
        assert session is not None
        assert session.session_id == ""
        assert session.message_count == 0

    async def test_cli_exception_returns_error_message(
        self, orch_with_mock_cli: tuple[Orchestrator, AsyncMock]
    ) -> None:
        orch, mock_execute = orch_with_mock_cli
        mock_execute.side_effect = RuntimeError("subprocess exploded")

        result = await orch.handle_message(CHAT_ID, "Crash test")

        assert "internal error" in result.text.lower()


# ---------------------------------------------------------------------------
# Test 7: Streaming flow
# ---------------------------------------------------------------------------


class TestStreamingFlow:
    @staticmethod
    def _get_mock_streaming(orch: Orchestrator) -> AsyncMock:
        """Retrieve the AsyncMock that replaced execute_streaming."""
        mock = orch._cli_service.execute_streaming
        assert isinstance(mock, AsyncMock)
        return mock

    async def test_streaming_calls_text_delta_callback(
        self, orch_with_mock_cli: tuple[Orchestrator, AsyncMock]
    ) -> None:
        orch, _ = orch_with_mock_cli
        mock_streaming = self._get_mock_streaming(orch)

        mock_streaming.return_value = _make_agent_response(result="Streamed result")

        deltas: list[str] = []

        async def on_delta(text: str) -> None:
            deltas.append(text)

        result = await orch.handle_message_streaming(
            CHAT_ID,
            "Stream this",
            on_text_delta=on_delta,
        )

        mock_streaming.assert_awaited_once()
        call_kwargs = mock_streaming.call_args[1]
        assert call_kwargs["on_text_delta"] is not None
        assert result.text == "Streamed result"

    async def test_streaming_error_triggers_reset(
        self, orch_with_mock_cli: tuple[Orchestrator, AsyncMock]
    ) -> None:
        orch, _ = orch_with_mock_cli
        mock_streaming = self._get_mock_streaming(orch)

        mock_streaming.return_value = _make_agent_response(result="", is_error=True)

        result = await orch.handle_message_streaming(CHAT_ID, "Broken stream")

        assert "Session error" in result.text

    async def test_streaming_passes_tool_activity_callback(
        self, orch_with_mock_cli: tuple[Orchestrator, AsyncMock]
    ) -> None:
        orch, _ = orch_with_mock_cli
        mock_streaming = self._get_mock_streaming(orch)
        mock_streaming.return_value = _make_agent_response(result="Tool result")

        tools: list[str] = []

        async def on_tool(name: str) -> None:
            tools.append(name)

        await orch.handle_message_streaming(
            CHAT_ID,
            "Use a tool",
            on_tool_activity=on_tool,
        )

        call_kwargs = mock_streaming.call_args[1]
        assert call_kwargs["on_tool_activity"] is not None


# ---------------------------------------------------------------------------
# Test 8: Session persistence (real SessionManager with tmp directory)
# ---------------------------------------------------------------------------


class TestSessionPersistence:
    async def test_sessions_survive_manager_recreation(
        self, workspace: tuple[DuctorPaths, AgentConfig]
    ) -> None:
        paths, config = workspace

        mgr1 = SessionManager(paths.sessions_path, config)
        session, is_new = await mgr1.resolve_session(CHAT_ID, provider="claude")
        assert is_new
        session.session_id = "persistent-sess"
        await mgr1.update_session(session, cost_usd=0.1, tokens=200)

        mgr2 = SessionManager(paths.sessions_path, config)
        session2, is_new2 = await mgr2.resolve_session(CHAT_ID, provider="claude")

        assert not is_new2
        assert session2.session_id == "persistent-sess"
        assert session2.message_count == 1
        assert session2.total_cost_usd == pytest.approx(0.1)

    async def test_session_json_is_valid(self, workspace: tuple[DuctorPaths, AgentConfig]) -> None:
        paths, config = workspace

        mgr = SessionManager(paths.sessions_path, config)
        await mgr.resolve_session(CHAT_ID, provider="claude")

        raw = paths.sessions_path.read_text(encoding="utf-8")
        data = json.loads(raw)
        assert str(CHAT_ID) in data


# ---------------------------------------------------------------------------
# Test 9: Full round-trip (message -> session -> hooks -> CLI -> response)
# ---------------------------------------------------------------------------


class TestFullRoundTrip:
    async def test_end_to_end_message_and_session(
        self, orch_with_mock_cli: tuple[Orchestrator, AsyncMock]
    ) -> None:
        orch, mock_execute = orch_with_mock_cli

        resp1 = _make_agent_response(result="First reply", session_id="sess-001", cost=0.02)
        resp2 = _make_agent_response(result="Second reply", session_id="sess-001", cost=0.03)
        mock_execute.side_effect = [resp1, resp2]

        r1 = await orch.handle_message(CHAT_ID, "Hello agent")
        assert r1.text == "First reply"

        r2 = await orch.handle_message(CHAT_ID, "Follow up")
        assert r2.text == "Second reply"

        session = await orch._sessions.get_active(CHAT_ID)
        assert session is not None
        assert session.message_count == 2
        assert session.total_cost_usd == pytest.approx(0.05)
        assert session.session_id == "sess-001"

        req2 = mock_execute.call_args_list[1][0][0]
        assert req2.resume_session == "sess-001"

    async def test_new_session_injects_mainmemory(
        self, orch_with_mock_cli: tuple[Orchestrator, AsyncMock]
    ) -> None:
        orch, mock_execute = orch_with_mock_cli

        memory_text = "User likes Python and espresso."
        orch.paths.mainmemory_path.write_text(memory_text, encoding="utf-8")

        await orch.handle_message(CHAT_ID, "First message")

        request = mock_execute.call_args[0][0]
        assert request.append_system_prompt == memory_text

    async def test_resumed_session_skips_mainmemory_injection(
        self, orch_with_mock_cli: tuple[Orchestrator, AsyncMock]
    ) -> None:
        orch, mock_execute = orch_with_mock_cli

        orch.paths.mainmemory_path.write_text("Some memory", encoding="utf-8")

        await orch.handle_message(CHAT_ID, "First")

        mock_execute.reset_mock()
        mock_execute.return_value = _make_agent_response(result="Second")

        await orch.handle_message(CHAT_ID, "Second")

        request = mock_execute.call_args[0][0]
        assert request.append_system_prompt is None
