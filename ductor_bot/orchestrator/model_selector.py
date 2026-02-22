"""Interactive model selector wizard for Telegram inline keyboards."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from ductor_bot.cli.auth import AuthStatus, check_all_auth
from ductor_bot.config import update_config_file_async

if TYPE_CHECKING:
    from ductor_bot.cli.codex_cache import CodexModelCache
    from ductor_bot.orchestrator.core import Orchestrator
    from ductor_bot.session import SessionData

logger = logging.getLogger(__name__)

MS_PREFIX = "ms:"

_CLAUDE_MODELS = ("haiku", "sonnet", "opus")

_EFFORT_LABELS: dict[str, str] = {
    "low": "Low",
    "medium": "Medium",
    "high": "High",
    "xhigh": "XHigh",
}


@dataclass(frozen=True)
class _SwitchSummaryContext:
    old_model: str
    new_model: str
    old_provider: str
    new_provider: str
    provider_changed: bool
    reasoning_effort: str | None
    effort_only: bool
    resume_session_id: str
    resume_message_count: int


def _resume_state_for_provider(session: SessionData | None, provider: str) -> tuple[str, int]:
    """Return (session_id, message_count) for provider if resumable history exists."""
    if session is None:
        return "", 0
    provider_data = session.provider_sessions.get(provider)
    if provider_data is None or provider_data.message_count <= 0:
        return "", 0
    return provider_data.session_id, provider_data.message_count


def _format_resume_hint(session_id: str, message_count: int, model_id: str) -> str:
    """Build post-switch resume hint text."""
    message_label = "message" if message_count == 1 else "messages"
    sid_display = session_id or "pending"
    return (
        "\n"
        f"Resuming session `{sid_display}`.\n"
        f"You have already sent {message_count} {message_label} in this provider session.\n"
        f"Current model: `{model_id}`.\n"
        "Use /new to start a fresh session."
    )


def _build_switch_summary(ctx: _SwitchSummaryContext) -> str:
    """Build user-facing model switch summary text."""
    parts: list[str] = ["**Model switched.**"]
    if ctx.old_model == ctx.new_model:
        parts.append(f"Model: {ctx.new_model}")
    else:
        parts.append(f"Model: {ctx.old_model} -> {ctx.new_model}")
    if ctx.provider_changed:
        parts.append(f"Provider: {ctx.old_provider} -> {ctx.new_provider}")
    if ctx.reasoning_effort:
        parts.append(f"Reasoning: {ctx.reasoning_effort}")
    if ctx.old_model != ctx.new_model and ctx.resume_message_count > 0:
        parts.append(
            _format_resume_hint(
                ctx.resume_session_id,
                ctx.resume_message_count,
                ctx.new_model,
            )
        )
    if ctx.effort_only:
        parts.append("\nReasoning effort updated.")
    return "\n".join(parts)


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
    """Execute model switch: kill processes, preserve sessions, persist config.

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

    active_session = await orch._sessions.get_active(chat_id)
    resume_session_id, resume_message_count = _resume_state_for_provider(
        active_session,
        new_provider,
    )

    if not same_model:
        await orch._process_registry.kill_all(chat_id)
        if active_session is not None:
            await orch._sessions.sync_session_target(
                active_session,
                provider=new_provider,
                model=model_id,
            )

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

    return _build_switch_summary(
        _SwitchSummaryContext(
            old_model=old,
            new_model=model_id,
            old_provider=old_provider,
            new_provider=new_provider,
            provider_changed=provider_changed,
            reasoning_effort=reasoning_effort,
            effort_only=effort_only,
            resume_session_id=resume_session_id,
            resume_message_count=resume_message_count,
        )
    )


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
