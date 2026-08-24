"""Tests for the /account Claude credential-store selector."""

from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from ductor_bot.orchestrator.selectors.account_selector import (
    ACC_PREFIX,
    account_selector_start,
    handle_account_callback,
    is_account_selector_callback,
    switch_account,
)

_ACCOUNTS = {"work": "/opt/creds/work", "personal": "/opt/creds/personal"}


def _orch(
    tmp_path: Path,
    *,
    accounts: dict[str, str] | None = None,
    active: str = "",
    docker: bool = False,
) -> Any:
    orch = MagicMock()
    orch._config.claude_accounts = _ACCOUNTS if accounts is None else accounts
    orch._config.claude_account = active
    orch._config.docker.enabled = docker
    orch.paths.config_path = tmp_path / "config.json"
    orch._cli_service = MagicMock()
    return orch


def _button_data(resp: Any) -> list[str]:
    assert resp.buttons is not None
    return [b.callback_data for row in resp.buttons.rows for b in row]


# -- callback matching ---------------------------------------------------------


def test_is_account_selector_callback() -> None:
    assert is_account_selector_callback(f"{ACC_PREFIX}work")
    assert not is_account_selector_callback("ms:p:claude")


# -- selector rendering --------------------------------------------------------


def test_selector_lists_default_plus_configured(tmp_path: Path) -> None:
    resp = account_selector_start(_orch(tmp_path))
    # "-" stands in for the default store: Telegram rejects empty callback data.
    assert _button_data(resp) == [f"{ACC_PREFIX}-", f"{ACC_PREFIX}personal", f"{ACC_PREFIX}work"]


def test_selector_marks_active_account(tmp_path: Path) -> None:
    resp = account_selector_start(_orch(tmp_path, active="work"))
    marked = [b.text for row in resp.buttons.rows for b in row if b.text.startswith("✅")]
    assert marked == ["✅ work"]


def test_selector_without_accounts_has_no_buttons(tmp_path: Path) -> None:
    resp = account_selector_start(_orch(tmp_path, accounts={}))
    assert resp.buttons is None
    assert "claude_accounts" in resp.text


# -- switching -----------------------------------------------------------------


@pytest.mark.asyncio
async def test_switch_applies_and_persists(tmp_path: Path) -> None:
    orch = _orch(tmp_path)
    with patch(
        "ductor_bot.orchestrator.selectors.account_selector.update_config_file_async",
        new=AsyncMock(),
    ) as save:
        text = await switch_account(orch, "work")

    assert orch._config.claude_account == "work"
    orch._cli_service.update_claude_account_dir.assert_called_once_with("/opt/creds/work")
    save.assert_awaited_once_with(orch.paths.config_path, claude_account="work")
    assert "work" in text


@pytest.mark.asyncio
async def test_switch_to_default_clears_account_dir(tmp_path: Path) -> None:
    orch = _orch(tmp_path, active="work")
    with patch(
        "ductor_bot.orchestrator.selectors.account_selector.update_config_file_async",
        new=AsyncMock(),
    ):
        await switch_account(orch, "")

    assert orch._config.claude_account == ""
    orch._cli_service.update_claude_account_dir.assert_called_once_with("")


@pytest.mark.asyncio
async def test_switch_rejects_unknown_account(tmp_path: Path) -> None:
    orch = _orch(tmp_path)
    with patch(
        "ductor_bot.orchestrator.selectors.account_selector.update_config_file_async",
        new=AsyncMock(),
    ) as save:
        text = await switch_account(orch, "nope")

    assert "nope" in text
    assert orch._config.claude_account == ""
    orch._cli_service.update_claude_account_dir.assert_not_called()
    save.assert_not_awaited()


@pytest.mark.asyncio
async def test_switch_warns_in_docker_mode(tmp_path: Path) -> None:
    orch = _orch(tmp_path, docker=True)
    with patch(
        "ductor_bot.orchestrator.selectors.account_selector.update_config_file_async",
        new=AsyncMock(),
    ):
        text = await switch_account(orch, "work")

    assert "Docker" in text


@pytest.mark.asyncio
async def test_callback_switches_and_redraws(tmp_path: Path) -> None:
    orch = _orch(tmp_path)

    async def _save(_path: Path, **updates: object) -> None:
        orch._config.claude_account = str(updates["claude_account"])

    with patch(
        "ductor_bot.orchestrator.selectors.account_selector.update_config_file_async",
        new=AsyncMock(side_effect=_save),
    ):
        resp = await handle_account_callback(orch, f"{ACC_PREFIX}personal")

    assert orch._config.claude_account == "personal"
    marked = [b.text for row in resp.buttons.rows for b in row if b.text.startswith("✅")]
    assert marked == ["✅ personal"]


@pytest.mark.asyncio
async def test_callback_default_token_selects_default(tmp_path: Path) -> None:
    orch = _orch(tmp_path, active="work")
    with patch(
        "ductor_bot.orchestrator.selectors.account_selector.update_config_file_async",
        new=AsyncMock(),
    ):
        await handle_account_callback(orch, f"{ACC_PREFIX}-")

    assert orch._config.claude_account == ""
