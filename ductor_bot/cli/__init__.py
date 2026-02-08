"""CLI layer: provider abstraction, process tracking, streaming."""

from ductor_bot.cli.auth import AuthResult as AuthResult
from ductor_bot.cli.auth import AuthStatus as AuthStatus
from ductor_bot.cli.auth import check_all_auth as check_all_auth
from ductor_bot.cli.base import BaseCLI as BaseCLI
from ductor_bot.cli.base import CLIConfig as CLIConfig
from ductor_bot.cli.coalescer import CoalesceConfig as CoalesceConfig
from ductor_bot.cli.coalescer import StreamCoalescer as StreamCoalescer
from ductor_bot.cli.factory import create_cli as create_cli
from ductor_bot.cli.process_registry import ProcessRegistry as ProcessRegistry
from ductor_bot.cli.service import CLIService as CLIService
from ductor_bot.cli.service import CLIServiceConfig as CLIServiceConfig
from ductor_bot.cli.types import AgentRequest as AgentRequest
from ductor_bot.cli.types import AgentResponse as AgentResponse
from ductor_bot.cli.types import CLIResponse as CLIResponse

__all__ = [
    "AgentRequest",
    "AgentResponse",
    "AuthResult",
    "AuthStatus",
    "BaseCLI",
    "CLIConfig",
    "CLIResponse",
    "CLIService",
    "CLIServiceConfig",
    "CoalesceConfig",
    "ProcessRegistry",
    "StreamCoalescer",
    "check_all_auth",
    "create_cli",
]
