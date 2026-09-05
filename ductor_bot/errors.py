"""Project-level exception hierarchy and safe error formatting."""

from __future__ import annotations

import re

# Error messages can originate in provider CLIs.  Keep useful diagnostics while
# making sure a provider accidentally echoing a credential cannot be copied to
# a user-facing task result or to the technical task traceback log.
_BEARER_RE = re.compile(r"(?i)\bBearer\s+[A-Za-z0-9._~+/=-]+")
_SECRET_RE = re.compile(
    r"(?i)\b(?P<key>api[_-]?key|access[_-]?token|refresh[_-]?token|auth(?:orization)?|"
    r"password|passwd|secret|credential|private[_-]?key|token)\b"
    r"(?P<separator>\s*[:=]\s*|\s+)(?P<value>[^\s,;]+)"
)


def redact_error_text(value: object, *, max_length: int = 500) -> str:
    """Return a bounded error string with common credential forms redacted."""
    text = str(value).strip()
    text = _BEARER_RE.sub("Bearer [redacted]", text)
    text = _SECRET_RE.sub(lambda match: f"{match.group('key')}=[redacted]", text)
    return text[:max_length]


def safe_exception_message(exc: BaseException) -> str:
    """Format an exception for users without exposing its traceback or secrets."""
    message = redact_error_text(exc)
    return message or f"{type(exc).__name__}; see logs for technical details"


class DuctorError(Exception):
    """Base for all ductor exceptions."""


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
