"""Interactive model selector wizard for Telegram inline keyboards."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ductor_bot.cli.auth import AuthStatus, check_all_auth
from ductor_bot.config import (
    ANTIGRAVITY_MODELS_ORDERED,
    CLAUDE_MODELS_ORDERED,
    get_antigravity_models,
    get_gemini_models,
    update_config_file_async,
)
from ductor_bot.i18n import t
from ductor_bot.multiagent.registry import update_agent_fields
from ductor_bot.orchestrator.selectors.models import Button, ButtonGrid, SelectorResponse

if TYPE_CHECKING:
    from ductor_bot.cli.codex_cache import CodexModelCache
    from ductor_bot.orchestrator.core import Orchestrator
    from ductor_bot.session import SessionData
    from ductor_bot.session.key import SessionKey

logger = logging.getLogger(__name__)

MS_PREFIX = "ms:"

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
    message_label = t("model.resume_msg_one") if message_count == 1 else t("model.resume_msg_other")
    sid_display = session_id or "pending"
    return "\n" + t(
        "model.resume_hint",
        sid=sid_display,
        count=message_count,
        label=message_label,
        model=model_id,
    )


def _build_switch_summary(ctx: _SwitchSummaryContext) -> str:
    """Build user-facing model switch summary text."""
    parts: list[str] = [t("model.switched")]
    if ctx.old_model == ctx.new_model:
        parts.append(t("model.model_line", model=ctx.new_model))
    else:
        parts.append(t("model.model_change", old=ctx.old_model, new=ctx.new_model))
    if ctx.provider_changed:
        parts.append(t("model.provider_change", old=ctx.old_provider, new=ctx.new_provider))
    if ctx.reasoning_effort:
        parts.append(t("model.reasoning_line", effort=ctx.reasoning_effort))
    if ctx.old_model != ctx.new_model and ctx.resume_message_count > 0:
        parts.append(
            _format_resume_hint(
                ctx.resume_session_id,
                ctx.resume_message_count,
                ctx.new_model,
            )
        )
    if ctx.effort_only:
        parts.append("\n" + t("model.effort_updated"))
    return "\n".join(parts)


def _validate_codex_reasoning_effort(
    orch: Orchestrator,
    model_id: str,
    reasoning_effort: str | None,
) -> str | None:
    """Return a user-facing error when Codex effort is unsupported."""
    if not reasoning_effort or orch.models.provider_for(model_id) != "codex":
        return None

    codex_cache = (
        orch._observers.codex_cache_obs.get_cache() if orch._observers.codex_cache_obs else None
    )
    if codex_cache is None:
        return None

    model_info = codex_cache.get_model(model_id)
    if model_info is None:
        return None

    supported = tuple(model_info.supported_efforts)
    if reasoning_effort in supported:
        return None

    supported_display = ", ".join(supported) if supported else "none"
    return (
        f"Invalid reasoning effort `{reasoning_effort}` for `{model_id}`. "
        f"Supported: {supported_display}."
    )


# Built-in Gemini aliases accepted by the Gemini CLI as `--model` values.
# `auto` lets the CLI pick the best model per request; `pro`/`flash`/`flash-lite`
# select the latest model of that tier without pinning a specific version.
_GEMINI_ALIAS_ORDER: tuple[str, ...] = ("auto", "pro", "flash", "flash-lite")


def _gemini_models_for_selector() -> list[str]:
    """Return Gemini built-in aliases, then models discovered from the CLI files."""
    models = sorted(get_gemini_models())
    # Prefer stable models before previews in the selector.
    stable = [model for model in models if "preview" not in model]
    preview = [model for model in models if "preview" in model]
    return [*_GEMINI_ALIAS_ORDER, *stable, *preview]


def _antigravity_models_for_selector() -> list[str]:
    """Return the agy default, then models discovered from ``agy models``."""
    discovered = sorted(get_antigravity_models())
    return [*ANTIGRAVITY_MODELS_ORDERED, *discovered]


def _button_label(model_id: str) -> str:
    """Compact button label while preserving identity in callback data."""
    return model_id.removeprefix("gemini-").removeprefix("auto-")


def _chunk_buttons(
    model_ids: list[str],
    *,
    columns: int = 3,
) -> list[list[Button]]:
    rows: list[list[Button]] = []
    for index in range(0, len(model_ids), columns):
        chunk = model_ids[index : index + columns]
        rows.append(
            [
                Button(
                    text=_button_label(model_id),
                    callback_data=f"ms:m:{model_id}",
                )
                for model_id in chunk
            ]
        )
    return rows


def is_model_selector_callback(data: str) -> bool:
    """Return True if *data* belongs to the model selector wizard."""
    return data.startswith(MS_PREFIX)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


async def model_selector_start(
    orch: Orchestrator,
    key: SessionKey,
) -> SelectorResponse:
    """Build the initial ``/model`` response with provider buttons.

    Returns a ``SelectorResponse``. Buttons are ``None`` when no providers
    are authenticated.
    """
    auth = await asyncio.to_thread(check_all_auth)
    authed = [name for name, res in auth.items() if res.status == AuthStatus.AUTHENTICATED]

    header = await _status_line(orch, key)

    if not authed:
        return SelectorResponse(
            text=f"{header}\n\n{t('model.no_auth')}",
        )

    if len(authed) == 1:
        provider = authed[0]
        codex_cache = (
            orch._observers.codex_cache_obs.get_cache() if orch._observers.codex_cache_obs else None
        )
        return await _build_model_step(provider, header, codex_cache)

    buttons: list[Button] = []
    if "claude" in authed:
        buttons.append(Button(text="CLAUDE", callback_data="ms:p:claude"))
    if "codex" in authed:
        buttons.append(Button(text="CODEX", callback_data="ms:p:codex"))
    if "gemini" in authed:
        buttons.append(Button(text="GEMINI", callback_data="ms:p:gemini"))
    if "antigravity" in authed:
        buttons.append(Button(text="ANTIGRAVITY", callback_data="ms:p:antigravity"))

    provider_rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    keyboard = ButtonGrid(rows=provider_rows)
    return SelectorResponse(text=f"{header}\n\n{t('model.pick_provider')}", buttons=keyboard)


async def handle_model_callback(
    orch: Orchestrator,
    key: SessionKey,
    data: str,
) -> SelectorResponse:
    """Route an ``ms:*`` callback to the correct wizard step.

    Returns a ``SelectorResponse`` for editing the message in-place.
    """
    logger.debug("Model selector step=%s", data[:40])
    parts = data[len(MS_PREFIX) :].split(":", 2)
    action = parts[0] if parts else ""
    payload = parts[1] if len(parts) > 1 else ""
    extra = parts[2] if len(parts) > 2 else ""

    codex_cache = (
        orch._observers.codex_cache_obs.get_cache() if orch._observers.codex_cache_obs else None
    )

    if action == "p":
        return await _build_model_step(payload, await _status_line(orch, key), codex_cache)

    if action == "m":
        return await _handle_model_selected(orch, key, payload, codex_cache)

    if action == "r":
        return await _handle_reasoning_selected(orch, key, effort=payload, model_id=extra)

    if action == "b":
        if payload == "root":
            return await model_selector_start(orch, key)
        return await _build_model_step(payload, await _status_line(orch, key), codex_cache)

    logger.warning("Unknown model selector callback: %s", data)
    return SelectorResponse(text=t("model.unknown_action"))


async def switch_model(  # noqa: C901
    orch: Orchestrator,
    key: SessionKey,
    model_id: str,
    *,
    reasoning_effort: str | None = None,
) -> str:
    """Execute model switch: kill processes, preserve sessions, persist config.

    Shared by ``/model <name>`` text command and the wizard callbacks.
    """
    is_topic = key.topic_id is not None
    active_session = await orch._sessions.get_active(key)

    old = active_session.model if is_topic and active_session else orch._config.model
    same_model = old == model_id
    effort_only = same_model and reasoning_effort is not None

    if same_model and reasoning_effort is None:
        return t("model.already_running", model=model_id)

    old_provider = orch.models.provider_for(old)
    new_provider = orch.models.provider_for(model_id)
    provider_changed = old_provider != new_provider

    validation_error = _validate_codex_reasoning_effort(orch, model_id, reasoning_effort)
    if validation_error is not None:
        return validation_error

    resume_source = active_session
    if not same_model:
        await orch._process_registry.kill_all(key.chat_id)
        if is_topic:
            resolved, created = await orch._sessions.resolve_session_target(
                key,
                provider=new_provider,
                model=model_id,
            )
            resume_source = None if created else resolved
        elif active_session is not None:
            await orch._sessions.sync_session_target(
                active_session,
                provider=new_provider,
                model=model_id,
            )
    resume_session_id, resume_message_count = _resume_state_for_provider(
        resume_source,
        new_provider,
    )

    if not is_topic:
        # Global config: update only from main chat / DM (not from topics).
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

        # Sub-agent: also sync model/provider/effort to agents.json so the
        # registry stays current and survives restarts without merge hacks.
        if orch.paths.ductor_home.parent.name == "agents":
            agents_path = orch.paths.ductor_home.parent.parent / "agents.json"
            agent_name = orch._cli_service._config.agent_name
            registry_updates = dict(updates)
            # Only Codex uses reasoning_effort — remove it when switching away
            if new_provider != "codex" and "reasoning_effort" not in registry_updates:
                registry_updates["reasoning_effort"] = None
            await asyncio.to_thread(
                update_agent_fields, agents_path, agent_name, **registry_updates
            )

    is_busy = orch.is_chat_busy(key.chat_id, key.topic_id)
    logger.info("Model switch model=%s provider=%s", model_id, orch._config.provider)

    summary = _build_switch_summary(
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
    if is_busy:
        summary += f"\n\n_{t('model.switched_while_busy')}_"
    return summary


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


async def _status_line(orch: Orchestrator, key: SessionKey) -> str:
    """Current model + reasoning effort as a short header."""
    session = await orch._sessions.get_active(key)
    if session:
        model = session.model
        provider = session.provider
    else:
        model, provider = orch.resolve_runtime_target(orch._config.model)

    effort = orch._config.reasoning_effort

    if provider == "codex":
        current = (
            f"{t('model.header')}\n{t('model.current_with_effort', model=model, effort=effort)}"
        )
    else:
        current = f"{t('model.header')}\n{t('model.current', model=model)}"

    if key.topic_id is None:
        configured = orch._config.model
        if model != configured:
            current += f"\n{t('model.configured_default', configured=configured)}"

    return current


async def _build_model_step(
    provider: str,
    header: str,
    codex_cache: CodexModelCache | None = None,
) -> SelectorResponse:
    """Build the model selection keyboard for a provider."""
    if provider == "claude":
        buttons = [Button(text=m.upper(), callback_data=f"ms:m:{m}") for m in CLAUDE_MODELS_ORDERED]
        keyboard = ButtonGrid(
            rows=[
                buttons,
                [Button(text=t("model.btn_back"), callback_data="ms:b:root")],
            ]
        )
        return SelectorResponse(text=f"{header}\n\n{t('model.select_claude')}", buttons=keyboard)

    if provider == "gemini":
        gemini_models = _gemini_models_for_selector()
        if not gemini_models:
            keyboard = ButtonGrid(
                rows=[
                    [Button(text=t("model.btn_back"), callback_data="ms:b:root")],
                ]
            )
            return SelectorResponse(
                text=f"{header}\n\n{t('model.no_gemini')}",
                buttons=keyboard,
            )

        gemini_rows = _chunk_buttons(gemini_models)
        gemini_rows.append([Button(text=t("model.btn_back"), callback_data="ms:b:root")])
        keyboard = ButtonGrid(rows=gemini_rows)
        return SelectorResponse(text=f"{header}\n\n{t('model.select_gemini')}", buttons=keyboard)

    if provider == "antigravity":
        antigravity_rows = _chunk_buttons(_antigravity_models_for_selector(), columns=1)
        antigravity_rows.append([Button(text=t("model.btn_back"), callback_data="ms:b:root")])
        keyboard = ButtonGrid(rows=antigravity_rows)
        return SelectorResponse(
            text=f"{header}\n\n{t('model.select_antigravity')}", buttons=keyboard
        )

    # Use cache instead of live discovery
    codex_models = codex_cache.models if codex_cache else []
    if not codex_models:
        keyboard = ButtonGrid(
            rows=[
                [Button(text=t("model.btn_back"), callback_data="ms:b:root")],
            ]
        )
        return SelectorResponse(text=f"{header}\n\n{t('model.no_codex')}", buttons=keyboard)

    rows: list[list[Button]] = [
        [Button(text=m.display_name, callback_data=f"ms:m:{m.id}")] for m in codex_models
    ]
    rows.append([Button(text=t("model.btn_back"), callback_data="ms:b:root")])

    keyboard = ButtonGrid(rows=rows)
    return SelectorResponse(text=f"{header}\n\n{t('model.select_codex')}", buttons=keyboard)


async def _handle_model_selected(
    orch: Orchestrator,
    key: SessionKey,
    model_id: str,
    codex_cache: CodexModelCache | None = None,
) -> SelectorResponse:
    """Handle a model button press. Codex shows reasoning; other providers switch directly."""
    provider = orch.models.provider_for(model_id)

    if provider in ("claude", "gemini", "antigravity"):
        result = await switch_model(orch, key, model_id)
        return SelectorResponse(text=result)

    # Use cache instead of live discovery
    codex_info = codex_cache.get_model(model_id) if codex_cache else None
    efforts = codex_info.supported_efforts if codex_info else ("low", "medium", "high", "xhigh")

    buttons = [
        Button(
            text=_EFFORT_LABELS.get(e, e),
            callback_data=f"ms:r:{e}:{model_id}",
        )
        for e in efforts
    ]
    keyboard = ButtonGrid(
        rows=[
            buttons,
            [Button(text=t("model.btn_back"), callback_data="ms:b:codex")],
        ]
    )

    header = await _status_line(orch, key)
    return SelectorResponse(
        text=f"{header}\n\n{t('model.thinking_level', model=model_id)}", buttons=keyboard
    )


async def _handle_reasoning_selected(
    orch: Orchestrator,
    key: SessionKey,
    *,
    effort: str,
    model_id: str,
) -> SelectorResponse:
    """Handle a reasoning effort button press. Final step: switch model + effort."""
    result = await switch_model(orch, key, model_id, reasoning_effort=effort)
    return SelectorResponse(text=result)
