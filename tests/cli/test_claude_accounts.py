"""Tests for Claude credential-store account resolution and env injection."""

from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock, patch

from ductor_bot.cli.base import CLIConfig
from ductor_bot.cli.claude_accounts import (
    ENV_VAR,
    account_names,
    is_known_account,
    resolve_account_dir,
)
from ductor_bot.cli.executor import build_subprocess_env
from ductor_bot.cli.service import CLIService, CLIServiceConfig
from ductor_bot.cli.types import AgentRequest

ACCOUNTS = {"work": "~/.claude-work", "personal": "/opt/creds/personal"}


# -- resolve_account_dir -------------------------------------------------------


def test_resolve_returns_none_for_default_account() -> None:
    assert resolve_account_dir(ACCOUNTS, "") is None


def test_resolve_returns_none_for_unknown_account() -> None:
    assert resolve_account_dir(ACCOUNTS, "nope") is None


def test_resolve_returns_none_for_blank_path() -> None:
    assert resolve_account_dir({"broken": "   "}, "broken") is None


def test_resolve_expands_user() -> None:
    resolved = resolve_account_dir(ACCOUNTS, "work")
    assert resolved == str(Path("~/.claude-work").expanduser())
    assert "~" not in resolved


def test_resolve_passes_absolute_path_through() -> None:
    assert resolve_account_dir(ACCOUNTS, "personal") == "/opt/creds/personal"


def test_account_names_sorted() -> None:
    assert account_names(ACCOUNTS) == ["personal", "work"]


def test_is_known_account() -> None:
    assert is_known_account(ACCOUNTS, "")  # default
    assert is_known_account(ACCOUNTS, "work")
    assert not is_known_account(ACCOUNTS, "nope")


# -- build_subprocess_env ------------------------------------------------------


def test_env_sets_account_dir(tmp_path: Path) -> None:
    config = CLIConfig(working_dir=str(tmp_path), claude_account_dir="/opt/creds/personal")
    env = build_subprocess_env(config)
    assert env is not None
    assert env[ENV_VAR] == "/opt/creds/personal"


def test_env_drops_inherited_var_for_default_account(tmp_path: Path) -> None:
    """The default account must UNSET the variable, not blank it.

    Claude Code reads an empty value as ``~/.claude``, which would silently
    ignore a custom ``CLAUDE_CONFIG_DIR`` instead of using the default store.
    """
    config = CLIConfig(working_dir=str(tmp_path), claude_account_dir="")
    with patch.dict(os.environ, {ENV_VAR: "/leaked/from/parent"}):
        env = build_subprocess_env(config)
    assert env is not None
    assert ENV_VAR not in env


# -- end-to-end plumbing -------------------------------------------------------


def test_service_passes_account_dir_into_cli_config() -> None:
    """The service config must reach CLIConfig at the single _make_cli choke point."""
    service = CLIService(
        config=CLIServiceConfig(
            working_dir="/workspace",
            default_model="sonnet",
            provider="claude",
            max_turns=None,
            max_budget_usd=None,
            permission_mode="bypassPermissions",
            claude_account_dir="/opt/creds/work",
        ),
        models=MagicMock(),
        available_providers=frozenset({"claude"}),
        process_registry=MagicMock(),
    )
    service.resolve_provider = MagicMock(return_value=("claude", "sonnet"))  # type: ignore[method-assign]

    with patch("ductor_bot.cli.service.create_cli") as create:
        service._make_cli(AgentRequest(prompt="hi"))

    assert create.call_args.args[0].claude_account_dir == "/opt/creds/work"


def test_service_updates_account_dir_at_runtime() -> None:
    """/account switching must take effect without rebuilding the service."""
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
    service.update_claude_account_dir("/opt/creds/personal")
    assert service._config.claude_account_dir == "/opt/creds/personal"
