# Development task runner for ductor
# Requires: just 1.42.0+ (https://github.com/casey/just)

min_version := "1.42.0"
current_version := `just --version | cut -d' ' -f2`
_check_version := if semver_matches(current_version, ">=" + min_version) == "true" { "" } else { error("just >= " + min_version + " required for [parallel]") }

# Auto-fix formatting and lint issues
fix:
    uv run ruff format .
    uv run ruff check --fix .

# Run all linters, type checks, and tests (parallel)
[parallel]
check: _lint _format _types _test

# Run the test suite only
test *args:
    uv run pytest -n auto {{args}}

[private]
_lint:
    uv run ruff check .

[private]
_format:
    uv run ruff format --check .

[private]
_types:
    uv run mypy ductor_bot

[private]
_test:
    uv run pytest -n auto
