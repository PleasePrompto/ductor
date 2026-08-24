"""Transport-neutral callback routing for button responses."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ductor_bot.orchestrator.core import Orchestrator
    from ductor_bot.orchestrator.selectors.models import ButtonGrid
    from ductor_bot.session.key import SessionKey


@dataclass(frozen=True, slots=True)
class CallbackResult:
    """Outcome of a callback routing attempt.

    ``text``
        Human-readable response text (may be empty for unhandled callbacks).
    ``buttons``
        Optional follow-up button grid for multi-step selectors.
    ``handled``
        ``True`` when the callback was processed by a shared selector.
        ``False`` when the prefix is transport-specific (``upg:``, ``ns:``,
        ``mq:``, ``fb:``) and should be handled by the transport bot itself.
    """

    text: str = ""
    buttons: ButtonGrid | None = None
    handled: bool = True


async def route_callback(  # noqa: PLR0911
    orch: Orchestrator,
    key: SessionKey,
    callback_data: str,
) -> CallbackResult:
    """Route *callback_data* to the appropriate shared selector handler.

    Shared prefixes (handled here):

    * ``acc:`` -- Claude account selector
    * ``ms:`` -- model selector
    * ``crn:`` -- cron selector
    * ``nsc:`` -- session selector
    * ``tsc:`` -- task selector

    Transport-specific prefixes (returned as ``handled=False``):

    * ``upg:`` -- upgrade flow
    * ``ns:`` -- named-session follow-up
    * ``mq:`` -- message-queue cancel (Telegram only)
    * ``fb:`` -- file browser (Telegram only)

    Returns a :class:`CallbackResult`.  When ``handled`` is ``False``, the
    transport bot must process the callback itself.
    """
    from ductor_bot.orchestrator.selectors.account_selector import (
        handle_account_callback,
        is_account_selector_callback,
    )
    from ductor_bot.orchestrator.selectors.cron_selector import (
        handle_cron_callback,
        is_cron_selector_callback,
    )
    from ductor_bot.orchestrator.selectors.model_selector import (
        handle_model_callback,
        is_model_selector_callback,
    )
    from ductor_bot.orchestrator.selectors.session_selector import (
        handle_session_callback,
        is_session_selector_callback,
    )
    from ductor_bot.orchestrator.selectors.task_selector import (
        handle_task_callback,
        is_task_selector_callback,
    )

    if is_account_selector_callback(callback_data):
        resp = await handle_account_callback(orch, callback_data)
        return CallbackResult(text=resp.text, buttons=resp.buttons)

    if is_model_selector_callback(callback_data):
        resp = await handle_model_callback(orch, key, callback_data)
        return CallbackResult(text=resp.text, buttons=resp.buttons)

    if is_cron_selector_callback(callback_data):
        resp = await handle_cron_callback(orch, callback_data)
        return CallbackResult(text=resp.text, buttons=resp.buttons)

    if is_session_selector_callback(callback_data):
        resp = await handle_session_callback(orch, key.chat_id, callback_data)
        return CallbackResult(text=resp.text, buttons=resp.buttons)

    if is_task_selector_callback(callback_data):
        hub = orch.task_hub
        if hub is None:
            return CallbackResult(text="Task system not available.", buttons=None)
        resp = await handle_task_callback(hub, key.chat_id, callback_data)
        return CallbackResult(text=resp.text, buttons=resp.buttons)

    # Transport-specific prefixes -- signal the caller to handle them.
    return CallbackResult(handled=False)
