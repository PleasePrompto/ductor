"""Security primitives: injection defense, path validation."""

from ductor_bot.security.content import detect_suspicious_patterns as detect_suspicious_patterns
from ductor_bot.security.paths import is_path_safe as is_path_safe
from ductor_bot.security.paths import validate_file_path as validate_file_path

__all__ = [
    "detect_suspicious_patterns",
    "is_path_safe",
    "validate_file_path",
]
