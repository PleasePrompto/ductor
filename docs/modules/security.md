# security/

Input and file-path safety utilities.

## Files

- `content.py`: suspicious prompt-pattern detection.
- `paths.py`: file path validation against allowed roots.

## Public API

- `detect_suspicious_patterns(text) -> list[str]`
- `validate_file_path(path, allowed_roots) -> Path`
- `is_path_safe(path, allowed_roots) -> bool`

## Suspicious Pattern Detection

`detect_suspicious_patterns()` returns matched pattern names such as:

- `instruction_override`
- `role_hijack`
- `fake_system_prompt`
- `special_token`
- `llama_markers`
- `anthropic_markers`
- `internal_file_ref`
- `tool_injection`
- `cli_flag_injection`
- `file_tag_injection`

Implementation details:

- folds selected fullwidth characters to ASCII before matching (`_fold_fullwidth`).
- marker sanitization helper (`_sanitize_markers`) exists, but `detect_suspicious_patterns()` currently scans folded text directly.
- returns matches only; does not block by itself.

Current use in orchestrator: log warning only.

## Path Validation

`validate_file_path()`:

1. reject null bytes,
2. reject control characters,
3. resolve to absolute path,
4. require containment in one of `allowed_roots` via `is_relative_to`.

Violation raises `PathValidationError`.

`is_path_safe()` wraps this as a non-throwing boolean check.
