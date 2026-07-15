"""Extended CLIService tests -- covering _make_cli provider resolution."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from ductor_bot.cli.base import CLIConfig
from ductor_bot.cli.process_registry import ProcessRegistry
from ductor_bot.cli.service import CLIService, CLIServiceConfig
from ductor_bot.cli.types import AgentRequest, Origin
from ductor_bot.config import ModelRegistry


def _make_service(tmp_path: Path, **overrides: Any) -> CLIService:
    config = CLIServiceConfig(
        working_dir=str(tmp_path),
        default_model=overrides.pop("default_model", "opus"),
        provider=overrides.pop("provider", "claude"),
        max_turns=overrides.pop("max_turns", None),
        max_budget_usd=overrides.pop("max_budget_usd", None),
        permission_mode=overrides.pop("permission_mode", "bypassPermissions"),
        gemini_api_key=overrides.pop("gemini_api_key", None),
    )
    return CLIService(
        config=config,
        models=ModelRegistry(),
        available_providers=overrides.pop("available_providers", frozenset({"claude"})),
        process_registry=ProcessRegistry(),
    )


def test_make_cli_default_provider(tmp_path: Path) -> None:
    svc = _make_service(tmp_path)
    with patch("ductor_bot.cli.service.create_cli") as mock_create:
        mock_create.return_value = MagicMock()
        svc._make_cli(AgentRequest(origin=Origin.HUMAN_CHAT, prompt="test", chat_id=1))

    call_args = mock_create.call_args[0][0]
    assert isinstance(call_args, CLIConfig)
    assert call_args.provider == "claude"
    assert call_args.model == "opus"


def test_make_cli_with_model_override(tmp_path: Path) -> None:
    svc = _make_service(tmp_path)
    with patch("ductor_bot.cli.service.create_cli") as mock_create:
        mock_create.return_value = MagicMock()
        svc._make_cli(
            AgentRequest(
                origin=Origin.HUMAN_CHAT, prompt="test", model_override="sonnet", chat_id=1
            )
        )

    call_args = mock_create.call_args[0][0]
    assert call_args.model == "sonnet"
    assert call_args.provider == "claude"


def test_make_cli_with_provider_override(tmp_path: Path) -> None:
    svc = _make_service(tmp_path)
    with patch("ductor_bot.cli.service.create_cli") as mock_create:
        mock_create.return_value = MagicMock()
        svc._make_cli(
            AgentRequest(
                origin=Origin.HUMAN_CHAT, prompt="test", provider_override="codex", chat_id=1
            )
        )

    call_args = mock_create.call_args[0][0]
    assert call_args.provider == "codex"


def test_make_cli_does_not_auto_fallback_provider(tmp_path: Path) -> None:
    """Native model/provider mapping should be preserved even if unavailable."""
    svc = _make_service(tmp_path, available_providers=frozenset({"codex"}))
    with patch("ductor_bot.cli.service.create_cli") as mock_create:
        mock_create.return_value = MagicMock()
        svc._make_cli(AgentRequest(origin=Origin.HUMAN_CHAT, prompt="test", chat_id=1))

    call_args = mock_create.call_args[0][0]
    assert call_args.provider == "claude"
    assert call_args.model == "opus"


def test_make_cli_passes_system_prompts(tmp_path: Path) -> None:
    svc = _make_service(tmp_path)
    with patch("ductor_bot.cli.service.create_cli") as mock_create:
        mock_create.return_value = MagicMock()
        svc._make_cli(
            AgentRequest(
                prompt="test",
                origin=Origin.HUMAN_CHAT,
                system_prompt="Be helpful",
                append_system_prompt="Follow rules",
                chat_id=1,
            )
        )

    call_args = mock_create.call_args[0][0]
    assert call_args.system_prompt == "Be helpful"
    assert call_args.append_system_prompt == "Follow rules"


def test_make_cli_passes_process_label(tmp_path: Path) -> None:
    svc = _make_service(tmp_path)
    with patch("ductor_bot.cli.service.create_cli") as mock_create:
        mock_create.return_value = MagicMock()
        svc._make_cli(
            AgentRequest(
                origin=Origin.HUMAN_CHAT, prompt="test", chat_id=42, process_label="worker"
            )
        )

    call_args = mock_create.call_args[0][0]
    assert call_args.chat_id == 42
    assert call_args.process_label == "worker"


def test_make_cli_passes_transport(tmp_path: Path) -> None:
    svc = _make_service(tmp_path)
    with patch("ductor_bot.cli.service.create_cli") as mock_create:
        mock_create.return_value = MagicMock()
        svc._make_cli(
            AgentRequest(origin=Origin.HUMAN_CHAT, prompt="test", chat_id=42, transport="mx")
        )

    call_args = mock_create.call_args[0][0]
    assert call_args.transport == "mx"


def test_make_cli_passes_gemini_api_key(tmp_path: Path) -> None:
    svc = _make_service(tmp_path, gemini_api_key="cfg-key-123")
    with patch("ductor_bot.cli.service.create_cli") as mock_create:
        mock_create.return_value = MagicMock()
        svc._make_cli(
            AgentRequest(
                origin=Origin.HUMAN_CHAT, prompt="test", provider_override="gemini", chat_id=1
            )
        )

    call_args = mock_create.call_args[0][0]
    assert call_args.provider == "gemini"
    assert call_args.gemini_api_key == "cfg-key-123"
