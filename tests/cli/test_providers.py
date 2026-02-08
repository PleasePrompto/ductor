"""Tests for CLI provider command building and response parsing."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from ductor_bot.cli.base import CLIConfig, docker_wrap
from ductor_bot.cli.claude_provider import ClaudeCodeCLI
from ductor_bot.cli.codex_provider import CodexCLI

if TYPE_CHECKING:
    import pytest

# -- docker_wrap --


def test_docker_wrap_without_container(tmp_path: Path) -> None:
    cmd = ["claude", "-p", "hello"]
    result_cmd, cwd = docker_wrap(cmd, "", 0, tmp_path)
    assert result_cmd == cmd
    assert cwd == str(tmp_path)


def test_docker_wrap_with_container(tmp_path: Path) -> None:
    cmd = ["claude", "-p", "hello"]
    result_cmd, cwd = docker_wrap(cmd, "my-container", 42, tmp_path)
    assert result_cmd[0] == "docker"
    assert "my-container" in result_cmd
    assert "DUCTOR_CHAT_ID=42" in result_cmd[3]
    assert cwd is None


# -- ClaudeCodeCLI command building --


def test_claude_build_command_basic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ductor_bot.cli.claude_provider.which", lambda _: "/usr/bin/claude")
    cfg = CLIConfig(provider="claude", model="opus", permission_mode="bypassPermissions")
    cli = ClaudeCodeCLI(cfg)
    cmd = cli._build_command("hello")
    assert cmd[0] == "/usr/bin/claude"
    assert "-p" in cmd
    assert "--output-format" in cmd
    assert "json" in cmd
    assert "--permission-mode" in cmd
    assert "bypassPermissions" in cmd
    assert "--model" in cmd
    assert "opus" in cmd
    assert cmd[-1] == "hello"


def test_claude_build_command_with_resume(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ductor_bot.cli.claude_provider.which", lambda _: "/usr/bin/claude")
    cfg = CLIConfig(provider="claude", model="opus")
    cli = ClaudeCodeCLI(cfg)
    cmd = cli._build_command("hello", resume_session="session-123")
    assert "--resume" in cmd
    assert "session-123" in cmd


def test_claude_build_command_with_continue(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ductor_bot.cli.claude_provider.which", lambda _: "/usr/bin/claude")
    cfg = CLIConfig(provider="claude", model="opus")
    cli = ClaudeCodeCLI(cfg)
    cmd = cli._build_command("hello", continue_session=True)
    assert "--continue" in cmd


def test_claude_build_command_with_system_prompt(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ductor_bot.cli.claude_provider.which", lambda _: "/usr/bin/claude")
    cfg = CLIConfig(provider="claude", model="opus", system_prompt="Be helpful")
    cli = ClaudeCodeCLI(cfg)
    cmd = cli._build_command("hello")
    assert "--system-prompt" in cmd
    idx = cmd.index("--system-prompt")
    assert cmd[idx + 1] == "Be helpful"


def test_claude_build_command_with_append_system_prompt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("ductor_bot.cli.claude_provider.which", lambda _: "/usr/bin/claude")
    cfg = CLIConfig(provider="claude", model="opus", append_system_prompt="Extra rules")
    cli = ClaudeCodeCLI(cfg)
    cmd = cli._build_command("hello")
    assert "--append-system-prompt" in cmd


def test_claude_build_command_with_max_turns(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ductor_bot.cli.claude_provider.which", lambda _: "/usr/bin/claude")
    cfg = CLIConfig(provider="claude", model="opus", max_turns=5)
    cli = ClaudeCodeCLI(cfg)
    cmd = cli._build_command("hello")
    assert "--max-turns" in cmd
    idx = cmd.index("--max-turns")
    assert cmd[idx + 1] == "5"


def test_claude_build_command_with_allowed_tools(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ductor_bot.cli.claude_provider.which", lambda _: "/usr/bin/claude")
    cfg = CLIConfig(provider="claude", model="opus", allowed_tools=["Read", "Write"])
    cli = ClaudeCodeCLI(cfg)
    cmd = cli._build_command("hello")
    assert "--allowedTools" in cmd
    idx = cmd.index("--allowedTools")
    assert cmd[idx + 1] == "Read"
    assert cmd[idx + 2] == "Write"


def test_claude_streaming_command_uses_stream_json(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ductor_bot.cli.claude_provider.which", lambda _: "/usr/bin/claude")
    cfg = CLIConfig(provider="claude", model="opus")
    cli = ClaudeCodeCLI(cfg)
    cmd = cli._build_command_streaming("hello")
    assert "stream-json" in cmd
    assert "json" not in cmd
    assert "--verbose" in cmd


# -- CodexCLI command building --


def test_codex_build_command_basic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ductor_bot.cli.codex_provider.which", lambda _: "/usr/bin/codex")
    cfg = CLIConfig(provider="codex", model="gpt-5.2-codex", permission_mode="bypassPermissions")
    cli = CodexCLI(cfg)
    cmd = cli._build_command("hello")
    assert cmd[0] == "/usr/bin/codex"
    assert "exec" in cmd
    assert "--json" in cmd
    assert "--model" in cmd
    assert "gpt-5.2-codex" in cmd
    assert "--dangerously-bypass-approvals-and-sandbox" in cmd


def test_codex_compose_prompt_injects_system_context(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ductor_bot.cli.codex_provider.which", lambda _: "/usr/bin/codex")
    cfg = CLIConfig(
        provider="codex",
        system_prompt="System",
        append_system_prompt="Append",
    )
    cli = CodexCLI(cfg)
    composed = cli._compose_prompt("User message")
    assert "System" in composed
    assert "User message" in composed
    assert "Append" in composed
    # System context comes first
    assert composed.index("System") < composed.index("User message")


def test_codex_sandbox_flags_bypass(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ductor_bot.cli.codex_provider.which", lambda _: "/usr/bin/codex")
    cfg = CLIConfig(provider="codex", permission_mode="bypassPermissions")
    cli = CodexCLI(cfg)
    flags = cli._sandbox_flags()
    assert "--dangerously-bypass-approvals-and-sandbox" in flags


def test_codex_sandbox_flags_full_access(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ductor_bot.cli.codex_provider.which", lambda _: "/usr/bin/codex")
    cfg = CLIConfig(provider="codex", permission_mode="other", sandbox_mode="full-access")
    cli = CodexCLI(cfg)
    flags = cli._sandbox_flags()
    assert flags == ["--sandbox", "danger-full-access"]


def test_codex_sandbox_flags_workspace_write(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ductor_bot.cli.codex_provider.which", lambda _: "/usr/bin/codex")
    cfg = CLIConfig(provider="codex", permission_mode="other", sandbox_mode="workspace-write")
    cli = CodexCLI(cfg)
    flags = cli._sandbox_flags()
    assert "--full-auto" in flags


def test_codex_build_command_with_resume(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ductor_bot.cli.codex_provider.which", lambda _: "/usr/bin/codex")
    cfg = CLIConfig(provider="codex", model="gpt-5.2-codex")
    cli = CodexCLI(cfg)
    cmd = cli._build_command("hello", resume_session="thread-abc")
    assert "resume" in cmd
    assert "thread-abc" in cmd


def test_codex_build_command_with_reasoning_effort(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ductor_bot.cli.codex_provider.which", lambda _: "/usr/bin/codex")
    cfg = CLIConfig(provider="codex", model="gpt-5.2-codex", reasoning_effort="high")
    cli = CodexCLI(cfg)
    cmd = cli._build_command("hello")
    assert "-c" in cmd


def test_codex_build_command_with_images(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("ductor_bot.cli.codex_provider.which", lambda _: "/usr/bin/codex")
    cfg = CLIConfig(provider="codex", model="gpt-5.2-codex", images=["img.png"])
    cli = CodexCLI(cfg)
    cmd = cli._build_command("hello")
    assert "--image" in cmd
    assert "img.png" in cmd
