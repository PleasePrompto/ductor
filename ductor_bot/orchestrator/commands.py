"""Command handlers for all slash commands."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from ductor_bot.cli.auth import check_all_auth
from ductor_bot.cli.claude_accounts import active_claude_account_dir
from ductor_bot.cli.types import AgentRequest
from ductor_bot.errors import CLIError
from ductor_bot.handoff.prompts import CONSOLIDATION_PROMPT
from ductor_bot.i18n import t
from ductor_bot.infra.version import check_pypi, get_current_version
from ductor_bot.orchestrator.registry import OrchestratorResult
from ductor_bot.orchestrator.selectors.account_selector import (
    account_selector_start,
    switch_account,
)
from ductor_bot.orchestrator.selectors.consult_selector import consult_selector
from ductor_bot.orchestrator.selectors.cron_selector import cron_selector_start
from ductor_bot.orchestrator.selectors.folder_selector import folder_selector
from ductor_bot.orchestrator.selectors.model_selector import (
    effort_selector_start,
    model_selector_start,
    switch_model,
)
from ductor_bot.orchestrator.selectors.models import Button, ButtonGrid
from ductor_bot.orchestrator.selectors.persona_selector import persona_selector
from ductor_bot.orchestrator.selectors.session_selector import session_selector_start
from ductor_bot.orchestrator.selectors.skills_selector import skill_detail, skills_root
from ductor_bot.orchestrator.selectors.task_selector import task_selector_start
from ductor_bot.text.response_format import SEP, fmt
from ductor_bot.workspace.loader import read_mainmemory

if TYPE_CHECKING:
    from ductor_bot.orchestrator.core import Orchestrator
    from ductor_bot.session.key import SessionKey

logger = logging.getLogger(__name__)


# -- Command wrappers (registered by Orchestrator._register_commands) --


async def _consolidate_handoff(orch: Orchestrator, key: SessionKey) -> None:
    """Run one silent turn asking the model to write the handoff up properly.

    Best effort by design: a consolidation that fails must not block the user
    from compacting or clearing, and the previous handoff stays on disk either
    way — the store refuses to replace a good file with an empty one.
    """
    session = await orch._sessions.get_active(key)
    if session is None or not session.session_id:
        return
    request = AgentRequest(
        prompt=CONSOLIDATION_PROMPT,
        chat_id=key.chat_id,
        topic_id=key.topic_id,
        transport=key.transport,
        resume_session=session.session_id,
        process_label="handoff_consolidation",
    )
    try:
        await orch._cli_service.execute(request)
    except (CLIError, RuntimeError, OSError) as exc:
        logger.warning("Handoff consolidation failed chat=%d: %s", key.chat_id, exc)


async def cmd_compact(orch: Orchestrator, key: SessionKey, _text: str) -> OrchestratorResult:
    """Handle /compact: same work, empty context window.

    There is no on-demand compaction in print mode, so this reclaims the window
    the only way available — a new session — and carries the work across in the
    handoff. The persona and the folder binding stay: nothing about the task
    has changed, only how much of it the model can see.
    """
    logger.info("Compact requested")
    await _consolidate_handoff(orch, key)
    await orch._process_registry.kill_by_chat_topic(key.chat_id, key.topic_id)
    await orch.reset_active_provider_session(key)
    orch.reinject.mark(key)
    return OrchestratorResult(text=t("handoff.compacted"))


async def cmd_clear(orch: Orchestrator, key: SessionKey, _text: str) -> OrchestratorResult:
    """Handle /clear: this task is finished, start another in the same topic.

    The handoff is consolidated first and then archived, so it stays available
    under Archived without being read back automatically. If archiving fails the
    session is left alone: losing a handoff is worse than not clearing.
    """
    logger.info("Clear requested")
    folder = orch.bindings.resolve(key.storage_key)
    await _consolidate_handoff(orch, key)

    had_handoff = bool(orch.handoffs.read(key, folder).strip())
    archived = orch.handoffs.archive(key, folder)
    if had_handoff and archived is None:
        return OrchestratorResult(text=t("handoff.archive_failed"))

    orch.personas.clear(key.storage_key)
    await orch._process_registry.kill_by_chat_topic(key.chat_id, key.topic_id)
    await orch.reset_active_provider_session(key)
    return OrchestratorResult(text=t("handoff.cleared"))


async def cmd_handoff(orch: Orchestrator, key: SessionKey, _text: str) -> OrchestratorResult:
    """Handle /handoff: the current handoff, and this conversation's archives."""
    logger.info("Handoff requested")
    folder = orch.bindings.resolve(key.storage_key)
    body = orch.handoffs.read(key, folder)
    if not body.strip():
        return OrchestratorResult(text=t("handoff.none_yet"))
    return OrchestratorResult(text=f"{body[:3000]}")


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


async def cmd_account(orch: Orchestrator, _key: SessionKey, text: str) -> OrchestratorResult:
    """Handle /account [name]: show or switch the Claude credential store."""
    logger.info("Account requested")
    parts = text.split(None, 1)
    if len(parts) < 2:
        resp = account_selector_start(orch)
        return OrchestratorResult(text=resp.text, buttons=resp.buttons)
    return OrchestratorResult(text=await switch_account(orch, parts[1].strip()))


async def cmd_skills(orch: Orchestrator, _key: SessionKey, text: str) -> OrchestratorResult:
    """Handle /skills [name]: browse skills by plugin, or show one in full."""
    logger.info("Skills requested")
    parts = text.split(None, 1)
    resp = skill_detail(orch, parts[1]) if len(parts) > 1 else skills_root(orch)
    return OrchestratorResult(text=resp.text, buttons=resp.buttons)


async def cmd_persona(orch: Orchestrator, key: SessionKey, _text: str) -> OrchestratorResult:
    """Handle /persona: choose which agent governs this conversation."""
    logger.info("Persona requested")
    resp = persona_selector(orch, key)
    return OrchestratorResult(text=resp.text, buttons=resp.buttons)


async def cmd_folder(orch: Orchestrator, key: SessionKey, _text: str) -> OrchestratorResult:
    """Handle /folder: choose which directory this conversation works in."""
    logger.info("Folder selection requested")
    if orch.bindings.is_protected(key.storage_key):
        return OrchestratorResult(text=t("folder.general_locked"))
    resp = folder_selector(orch, key)
    return OrchestratorResult(text=resp.text, buttons=resp.buttons)


async def cmd_consult(orch: Orchestrator, _key: SessionKey, _text: str) -> OrchestratorResult:
    """Handle /consult: how often the Consult topic is wiped."""
    logger.info("Consult schedule requested")
    resp = consult_selector(orch)
    return OrchestratorResult(text=resp.text, buttons=resp.buttons)


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
    resp = await session_selector_start(orch, key.chat_id)
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

    auth = await asyncio.to_thread(check_all_auth, active_claude_account_dir(orch._config))
    auth_lines: list[str] = []
    for provider, result in auth.items():
        age_label = f" ({result.age_human})" if result.age_human else ""
        auth_lines.append(f"  [{provider}] {result.status.value}{age_label}")
    # Which Claude subscription the next turn will spend. Only shown when more
    # than one credential store is configured, since otherwise there is nothing
    # to disambiguate.
    if orch._config.claude_accounts:
        active = orch._config.claude_account or t("account.default_label")
        auth_lines.append(f"  {t('status.account_line', account=active)}")
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
