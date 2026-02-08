"""Project-level exception hierarchy."""


class DuctorError(Exception):
    """Base for all ductor-bot exceptions."""


class CLIError(DuctorError):
    """CLI execution failed."""


class WorkspaceError(DuctorError):
    """Workspace initialization or access failed."""


class SessionError(DuctorError):
    """Session persistence or lifecycle failed."""


class CronError(DuctorError):
    """Cron job scheduling or execution failed."""


class StreamError(DuctorError):
    """Streaming output failed."""


class SecurityError(DuctorError):
    """Security violation detected."""


class PathValidationError(SecurityError):
    """File path failed validation."""


class WebhookError(DuctorError):
    """Webhook server or dispatch failed."""
