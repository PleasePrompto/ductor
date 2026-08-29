"""The chosen persona must reach the command line.

Describing a persona in the prompt is not the same as loading it: only
``--agent`` makes Claude Code read the agent definition.
"""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from ductor_bot.cli.base import CLIConfig
from ductor_bot.cli.claude_provider import ClaudeCodeCLI
from ductor_bot.cli.service import CLIService, CLIServiceConfig
from ductor_bot.cli.types import AgentRequest


def _cmd(persona: str) -> list[str]:
    with patch.object(ClaudeCodeCLI, "_find_cli", staticmethod(lambda: "/usr/bin/claude")):
        cli = ClaudeCodeCLI(CLIConfig(provider="claude", model="sonnet", persona=persona))
        return cli._build_command("hello")


def test_persona_becomes_an_agent_flag() -> None:
    cmd = _cmd("coder")
    assert "--agent" in cmd
    assert cmd[cmd.index("--agent") + 1] == "coder"


def test_no_persona_omits_the_flag() -> None:
    """An empty --agent would be worse than none: it means something different."""
    assert "--agent" not in _cmd("")


def _service(resolver) -> CLIService:
    service = CLIService(
        config=CLIServiceConfig(
            working_dir="/workspace",
            default_model="sonnet",
            provider="claude",
            max_turns=None,
            max_budget_usd=None,
            permission_mode="bypassPermissions",
        ),
        models=MagicMock(),
        available_providers=frozenset({"claude"}),
        process_registry=MagicMock(),
    )
    service.resolve_provider = MagicMock(return_value=("claude", "sonnet"))  # type: ignore[method-assign]
    service.set_persona_resolver(resolver)
    return service


def test_resolver_reaches_the_cli_config() -> None:
    service = _service(lambda _req: "scout")
    with patch("ductor_bot.cli.service.create_cli") as create:
        service._make_cli(AgentRequest(prompt="hi"))
    assert create.call_args.args[0].persona == "scout"


def test_absent_resolver_means_no_persona() -> None:
    service = _service(None)
    with patch("ductor_bot.cli.service.create_cli") as create:
        service._make_cli(AgentRequest(prompt="hi"))
    assert create.call_args.args[0].persona == ""


def test_resolver_sees_the_request_so_choice_is_per_conversation() -> None:
    seen: list[tuple[int, int | None]] = []

    def resolver(req: AgentRequest) -> str:
        seen.append((req.chat_id, req.topic_id))
        return "coder" if req.topic_id == 7 else ""

    service = _service(resolver)
    with patch("ductor_bot.cli.service.create_cli") as create:
        service._make_cli(AgentRequest(prompt="hi", chat_id=1, topic_id=7))
        assert create.call_args.args[0].persona == "coder"
        service._make_cli(AgentRequest(prompt="hi", chat_id=1, topic_id=8))
        assert create.call_args.args[0].persona == ""
    assert seen == [(1, 7), (1, 8)]
