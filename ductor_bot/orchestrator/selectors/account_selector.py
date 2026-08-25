"""Claude account selector for ``/account``.

Switches which credential store Claude Code authenticates with, leaving the
config dir — and therefore the resumable session — untouched. The intended use
is hitting a subscription rate limit mid-conversation and continuing on a second
account without losing context.

The choice is global (like the configured default model), not per-topic: the
credential store is a property of the CLI process, and a per-topic value would
make it unclear which subscription a background task or cron job is spending.
"""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from ductor_bot.cli.claude_accounts import (
    account_names,
    resolve_account_dir,
    usable_accounts,
)
from ductor_bot.config import update_config_file_async
from ductor_bot.i18n import t
from ductor_bot.orchestrator.selectors.models import Button, ButtonGrid, SelectorResponse

if TYPE_CHECKING:
    from ductor_bot.orchestrator.core import Orchestrator

logger = logging.getLogger(__name__)

ACC_PREFIX = "acc:"

#: Accounts are addressed in callback data by index into the sorted name list,
#: not by name: Telegram caps callback_data at 64 UTF-8 bytes, and any reserved
#: string marker would collide with an account legitimately named that.
#: Index -1 is the default store.
_DEFAULT_INDEX = -1

_BUTTONS_PER_ROW = 2

#: The selection is global, so two topics (or transports) switching at once could
#: interleave the in-memory update with the read-modify-write of config.json and
#: leave them disagreeing. One process-wide lock covers both.
_switch_lock = asyncio.Lock()


def is_account_selector_callback(data: str) -> bool:
    """Return True if *data* belongs to the account selector."""
    return data.startswith(ACC_PREFIX)


def _label(name: str) -> str:
    """Return the display label for an account name."""
    return name or t("account.default_label")


def _active_name(orch: Orchestrator) -> str:
    return orch._config.claude_account


def _header(orch: Orchestrator) -> str:
    return t("account.active_line", account=_label(_active_name(orch)))


def account_selector_start(orch: Orchestrator) -> SelectorResponse:
    """Build the ``/account`` response: one button per configured account."""
    accounts = usable_accounts(orch._config.claude_accounts)
    if not accounts:
        return SelectorResponse(text=t("account.none_configured"))

    active = _active_name(orch)
    names = account_names(accounts)

    buttons = [
        Button(
            text=f"✅ {_label('')}" if not active else _label(""),
            callback_data=f"{ACC_PREFIX}{_DEFAULT_INDEX}",
        )
    ]
    buttons += [
        Button(
            text=f"✅ {name}" if name == active else name,
            callback_data=f"{ACC_PREFIX}{i}",
        )
        for i, name in enumerate(names)
    ]
    rows = [buttons[i : i + _BUTTONS_PER_ROW] for i in range(0, len(buttons), _BUTTONS_PER_ROW)]

    # Spelled out in the text as well: Matrix renders buttons as reactions and
    # Slack drops them entirely, so a button-only list would leave those
    # transports with no way to see what can be selected.
    listing = "\n".join(f"• `{_label('')}`" if not n else f"• `{n}`" for n in ["", *names])
    return SelectorResponse(
        text=f"{_header(orch)}\n\n{t('account.select')}\n{listing}",
        buttons=ButtonGrid(rows=rows),
    )


async def switch_account(orch: Orchestrator, name: str) -> str:
    """Activate the credential store for *name* and persist the choice.

    An empty *name* selects the default store. Returns user-facing text.
    """
    accounts = usable_accounts(orch._config.claude_accounts)
    if name and name not in accounts:
        return t("account.unknown", account=name, known=", ".join(account_names(accounts)))

    async with _switch_lock:
        account_dir = resolve_account_dir(accounts, name) or ""
        orch._config.claude_account = name
        orch._cli_service.update_claude_account_dir(account_dir)
        await update_config_file_async(orch.paths.config_path, claude_account=name)
    logger.info("Claude account switched to %r", name or "default")

    # docker.enabled is the requested configuration; after a failed start or a
    # recovery fallback the service can be running on the host with an empty
    # container name, where the account switch does apply. Ask the service.
    if getattr(orch._cli_service, "docker_enabled", False):
        return t("account.switched_docker_warning", account=_label(name))
    return t("account.switched", account=_label(name))


async def handle_account_callback(orch: Orchestrator, data: str) -> SelectorResponse:
    """Apply an ``acc:*`` callback and redraw the selector in place."""
    payload = data[len(ACC_PREFIX) :]
    names = account_names(orch._config.claude_accounts)
    try:
        index = int(payload)
    except ValueError:
        logger.debug("Bad account callback payload: %r", payload)
        return account_selector_start(orch)

    if index == _DEFAULT_INDEX:
        name = ""
    elif 0 <= index < len(names):
        name = names[index]
    else:
        logger.debug("Account index out of range: %d", index)
        return account_selector_start(orch)

    result = await switch_account(orch, name)
    redrawn = account_selector_start(orch)
    return SelectorResponse(text=f"{result}\n\n{redrawn.text}", buttons=redrawn.buttons)
