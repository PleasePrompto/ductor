"""Extended CLIService tests -- covering _make_cli provider resolution."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

from ductor_bot.cli.base import CLIConfig
from ductor_bot.cli.process_registry import ProcessRegistry
from ductor_bot.cli.service import CLIService, CLIServiceConfig
from ductor_bot.cli.types import AgentRequest
from ductor_bot.config import ModelRegistry


def _make_service(tmp_path: Path, **overrides: Any) -> CLIService:
    config = CLIServiceConfig(
        working_dir=str(tmp_path),
        default_model=overrides.pop("default_model", "opus"),
        provider=overrides.pop("provider", "claude"),
        max_turns=overrides.pop("max_turns", None),
        max_budget_usd=overrides.pop("max_budget_usd", None),
        permission_mode=overrides.pop("permission_mode", "bypassPermissions"),
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
        svc._make_cli(AgentRequest(prompt="test", chat_id=1))

    call_args = mock_create.call_args[0][0]
    assert isinstance(call_args, CLIConfig)
    assert call_args.provider == "claude"
    assert call_args.model == "opus"


def test_make_cli_with_model_override(tmp_path: Path) -> None:
    svc = _make_service(tmp_path)
    with patch("ductor_bot.cli.service.create_cli") as mock_create:
        mock_create.return_value = MagicMock()
        svc._make_cli(AgentRequest(prompt="test", model_override="sonnet", chat_id=1))

    call_args = mock_create.call_args[0][0]
    assert call_args.model == "sonnet"
    assert call_args.provider == "claude"


def test_make_cli_with_provider_override(tmp_path: Path) -> None:
    svc = _make_service(tmp_path)
    with patch("ductor_bot.cli.service.create_cli") as mock_create:
        mock_create.return_value = MagicMock()
        svc._make_cli(AgentRequest(prompt="test", provider_override="codex", chat_id=1))

    call_args = mock_create.call_args[0][0]
    assert call_args.provider == "codex"


def test_make_cli_cross_provider_fallback(tmp_path: Path) -> None:
    """When native provider is not available, fall back via equivalence map."""
    svc = _make_service(tmp_path, available_providers=frozenset({"codex"}))
    with patch("ductor_bot.cli.service.create_cli") as mock_create:
        mock_create.return_value = MagicMock()
        svc._make_cli(AgentRequest(prompt="test", chat_id=1))

    call_args = mock_create.call_args[0][0]
    assert call_args.provider == "codex"
    assert call_args.model == "gpt-5.2-codex"


def test_make_cli_passes_system_prompts(tmp_path: Path) -> None:
    svc = _make_service(tmp_path)
    with patch("ductor_bot.cli.service.create_cli") as mock_create:
        mock_create.return_value = MagicMock()
        svc._make_cli(
            AgentRequest(
                prompt="test",
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
        svc._make_cli(AgentRequest(prompt="test", chat_id=42, process_label="worker"))

    call_args = mock_create.call_args[0][0]
    assert call_args.chat_id == 42
    assert call_args.process_label == "worker"
