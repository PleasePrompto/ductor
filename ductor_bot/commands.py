"""Bot command definitions shared across layers.

Commands are ordered by usage frequency (most used first).
Descriptions are kept ≤22 chars so mobile clients don't truncate.
"""

from __future__ import annotations

from ductor_bot.i18n import t_cmd

# -- Core commands (every agent, shown in Telegram popup) ------------------
# Sorted by typical usage: daily actions → power-user → rare maintenance.


def get_bot_commands() -> list[tuple[str, str]]:
    """Return bot commands with translated descriptions."""
    return [
        # Daily
        ("clear", t_cmd("bot.clear")),
        ("compact", t_cmd("bot.compact")),
        ("handoff", t_cmd("bot.handoff")),
        ("stop", t_cmd("bot.stop")),
        ("interrupt", t_cmd("bot.interrupt")),
        ("model", t_cmd("bot.model")),
        ("effort", t_cmd("bot.effort")),
        ("account", t_cmd("bot.account")),
        ("persona", t_cmd("bot.persona")),
        ("folder", t_cmd("bot.folder")),
        ("consult", t_cmd("bot.consult")),
        ("skills", t_cmd("bot.skills")),
        ("status", t_cmd("bot.status")),
        ("memory", t_cmd("bot.memory")),
        # Automation & multi-agent
        ("session", t_cmd("bot.session")),
        ("cron", t_cmd("bot.cron")),
        ("agent_commands", t_cmd("bot.agent_commands")),
        # Browse & info
        ("files", t_cmd("bot.files")),
        ("menu", t_cmd("bot.menu")),
        ("info", t_cmd("bot.info")),
        ("help", t_cmd("bot.help")),
        # Maintenance (rare)
        ("diagnose", t_cmd("bot.diagnose")),
        ("upgrade", t_cmd("bot.upgrade")),
        ("restart", t_cmd("bot.restart")),
    ]


#: Commands the inline menu already offers. They stay registered, keep their
#: descriptions, and still appear in /help — they are only dropped from
#: Telegram's "/" picker, which is otherwise a list of twenty-four entries most
#: of which are one tap away in the menu.
#:
#: /help is deliberately NOT hidden despite being in the menu: it is the way
#: back when the menu itself is broken, and that has already happened once.
MENU_DUPLICATES = frozenset(
    {
        "files",
        "folder",
        "persona",
        "model",
        "account",
        "skills",
        "compact",
        "clear",
        "handoff",
        "status",
        "consult",
    }
)


def get_picker_commands() -> list[tuple[str, str]]:
    """Commands offered in Telegram's "/" list.

    A subset of :func:`get_bot_commands`, which stays complete: /help renders
    from the full list, so hiding an entry here never makes it undiscoverable.
    """
    return [(name, desc) for name, desc in get_bot_commands() if name not in MENU_DUPLICATES]


def get_multiagent_sub_commands() -> list[tuple[str, str]]:
    """Return multi-agent sub-commands with translated descriptions."""
    return [
        ("agents", t_cmd("multiagent.agents")),
        ("agent_start", t_cmd("multiagent.agent_start")),
        ("agent_stop", t_cmd("multiagent.agent_stop")),
        ("agent_restart", t_cmd("multiagent.agent_restart")),
        ("stop_all", t_cmd("multiagent.stop_all")),
    ]


# Backward-compatible module-level aliases.
# These are evaluated at import time, so i18n must be auto-initialized by then.
BOT_COMMANDS: list[tuple[str, str]] = get_bot_commands()
MULTIAGENT_SUB_COMMANDS: list[tuple[str, str]] = get_multiagent_sub_commands()
PICKER_COMMANDS: list[tuple[str, str]] = get_picker_commands()
