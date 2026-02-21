"""Interactive model selector wizard for Telegram inline keyboards."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from ductor_bot.cli.auth import AuthStatus, check_all_auth
from ductor_bot.config import update_config_file_async

if TYPE_CHECKING:
    from ductor_bot.cli.codex_cache import CodexModelCache
    from ductor_bot.orchestrator.core import Orchestrator

logger = logging.getLogger(__name__)

MS_PREFIX = "ms:"

_CLAUDE_MODELS = ("haiku", "sonnet", "opus")

_EFFORT_LABELS: dict[str, str] = {
    "low": "Low",
    "medium": "Medium",
    "high": "High",
    "xhigh": "XHigh",
}


def is_model_selector_callback(data: str) -> bool:
    """Return True if *data* belongs to the model selector wizard."""
    return data.startswith(MS_PREFIX)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def model_selector_start(
    orch: Orchestrator,
    chat_id: int,
) -> tuple[str, InlineKeyboardMarkup | None]:
    """Build the initial ``/model`` response with provider buttons.

    Returns ``(text, keyboard)``. Keyboard is ``None`` when no providers
    are authenticated.
    """
    auth = await asyncio.to_thread(check_all_auth)
    authed = [name for name, res in auth.items() if res.status == AuthStatus.AUTHENTICATED]

    header = await _status_line(orch, chat_id)

    if not authed:
        return (
            f"{header}\n\n"
            "No authenticated providers found.\n"
            "Run `claude auth` or `codex auth` to get started.",
            None,
        )

    if len(authed) == 1:
        provider = authed[0]
        codex_cache = orch._codex_cache_observer.get_cache() if orch._codex_cache_observer else None
        return await _build_model_step(provider, header, codex_cache)

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="CLAUDE", callback_data="ms:p:claude"),
                InlineKeyboardButton(text="CODEX", callback_data="ms:p:codex"),
                InlineKeyboardButton(text="GEMINI", callback_data="ms:p:gemini"),
            ]
        ]
    )
    return f"{header}\n\nPick a provider:", keyboard


async def handle_model_callback(
    orch: Orchestrator,
    chat_id: int,
    data: str,
) -> tuple[str, InlineKeyboardMarkup | None]:
    """Route an ``ms:*`` callback to the correct wizard step.

    Returns ``(text, keyboard)`` for editing the message in-place.
    """
    logger.debug("Model selector step=%s", data[:40])
    parts = data[len(MS_PREFIX) :].split(":", 2)
    action = parts[0] if parts else ""
    payload = parts[1] if len(parts) > 1 else ""
    extra = parts[2] if len(parts) > 2 else ""

    codex_cache = orch._codex_cache_observer.get_cache() if orch._codex_cache_observer else None

    if action == "p":
        return await _build_model_step(payload, await _status_line(orch, chat_id), codex_cache)

    if action == "m":
        return await _handle_model_selected(orch, chat_id, payload, codex_cache)

    if action == "r":
        return await _handle_reasoning_selected(orch, chat_id, effort=payload, model_id=extra)

    if action == "b":
        if payload == "root":
            return await model_selector_start(orch, chat_id)
        return await _build_model_step(payload, await _status_line(orch, chat_id), codex_cache)

    logger.warning("Unknown model selector callback: %s", data)
    return "Unknown action.", None


async def switch_model(
    orch: Orchestrator,
    chat_id: int,
    model_id: str,
    *,
    reasoning_effort: str | None = None,
) -> str:
    """Execute model switch: kill processes, reset session, persist config.

    Shared by ``/model <name>`` text command and the wizard callbacks.
    """
    old = orch._config.model
    same_model = old == model_id
    effort_only = same_model and reasoning_effort is not None

    if same_model and reasoning_effort is None:
        return f"Already running {model_id}. No changes made."

    old_provider = orch._models.provider_for(old)
    new_provider = orch._models.provider_for(model_id)
    provider_changed = old_provider != new_provider

    if not same_model:
        await orch._process_registry.kill_all(chat_id)
        await orch._sessions.reset_session(chat_id, provider=new_provider, model=model_id)

    orch._config.model = model_id
    orch._cli_service.update_default_model(model_id)
    if provider_changed:
        orch._config.provider = new_provider

    updates: dict[str, object] = {"model": model_id, "provider": orch._config.provider}

    if reasoning_effort is not None:
        orch._config.reasoning_effort = reasoning_effort
        orch._cli_service.update_reasoning_effort(reasoning_effort)
        updates["reasoning_effort"] = reasoning_effort

    await update_config_file_async(orch.paths.config_path, **updates)

    logger.info("Model switch model=%s provider=%s", model_id, orch._config.provider)

    parts: list[str] = ["**Model switched.**"]
    if same_model:
        parts.append(f"Model: {model_id}")
    else:
        parts.append(f"Model: {old} -> {model_id}")
    if provider_changed:
        parts.append(f"Provider: {old_provider} -> {new_provider}")
    if reasoning_effort:
        parts.append(f"Reasoning: {reasoning_effort}")
    if not same_model:
        parts.append("\nSession reset. Send a message to continue.")
    elif effort_only:
        parts.append("\nReasoning effort updated.")
    return "\n".join(parts)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _status_line(orch: Orchestrator, chat_id: int) -> str:
    """Current model + reasoning effort as a short header."""
    session = await orch._sessions.get_active(chat_id)
    if session:
        model = session.model
        provider = session.provider
    else:
        model, provider = orch.resolve_runtime_target(orch._config.model)

    configured = orch._config.model
    effort = orch._config.reasoning_effort

    if provider == "codex":
        current = f"**Model Selector**\nCurrent: {model} ({effort})"
    else:
        current = f"**Model Selector**\nCurrent: {model}"

    if model != configured:
        current += f"\nConfigured default: {configured}"

    return current


async def _build_model_step(
    provider: str,
    header: str,
    codex_cache: CodexModelCache | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    """Build the model selection keyboard for a provider."""
    if provider == "claude":
        buttons = [
            InlineKeyboardButton(text=m.upper(), callback_data=f"ms:m:{m}") for m in _CLAUDE_MODELS
        ]
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                buttons,
                [InlineKeyboardButton(text="<< Back", callback_data="ms:b:root")],
            ]
        )
        return f"{header}\n\nSelect Claude model:", keyboard

    if provider == "gemini":
        # Group 1: Core Aliases
        core = ["auto", "pro", "flash", "flash-lite"]
        # Group 2: Specific/Preview versions
        versions = [
            "gemini-2.5-flash-lite",
            "gemini-2.5-flash",
            "gemini-2.5-pro",
            "gemini-3-flash-preview",
            "gemini-3-pro-preview",
        ]

        rows = []
        # Row 1: Core aliases
        rows.append([InlineKeyboardButton(text=m.upper(), callback_data=f"ms:m:{m}") for m in core])

        # Version models in rows of 2
        current_row = []
        for m in versions:
            label = m.replace("gemini-", "")
            current_row.append(InlineKeyboardButton(text=label, callback_data=f"ms:m:{m}"))
            if len(current_row) >= 2:
                rows.append(current_row)
                current_row = []
        if current_row:
            rows.append(current_row)

        rows.append([InlineKeyboardButton(text="<< Back", callback_data="ms:b:root")])

        keyboard = InlineKeyboardMarkup(inline_keyboard=rows)
        return f"{header}\n\nSelect Gemini model:", keyboard

    # Use cache instead of live discovery
    codex_models = codex_cache.models if codex_cache else []
    if not codex_models:
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [InlineKeyboardButton(text="<< Back", callback_data="ms:b:root")],
            ]
        )
        return f"{header}\n\nNo Codex models available.", keyboard

    rows: list[list[InlineKeyboardButton]] = [
        [InlineKeyboardButton(text=m.display_name, callback_data=f"ms:m:{m.id}")]
        for m in codex_models
    ]
    rows.append([InlineKeyboardButton(text="<< Back", callback_data="ms:b:root")])

    keyboard = InlineKeyboardMarkup(inline_keyboard=rows)
    return f"{header}\n\nSelect Codex model:", keyboard


async def _handle_model_selected(
    orch: Orchestrator,
    chat_id: int,
    model_id: str,
    codex_cache: CodexModelCache | None = None,
) -> tuple[str, InlineKeyboardMarkup | None]:
    """Handle a model button press. Claude: switch immediately. Codex: show reasoning."""
    provider = orch._models.provider_for(model_id)

    if provider == "claude":
        result = await switch_model(orch, chat_id, model_id)
        return result, None

    if provider == "gemini":
        result = await switch_model(orch, chat_id, model_id)
        return result, None

    # Use cache instead of live discovery
    codex_info = codex_cache.get_model(model_id) if codex_cache else None
    efforts = codex_info.supported_efforts if codex_info else ("low", "medium", "high", "xhigh")

    buttons = [
        InlineKeyboardButton(
            text=_EFFORT_LABELS.get(e, e),
            callback_data=f"ms:r:{e}:{model_id}",
        )
        for e in efforts
    ]
    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            buttons,
            [InlineKeyboardButton(text="<< Back", callback_data="ms:b:codex")],
        ]
    )

    header = await _status_line(orch, chat_id)
    return f"{header}\n\nThinking level for {model_id}:", keyboard


async def _handle_reasoning_selected(
    orch: Orchestrator,
    chat_id: int,
    *,
    effort: str,
    model_id: str,
) -> tuple[str, InlineKeyboardMarkup | None]:
    """Handle a reasoning effort button press. Final step: switch model + effort."""
    result = await switch_model(orch, chat_id, model_id, reasoning_effort=effort)
    return result, None
