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
    # Accounts are addressed by index; -1 is the default store. Names never
    # enter callback_data, which Telegram caps at 64 bytes.
    assert _button_data(resp) == [f"{ACC_PREFIX}-1", f"{ACC_PREFIX}0", f"{ACC_PREFIX}1"]


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
        resp = await handle_account_callback(orch, f"{ACC_PREFIX}0")  # personal

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
        await handle_account_callback(orch, f"{ACC_PREFIX}-1")

    assert orch._config.claude_account == ""


# -- review hardening ----------------------------------------------------------


def test_callback_data_stays_within_telegram_limit(tmp_path: Path) -> None:
    """Telegram rejects callback_data over 64 UTF-8 bytes."""
    long_name = "a" * 200
    resp = account_selector_start(_orch(tmp_path, accounts={long_name: "/opt/creds/x"}))
    for data in _button_data(resp):
        assert len(data.encode()) <= 64


def test_account_literally_named_dash_is_selectable(tmp_path: Path) -> None:
    """A reserved string marker would make this name unreachable; indexes do not."""
    orch = _orch(tmp_path, accounts={"-": "/opt/creds/dash"})
    resp = account_selector_start(orch)
    with patch(
        "ductor_bot.orchestrator.selectors.account_selector.update_config_file_async",
        new=AsyncMock(),
    ):
        import asyncio

        asyncio.run(handle_account_callback(orch, _button_data(resp)[1]))
    assert orch._config.claude_account == "-"


def test_blank_path_accounts_are_not_offered(tmp_path: Path) -> None:
    """A name mapped to whitespace resolves to the default store; hiding it keeps
    the displayed account and the credentials actually used in agreement."""
    resp = account_selector_start(_orch(tmp_path, accounts={"broken": "   "}))
    assert resp.buttons is None
    assert "claude_accounts" in resp.text


@pytest.mark.asyncio
async def test_switch_rejects_blank_path_account(tmp_path: Path) -> None:
    orch = _orch(tmp_path, accounts={"broken": "  "})
    with patch(
        "ductor_bot.orchestrator.selectors.account_selector.update_config_file_async",
        new=AsyncMock(),
    ):
        text = await switch_account(orch, "broken")
    assert "broken" in text
    orch._cli_service.update_claude_account_dir.assert_not_called()


def test_selector_text_lists_accounts_for_buttonless_transports(tmp_path: Path) -> None:
    """Slack drops buttons and Matrix renders them as reactions, so the names
    have to appear in the text too."""
    resp = account_selector_start(_orch(tmp_path))
    assert "personal" in resp.text
    assert "work" in resp.text


@pytest.mark.asyncio
async def test_docker_warning_follows_service_state_not_config(tmp_path: Path) -> None:
    """docker.enabled is the request; the service knows whether a container is
    actually in use after a failed start or recovery fallback."""
    orch = _orch(tmp_path, docker=True)
    orch._cli_service.docker_enabled = False
    with patch(
        "ductor_bot.orchestrator.selectors.account_selector.update_config_file_async",
        new=AsyncMock(),
    ):
        text = await switch_account(orch, "work")
    assert "Docker" not in text


@pytest.mark.asyncio
async def test_concurrent_switches_do_not_interleave(tmp_path: Path) -> None:
    """The in-memory update and the persisted write must not be split apart."""
    import asyncio

    orch = _orch(tmp_path)
    observed: list[tuple[str, str]] = []

    async def _save(_path: Path, **updates: object) -> None:
        await asyncio.sleep(0)  # yield, inviting interleaving
        observed.append((orch._config.claude_account, str(updates["claude_account"])))

    with patch(
        "ductor_bot.orchestrator.selectors.account_selector.update_config_file_async",
        new=AsyncMock(side_effect=_save),
    ):
        await asyncio.gather(switch_account(orch, "work"), switch_account(orch, "personal"))

    # Whatever the order, memory and the persisted value always agreed.
    assert all(mem == persisted for mem, persisted in observed)


# -- /status integration -------------------------------------------------------


def test_status_line_key_exists_for_every_locale() -> None:
    """The /status account line must resolve in all shipped languages."""
    from ductor_bot.i18n import LANGUAGES, init, t

    for lang in LANGUAGES:
        init(lang)
        rendered = t("status.account_line", account="claude2")
        assert "MISSING" not in rendered
        assert "claude2" in rendered
    init("en")
