"""Messenger abstraction layer — transport-agnostic protocols and registry."""

from ductor_bot.messenger.commands import (
    DIRECT_COMMANDS,
    MULTIAGENT_COMMANDS,
    ORCHESTRATOR_COMMANDS,
    classify_command,
)
from ductor_bot.messenger.multi import MultiBotAdapter
from ductor_bot.messenger.notifications import CompositeNotificationService, NotificationService
from ductor_bot.messenger.protocol import BotProtocol
from ductor_bot.messenger.registry import create_bot
from ductor_bot.messenger.send_opts import BaseSendOpts

__all__ = [
    "DIRECT_COMMANDS",
    "MULTIAGENT_COMMANDS",
    "ORCHESTRATOR_COMMANDS",
    "BaseSendOpts",
    "BotProtocol",
    "CompositeNotificationService",
    "MultiBotAdapter",
    "NotificationService",
    "classify_command",
    "create_bot",
]
