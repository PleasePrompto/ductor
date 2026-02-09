"""Command handlers for all slash commands."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import TYPE_CHECKING

from aiogram.types import InlineKeyboardButton, InlineKeyboardMarkup

from ductor_bot.cli.auth import check_all_auth
from ductor_bot.infra.version import check_pypi
from ductor_bot.orchestrator.model_selector import model_selector_start, switch_model
from ductor_bot.orchestrator.registry import OrchestratorResult
from ductor_bot.workspace.loader import read_mainmemory

if TYPE_CHECKING:
    from ductor_bot.orchestrator.core import Orchestrator

logger = logging.getLogger(__name__)


# -- Command wrappers (registered by Orchestrator._register_commands) --


async def cmd_reset(orch: Orchestrator, chat_id: int, _text: str) -> OrchestratorResult:
    """Handle /new: kill processes and reset session."""
    logger.info("Reset requested")
    await orch._process_registry.kill_all(chat_id)
    await orch._sessions.reset_session(chat_id)
    return OrchestratorResult(text="**Fresh session.** Everything cleared -- ready to go.")


async def cmd_stop(orch: Orchestrator, chat_id: int, _text: str) -> OrchestratorResult:
    """Handle /stop: kill all active processes."""
    logger.info("Stop requested")
    killed = await orch._process_registry.kill_all(chat_id)
    if killed:
        provider = orch.active_provider_name
        return OrchestratorResult(
            text=f"**{provider} has been terminated!** All queued messages will be discarded.",
        )
    return OrchestratorResult(text="Nothing running right now.")


async def cmd_status(orch: Orchestrator, chat_id: int, _text: str) -> OrchestratorResult:
    """Handle /status."""
    logger.info("Status requested")
    return OrchestratorResult(text=await _build_status(orch, chat_id))


async def cmd_model(orch: Orchestrator, chat_id: int, text: str) -> OrchestratorResult:
    """Handle /model [name]."""
    logger.info("Model requested")
    parts = text.split(None, 1)
    if len(parts) < 2:
        msg_text, keyboard = await model_selector_start(orch, chat_id)
        return OrchestratorResult(text=msg_text, reply_markup=keyboard)
    name = parts[1].strip()
    result_text = await switch_model(orch, chat_id, name)
    return OrchestratorResult(text=result_text)


async def cmd_memory(orch: Orchestrator, _chat_id: int, _text: str) -> OrchestratorResult:
    """Handle /memory."""
    logger.info("Memory requested")
    content = await asyncio.to_thread(read_mainmemory, orch.paths)
    if not content.strip():
        return OrchestratorResult(
            text="**Main Memory**\n\nEmpty. The agent will start building memory as you interact.",
        )
    return OrchestratorResult(text=f"**Main Memory**\n\n{content}")


async def cmd_cron(orch: Orchestrator, _chat_id: int, _text: str) -> OrchestratorResult:
    """Handle /cron."""
    logger.info("Cron requested")
    jobs = orch._cron_manager.list_jobs()
    if not jobs:
        return OrchestratorResult(
            text="**Scheduled Tasks**\n\nNo cron jobs configured yet.",
        )
    lines = ["**Scheduled Tasks**", ""]
    for j in jobs:
        status = f" [{j.last_run_status}]" if j.last_run_status else ""
        enabled = "" if j.enabled else " (disabled)"
        lines.append(f"  `{j.schedule}` -- {j.title}{enabled}{status}")
    return OrchestratorResult(text="\n".join(lines))


async def cmd_upgrade(_orch: Orchestrator, _chat_id: int, _text: str) -> OrchestratorResult:
    """Handle /upgrade: check for updates and offer upgrade."""
    logger.info("Upgrade check requested")

    from ductor_bot.infra.install import detect_install_mode

    if detect_install_mode() == "dev":
        return OrchestratorResult(
            text=(
                "**Running from source**\n\n"
                "Self-upgrade is not available for development installs.\n"
                "Update with `git pull` in your project directory."
            ),
        )

    info = await check_pypi()

    if info is None:
        return OrchestratorResult(
            text="Could not reach PyPI to check for updates. Try again later.",
        )

    if not info.update_available:
        keyboard = InlineKeyboardMarkup(
            inline_keyboard=[
                [
                    InlineKeyboardButton(
                        text=f"Changelog v{info.current}",
                        callback_data=f"upg:cl:{info.current}",
                    ),
                ],
            ],
        )
        return OrchestratorResult(
            text=(
                f"**Already up to date**\n\n"
                f"Installed: `{info.current}`\n"
                f"Latest:    `{info.latest}`\n\n"
                f"You're running the latest version."
            ),
            reply_markup=keyboard,
        )

    keyboard = InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text=f"Changelog v{info.latest}",
                    callback_data=f"upg:cl:{info.latest}",
                ),
            ],
            [
                InlineKeyboardButton(
                    text="Yes, upgrade now", callback_data=f"upg:yes:{info.latest}"
                ),
                InlineKeyboardButton(text="Not now", callback_data="upg:no"),
            ],
        ],
    )

    return OrchestratorResult(
        text=(
            f"**Update available**\n\n"
            f"Installed: `{info.current}`\n"
            f"New:       `{info.latest}`\n\n"
            f"Upgrade now?"
        ),
        reply_markup=keyboard,
    )


async def cmd_diagnose(orch: Orchestrator, _chat_id: int, _text: str) -> OrchestratorResult:
    """Handle /diagnose."""
    logger.info("Diagnose requested")
    sections: list[str] = ["**System Diagnostics**", ""]

    log_path = orch.paths.logs_dir / "agent.log"
    log_tail = await _read_log_tail(log_path)
    if log_tail:
        sections.append("Recent logs (last 50 lines):")
        sections.append(f"```\n{log_tail}\n```")
    else:
        sections.append("No log file found.")

    return OrchestratorResult(text="\n".join(sections))


# -- Helpers ------------------------------------------------------------------


async def _build_status(orch: Orchestrator, chat_id: int) -> str:
    """Build the /status response text."""
    session = await orch._sessions.get_active(chat_id)
    if session:
        session_block = (
            f"Session: `{session.session_id[:8]}...`\n"
            f"Messages: {session.message_count}\n"
            f"Tokens: {session.total_tokens:,}\n"
            f"Cost: ${session.total_cost_usd:.4f}"
        )
    else:
        session_block = "No active session."

    auth = await asyncio.to_thread(check_all_auth)
    auth_lines: list[str] = []
    for provider, result in auth.items():
        age = f" ({result.age_human})" if result.age_human else ""
        auth_lines.append(f"  [{provider}] {result.status.value}{age}")
    auth_block = "\n".join(auth_lines)

    return f"**Status**\n\n{session_block}\nModel: {orch._config.model}\n\nAuth:\n{auth_block}"


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
