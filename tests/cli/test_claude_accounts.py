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


# -- one-shot path (cron / webhook / heartbeat / background) --------------------


def test_oneshot_claude_cmd_sets_account_dir() -> None:
    """These runs bypass CLIService entirely and would otherwise ignore /account."""
    from ductor_bot.cli.param_resolver import CLIRunConfig
    from ductor_bot.cron.execution import _build_claude_cmd

    cfg = CLIRunConfig(
        provider="claude",
        model="sonnet",
        reasoning_effort="medium",
        cli_parameters=[],
        permission_mode="bypassPermissions",
        working_dir="/w",
        file_access="all",
        claude_account_dir="/opt/creds/work",
    )
    with patch("ductor_bot.cron.execution.which", return_value="/usr/bin/claude"):
        one_shot = _build_claude_cmd(cfg, "hi")

    assert one_shot is not None
    assert one_shot.env_overrides[ENV_VAR] == "/opt/creds/work"
    assert ENV_VAR not in one_shot.env_unset


def test_oneshot_claude_cmd_unsets_for_default_account() -> None:
    """Setting the variable to empty would mean ~/.claude, not 'the default'."""
    from ductor_bot.cli.param_resolver import CLIRunConfig
    from ductor_bot.cron.execution import _build_claude_cmd

    cfg = CLIRunConfig(
        provider="claude",
        model="sonnet",
        reasoning_effort="medium",
        cli_parameters=[],
        permission_mode="bypassPermissions",
        working_dir="/w",
        file_access="all",
    )
    with patch("ductor_bot.cron.execution.which", return_value="/usr/bin/claude"):
        one_shot = _build_claude_cmd(cfg, "hi")

    assert one_shot is not None
    assert ENV_VAR in one_shot.env_unset
    assert ENV_VAR not in one_shot.env_overrides


def test_apply_to_env_clears_rather_than_blanks() -> None:
    from ductor_bot.cli.claude_accounts import apply_to_env

    assert apply_to_env({ENV_VAR: "/old"}, None) == {}
    assert apply_to_env({}, "/new")[ENV_VAR] == "/new"


def test_usable_accounts_drops_blank_paths() -> None:
    from ductor_bot.cli.claude_accounts import usable_accounts

    assert usable_accounts({"a": "/p", "b": "  ", "c": ""}) == {"a": "/p"}


# -- auth detection scoped to the selected account -----------------------------


def test_auth_reads_the_selected_store(tmp_path: Path) -> None:
    """Without this, a setup whose only logged-in store is the non-default one
    reports Claude as unauthenticated and drops it from available providers."""
    from ductor_bot.cli.auth import AuthStatus, check_claude_auth

    alt = tmp_path / "auth2"
    alt.mkdir()
    (alt / ".credentials.json").write_text("{}")

    result = check_claude_auth(str(alt))
    assert result.status is AuthStatus.AUTHENTICATED
    assert result.auth_file == alt / ".credentials.json"


def test_auth_cli_fallback_runs_with_the_account_env() -> None:
    """The `claude auth status` probe must see the same store the agent will use."""
    from ductor_bot.cli import auth as auth_mod

    captured: dict[str, str] = {}

    class _Proc:
        stdout = '{"loggedIn": false}'

    def _fake_run(_cmd, **kwargs):  # type: ignore[no-untyped-def]
        captured.update(kwargs.get("env") or {})
        return _Proc()

    with patch.object(auth_mod.subprocess, "run", _fake_run):
        auth_mod._claude_cli_logged_in("/opt/creds/work")
    assert captured.get(ENV_VAR) == "/opt/creds/work"

    captured.clear()
    with (
        patch.object(auth_mod.subprocess, "run", _fake_run),
        patch.dict(os.environ, {ENV_VAR: "/leaked"}),
    ):
        auth_mod._claude_cli_logged_in(None)
    assert ENV_VAR not in captured


def test_active_claude_account_dir_helper() -> None:
    from types import SimpleNamespace

    from ductor_bot.cli.claude_accounts import active_claude_account_dir

    cfg = SimpleNamespace(claude_accounts={"work": "/opt/creds/work"}, claude_account="work")
    assert active_claude_account_dir(cfg) == "/opt/creds/work"
    assert active_claude_account_dir(SimpleNamespace()) == ""
