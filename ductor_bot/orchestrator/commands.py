"""Command handlers for all slash commands."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from ductor_bot.cli.auth import check_all_auth
from ductor_bot.i18n import t
from ductor_bot.infra.version import check_pypi, get_current_version
from ductor_bot.orchestrator.flows import normal
from ductor_bot.orchestrator.registry import OrchestratorResult
from ductor_bot.orchestrator.selectors.cron_selector import cron_selector_start
from ductor_bot.orchestrator.selectors.model_selector import (
    effort_selector_start,
    model_selector_start,
    switch_model,
)
from ductor_bot.orchestrator.selectors.models import Button, ButtonGrid
from ductor_bot.orchestrator.selectors.session_selector import session_selector_start
from ductor_bot.orchestrator.selectors.task_selector import task_selector_start
from ductor_bot.text.response_format import SEP, fmt, new_session_text
from ductor_bot.workspace.loader import read_mainmemory

if TYPE_CHECKING:
    from ductor_bot.orchestrator.core import Orchestrator
    from ductor_bot.session.key import SessionKey

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _PlannerCommand:
    target_name: str | None
    prompt: str
    turn_off: bool


def _parse_planner_command(text: str) -> _PlannerCommand:
    """Parse `/plan` and `/implement` into a target, prompt, and off-toggle."""
    parts = text.strip().split(None, 1)
    rest = parts[1].strip() if len(parts) > 1 else ""
    if not rest:
        return _PlannerCommand(target_name=None, prompt="", turn_off=False)

    head, sep, tail = rest.partition(" ")
    if head == "off":
        return _PlannerCommand(target_name=None, prompt="", turn_off=True)
    if head.startswith("@"):
        target = head[1:].lower()
        remainder = tail.strip() if sep else ""
        if remainder == "off":
            return _PlannerCommand(target_name=target, prompt="", turn_off=True)
        return _PlannerCommand(target_name=target, prompt=remainder, turn_off=False)
    return _PlannerCommand(target_name=None, prompt=rest, turn_off=False)


def _planner_status_text(target: str, *, enabled: bool, waiting: bool) -> str:
    state = "on" if enabled else "off"
    suffix = " (awaiting reply)" if waiting else ""
    return f"Planner mode for {target}: {state}{suffix}"


async def _resolve_main_planner_target(orch: Orchestrator, key: SessionKey) -> tuple[str, str, bool, bool]:
    session = await orch._sessions.get_active(key)
    if session is not None:
        return session.provider, session.model, session.planner_mode, session.planner_waiting
    model_name, provider_name = orch.resolve_runtime_target(orch.config.model)
    return provider_name, model_name, False, False


async def cmd_plan(  # noqa: C901, PLR0911
    orch: Orchestrator, key: SessionKey, text: str
) -> OrchestratorResult:
    """Handle `/plan` for the main target or `/plan @name` for a named target."""
    logger.info("Plan requested")
    parsed = _parse_planner_command(text)

    if parsed.target_name:
        ns = orch.get_named_session(key.chat_id, parsed.target_name)
        if ns is None:
            return OrchestratorResult(text=t("session.not_found", name=parsed.target_name))
        if ns.status == "ended":
            return OrchestratorResult(text=t("session.ended", name=parsed.target_name))
        if ns.provider != "codex":
            return OrchestratorResult(
                text=f"Planner mode is only available for Codex sessions. `{parsed.target_name}` uses {ns.provider}."
            )
        if parsed.turn_off:
            orch.named_sessions.set_planner_mode(
                key.chat_id, parsed.target_name, enabled=False
            )
            return OrchestratorResult(
                text=_planner_status_text(f"@{parsed.target_name}", enabled=False, waiting=False)
            )
        if ns.planner_mode:
            return OrchestratorResult(
                text=_planner_status_text(
                    f"@{parsed.target_name}",
                    enabled=True,
                    waiting=ns.planner_waiting,
                )
            )
        orch.named_sessions.set_planner_mode(key.chat_id, parsed.target_name, enabled=True)
        if not parsed.prompt:
            return OrchestratorResult(
                text=_planner_status_text(f"@{parsed.target_name}", enabled=True, waiting=False)
            )
        try:
            task_id = orch.submit_named_followup_bg(
                key.chat_id,
                parsed.target_name,
                f"/implement {parsed.prompt}",
                0,
                key.topic_id,
            )
        except ValueError as exc:
            return OrchestratorResult(text=str(exc))
        return OrchestratorResult(
            text=(
                f"{_planner_status_text(f'@{parsed.target_name}', enabled=True, waiting=True)}\n"
                f"Task `{task_id}` queued."
            )
        )

    provider_name, model_name, enabled, waiting = await _resolve_main_planner_target(orch, key)
    if provider_name != "codex":
        return OrchestratorResult(
            text=f"Planner mode is only available for Codex. Current main target is {provider_name}/{model_name}."
        )
    if parsed.turn_off:
        await orch.set_main_planner_state(
            key,
            provider=provider_name,
            model=model_name,
            enabled=False,
            waiting=False,
        )
        return OrchestratorResult(text=_planner_status_text("this chat", enabled=False, waiting=False))
    if enabled:
        return OrchestratorResult(text=_planner_status_text("this chat", enabled=True, waiting=waiting))
    await orch.set_main_planner_state(
        key,
        provider=provider_name,
        model=model_name,
        enabled=True,
        waiting=bool(parsed.prompt),
    )
    if not parsed.prompt:
        return OrchestratorResult(text=_planner_status_text("this chat", enabled=True, waiting=False))
    return await normal(orch, key, parsed.prompt)


async def cmd_implement(  # noqa: PLR0911
    orch: Orchestrator, key: SessionKey, text: str
) -> OrchestratorResult:
    """Handle `/implement` for the main target or `/implement @name` for a named target."""
    logger.info("Implement requested")
    parsed = _parse_planner_command(text)

    if parsed.target_name:
        ns = orch.get_named_session(key.chat_id, parsed.target_name)
        if ns is None:
            return OrchestratorResult(text=t("session.not_found", name=parsed.target_name))
        if ns.status == "ended":
            return OrchestratorResult(text=t("session.ended", name=parsed.target_name))
        if ns.provider != "codex":
            return OrchestratorResult(
                text=f"Planner mode is only available for Codex sessions. `{parsed.target_name}` uses {ns.provider}."
            )
        orch.named_sessions.set_planner_mode(key.chat_id, parsed.target_name, enabled=False)
        if not parsed.prompt:
            return OrchestratorResult(
                text=_planner_status_text(f"@{parsed.target_name}", enabled=False, waiting=False)
            )
        try:
            task_id = orch.submit_named_followup_bg(
                key.chat_id,
                parsed.target_name,
                parsed.prompt,
                0,
                key.topic_id,
            )
        except ValueError as exc:
            return OrchestratorResult(text=str(exc))
        return OrchestratorResult(
            text=(
                f"{_planner_status_text(f'@{parsed.target_name}', enabled=False, waiting=False)}\n"
                f"Task `{task_id}` queued."
            )
        )

    provider_name, model_name, _enabled, _waiting = await _resolve_main_planner_target(orch, key)
    if provider_name != "codex":
        return OrchestratorResult(
            text=f"Planner mode is only available for Codex. Current main target is {provider_name}/{model_name}."
        )
    await orch.set_main_planner_state(
        key,
        provider=provider_name,
        model=model_name,
        enabled=False,
        waiting=False,
    )
    if not parsed.prompt:
        return OrchestratorResult(text=_planner_status_text("this chat", enabled=False, waiting=False))
    # Keep the explicit implementation token in the provider prompt. The
    # Ductor command parser consumes `/implement`; without forwarding it,
    # Codex's planner overlay asks for the command again.
    return await normal(orch, key, f"/implement {parsed.prompt}")


# -- Command wrappers (registered by Orchestrator._register_commands) --


async def cmd_reset(orch: Orchestrator, key: SessionKey, _text: str) -> OrchestratorResult:
    """Handle /new: kill processes and reset only active provider session."""
    logger.info("Reset requested")
    await orch._process_registry.kill_by_chat_topic(key.chat_id, key.topic_id)
    provider = await orch.reset_active_provider_session(key)
    return OrchestratorResult(text=new_session_text(provider))


async def cmd_reset_current(orch: Orchestrator, key: SessionKey, _text: str) -> OrchestratorResult:
    """Handle /reset: kill processes and reset the *current* provider session."""
    logger.info("Reset (current) requested")
    await orch._process_registry.kill_by_chat_topic(key.chat_id, key.topic_id)
    provider = await orch.reset_current_provider_session(key)
    return OrchestratorResult(text=new_session_text(provider))


async def cmd_status(orch: Orchestrator, key: SessionKey, _text: str) -> OrchestratorResult:
    """Handle /status."""
    logger.info("Status requested")
    return OrchestratorResult(text=await _build_status(orch, key))


async def cmd_model(orch: Orchestrator, key: SessionKey, text: str) -> OrchestratorResult:
    """Handle /model [name]."""
    logger.info("Model requested")
    parts = text.split(None, 1)
    if len(parts) < 2:
        resp = await model_selector_start(orch, key)
        return OrchestratorResult(text=resp.text, buttons=resp.buttons)
    name = parts[1].strip()
    result_text = await switch_model(orch, key, name)
    return OrchestratorResult(text=result_text)


async def cmd_effort(orch: Orchestrator, key: SessionKey, _text: str) -> OrchestratorResult:
    """Handle /effort: show reasoning-effort buttons for the active provider."""
    logger.info("Effort requested")
    resp = await effort_selector_start(orch, key)
    return OrchestratorResult(text=resp.text, buttons=resp.buttons)


_FAST_MODELS = ("gpt-5.6", "gpt-5.5", "gpt-5.4")


def _fast_status_text(enabled: bool) -> str:
    state = "ON" if enabled else "OFF"
    return (
        f"Fast mode: {state}\n"
        "Applies only to this conversation's Codex turns. GPT-5.6 Fast mode uses credits at 2.5x the Standard rate."
    )


async def cmd_fast(orch: Orchestrator, key: SessionKey, text: str) -> OrchestratorResult:
    """Handle `/fast on`, `/fast off`, and `/fast status` per conversation."""
    logger.info("Fast mode requested")
    parts = text.strip().split()
    action = parts[1].lower() if len(parts) == 2 else "status" if len(parts) == 1 else ""
    if action not in {"on", "off", "status"}:
        return OrchestratorResult(text="Usage: /fast on, /fast off, or /fast status")

    session = await orch._sessions.get_active(key)
    if session is None:
        model, provider = orch.resolve_runtime_target(orch._config.model)
        enabled = False
    else:
        model, provider, enabled = session.model, session.provider, session.fast_mode

    if provider != "codex" or not model.startswith(_FAST_MODELS):
        return OrchestratorResult(
            text=(
                "Fast mode is available only for Codex GPT-5.6, GPT-5.5, and GPT-5.4 sessions. "
                f"This conversation uses {provider}/{model}."
            )
        )
    if action == "status":
        return OrchestratorResult(text=_fast_status_text(enabled))

    enabled = action == "on"
    await orch.set_main_fast_mode(key, provider=provider, model=model, enabled=enabled)
    return OrchestratorResult(text=_fast_status_text(enabled))


async def cmd_fast_on(orch: Orchestrator, key: SessionKey, _text: str) -> OrchestratorResult:
    """Enable Fast mode from Telegram's argument-free command menu."""
    return await cmd_fast(orch, key, "/fast on")


async def cmd_memory(orch: Orchestrator, _key: SessionKey, _text: str) -> OrchestratorResult:
    """Handle /memory."""
    logger.info("Memory requested")
    content = await asyncio.to_thread(read_mainmemory, orch.paths)
    if not content.strip():
        return OrchestratorResult(
            text=fmt(
                t("memory.header"),
                SEP,
                t("memory.empty"),
                SEP,
                t("memory.empty_tip"),
            ),
        )
    return OrchestratorResult(
        text=fmt(
            t("memory.header"),
            SEP,
            content,
            SEP,
            t("memory.filled_tip"),
        ),
    )


async def cmd_sessions(orch: Orchestrator, key: SessionKey, _text: str) -> OrchestratorResult:
    """Handle /sessions."""
    logger.info("Sessions requested")
    resp = await session_selector_start(orch, key)
    return OrchestratorResult(text=resp.text, buttons=resp.buttons)


async def cmd_tasks(orch: Orchestrator, key: SessionKey, _text: str) -> OrchestratorResult:
    """Handle /tasks."""
    logger.info("Tasks requested")
    hub = orch.task_hub
    if hub is None:
        return OrchestratorResult(
            text=fmt(t("tasks.header"), SEP, t("tasks.disabled")),
        )
    resp = task_selector_start(hub, key.chat_id)
    return OrchestratorResult(text=resp.text, buttons=resp.buttons)


async def cmd_cron(orch: Orchestrator, _key: SessionKey, _text: str) -> OrchestratorResult:
    """Handle /cron."""
    logger.info("Cron requested")
    resp = await cron_selector_start(orch)
    return OrchestratorResult(text=resp.text, buttons=resp.buttons)


async def cmd_upgrade(_orch: Orchestrator, _key: SessionKey, _text: str) -> OrchestratorResult:
    """Handle /upgrade: check for updates and offer upgrade."""
    logger.info("Upgrade check requested")

    from ductor_bot.infra.install import detect_install_mode

    if detect_install_mode() == "dev":
        return OrchestratorResult(
            text=fmt(
                t("upgrade.dev_header"),
                SEP,
                t("upgrade.dev_body"),
            ),
        )

    info = await check_pypi(fresh=True)

    if info is None:
        return OrchestratorResult(
            text=t("upgrade.pypi_unreachable"),
        )

    if not info.update_available:
        keyboard = ButtonGrid(
            rows=[
                [
                    Button(
                        text=t("upgrade.btn_changelog", version=info.current),
                        callback_data=f"upg:cl:{info.current}",
                    )
                ],
            ]
        )
        return OrchestratorResult(
            text=fmt(
                t("upgrade.up_to_date_header"),
                SEP,
                t("upgrade.up_to_date_body", current=info.current, latest=info.latest),
            ),
            buttons=keyboard,
        )

    keyboard = ButtonGrid(
        rows=[
            [
                Button(
                    text=t("upgrade.btn_changelog", version=info.latest),
                    callback_data=f"upg:cl:{info.latest}",
                )
            ],
            [
                Button(
                    text=t("upgrade.btn_yes"),
                    callback_data=f"upg:yes:{info.latest}",
                ),
                Button(text=t("upgrade.btn_not_now"), callback_data="upg:no"),
            ],
        ]
    )

    return OrchestratorResult(
        text=fmt(
            t("upgrade.available_header"),
            SEP,
            t("upgrade.available_body", current=info.current, latest=info.latest),
        ),
        buttons=keyboard,
    )


def _build_codex_cache_block(orch: Orchestrator) -> str:
    """Build the Codex model cache section for /diagnose."""
    if not orch._observers.codex_cache_obs:
        return "\n🔄 " + t("diagnose.codex_cache_not_init")
    cache = orch._observers.codex_cache_obs.get_cache()
    if not cache or not cache.models:
        return "\n🔄 " + t("diagnose.codex_cache_not_loaded")
    default_model = next((m.id for m in cache.models if m.is_default), "N/A")
    return "\n🔄 " + t(
        "diagnose.codex_cache_info",
        updated=cache.last_updated,
        count=len(cache.models),
        default=default_model,
    )


def _build_diagnose_health_block(orch: Orchestrator) -> str:
    """Build the multi-agent health section for /diagnose."""
    supervisor = orch._supervisor
    if supervisor is None:
        return ""
    status_icon = {"running": "●", "starting": "◐", "crashed": "✖", "stopped": "○"}
    agent_lines = ["\n" + t("diagnose.health_header")]
    for name in sorted(supervisor.health.keys()):
        h = supervisor.health[name]
        icon = status_icon.get(h.status, "?")
        role = "main" if name == "main" else "sub"
        line = f"  {icon} `{name}` [{role}] — {h.status}"
        if h.status == "running" and h.uptime_human:
            line += f" ({h.uptime_human})"
        if h.restart_count > 0:
            line += f" | restarts: {h.restart_count}"
        if h.status == "crashed" and h.last_crash_error:
            line += f"\n      `{h.last_crash_error[:100]}`"
        agent_lines.append(line)
    return "\n".join(agent_lines)


def _resolve_log_path(orch: Orchestrator) -> Path:
    """Return the best available log file path.

    Sub-agents don't have their own log files — fall back to the central
    log in the main ductor home (parent of ``agents/<name>``).
    """
    log_path = orch.paths.logs_dir / "agent.log"
    if not log_path.exists():
        main_logs = orch.paths.ductor_home.parent.parent / "logs" / "agent.log"
        if main_logs.exists():
            return main_logs
    return log_path


async def cmd_diagnose(orch: Orchestrator, _key: SessionKey, _text: str) -> OrchestratorResult:
    """Handle /diagnose."""
    logger.info("Diagnose requested")
    version = get_current_version()
    effective_model, effective_provider = orch.resolve_runtime_target(orch._config.model)
    info_block = (
        f"{t('diagnose.version_line', version=version)}\n"
        f"{t('diagnose.configured_line', provider=orch._config.provider, model=orch._config.model)}\n"
        f"{t('diagnose.effective_line', provider=effective_provider, model=effective_model)}"
    )

    cache_block = _build_codex_cache_block(orch)
    agent_block = _build_diagnose_health_block(orch)

    log_tail = await _read_log_tail(_resolve_log_path(orch))
    log_block = (
        f"{t('diagnose.log_header')}\n```\n{log_tail}\n```" if log_tail else t("diagnose.no_log")
    )

    return OrchestratorResult(
        text=fmt(t("diagnose.header"), SEP, info_block, cache_block, agent_block, SEP, log_block),
    )


# -- Helpers ------------------------------------------------------------------


def _build_agent_health_block(orch: Orchestrator) -> str:
    """Build the multi-agent health section for /status (main agent only)."""
    supervisor = orch._supervisor
    if supervisor is None or len(supervisor.health) <= 1:
        return ""

    status_icon = {
        "running": "●",
        "starting": "◐",
        "crashed": "✖",
        "stopped": "○",
    }
    agent_lines = [t("status.agents_header")]
    for name in sorted(supervisor.health.keys()):
        if name == "main":
            continue
        h = supervisor.health[name]
        icon = status_icon.get(h.status, "?")
        line = f"  {icon} {name} — {h.status}"
        if h.status == "running" and h.uptime_human:
            line += f" ({h.uptime_human})"
        if h.restart_count > 0:
            line += f" ⟳{h.restart_count}"
        if h.status == "crashed" and h.last_crash_error:
            line += f"\n      {h.last_crash_error[:80]}"
        agent_lines.append(line)
    return "\n".join(agent_lines)


def _status_effort_suffix(orch: Orchestrator, model_name: str, effort: str) -> str:
    """Return the ``/status`` reasoning-effort line for effort-using providers.

    *effort* is the effective effort (the session's value in a topic, else the
    global default) so /status reflects what the next turn actually uses.
    """
    provider = orch.models.provider_for(model_name)
    if provider in ("codex", "claude", "grok") and effort and effort != "default":
        return f"\n{t('status.effort_line', effort=effort)}"
    return ""


async def _build_status(orch: Orchestrator, key: SessionKey) -> str:
    """Build the /status response text."""
    runtime_model, _runtime_provider = orch.resolve_runtime_target(orch._config.model)
    configured_model = orch._config.model

    def _model_line(model_name: str) -> str:
        if model_name == configured_model:
            return t("status.model_line", model=model_name)
        return t("status.model_line_configured", model=model_name, configured=configured_model)

    session = await orch._sessions.get_active(key)
    if session:
        topic_line = (
            f"{t('status.topic_line', topic=session.topic_name)}\n" if session.topic_name else ""
        )
        session_block = (
            f"{topic_line}"
            f"{t('status.session_line', sid=session.session_id[:8] + '...')}\n"
            f"{t('status.messages_line', count=session.message_count)}\n"
            f"{t('status.tokens_line', tokens=f'{session.total_tokens:,}')}\n"
            f"{t('status.cost_line', cost=f'{session.total_cost_usd:.4f}')}\n"
            f"{_model_line(session.model)}"
            f"{_status_effort_suffix(orch, session.model, session.reasoning_effort or orch._config.reasoning_effort)}"
        )
    else:
        session_block = (
            f"{t('status.no_session')}\n"
            f"{_model_line(runtime_model)}{_status_effort_suffix(orch, runtime_model, orch._config.reasoning_effort)}"
        )

    bg_tasks = orch.active_background_tasks(key.chat_id)
    bg_block = ""
    if bg_tasks:
        import time

        bg_lines = [t("status.bg_header", count=len(bg_tasks))]
        for bg_t in bg_tasks:
            age = time.monotonic() - bg_t.submitted_at
            bg_lines.append(f"  `{bg_t.task_id}` {bg_t.prompt[:40]}... ({age:.0f}s)")
        bg_block = "\n".join(bg_lines)

    auth = await asyncio.to_thread(check_all_auth)
    auth_lines: list[str] = []
    for provider, result in auth.items():
        age_label = f" ({result.age_human})" if result.age_human else ""
        auth_lines.append(f"  [{provider}] {result.status.value}{age_label}")
    auth_block = t("status.auth_header") + "\n" + "\n".join(auth_lines)

    streaming_cfg = orch._config.streaming
    streaming_block = "\n".join(
        [
            "Streaming visibility:",
            f"  Reasoning stream: {'on' if streaming_cfg.show_reasoning_stream else 'off'}",
            f"  Tool progress: {'on' if streaming_cfg.show_tool_progress else 'off'}",
            f"  Thinking indicator: {'on' if streaming_cfg.show_thinking_indicator else 'off'}",
        ]
    )

    agent_block = _build_agent_health_block(orch)

    blocks = [t("status.header"), SEP, session_block]
    if bg_block:
        blocks += [SEP, bg_block]
    blocks += [SEP, auth_block, SEP, streaming_block]
    if agent_block:
        blocks += [SEP, agent_block]
    return fmt(*blocks)


async def _read_log_tail(log_path: Path, lines: int = 50) -> str:
    """Read the last *lines* of a log file without blocking the event loop."""

    def _read() -> str:
        if not log_path.is_file():
            return ""
        try:
            text = log_path.read_text(encoding="utf-8", errors="replace")
            return "\n".join(text.strip().splitlines()[-lines:])
        except OSError:
            return "(could not read log file)"

    return await asyncio.to_thread(_read)
