"""The prompts, and the framing of what goes back into context."""

from __future__ import annotations

from ductor_bot.handoff.prompts import (
    CONSOLIDATION_PROMPT,
    DELTA_SUFFIX,
    TEMPLATE,
    injection_block,
)


def test_the_template_names_every_required_section() -> None:
    for section in (
        "## Objective",
        "## Current state",
        "## Done",
        "## Next",
        "## Open questions",
        "## Constraints",
        "## Dead ends",
        "## Artifacts",
        "## Log",
    ):
        assert section in TEMPLATE


def test_the_delta_allows_doing_nothing() -> None:
    """A turn that changed nothing should cost nothing."""
    assert "do nothing" in DELTA_SUFFIX.lower()


def test_the_delta_forbids_rewriting() -> None:
    assert "do not rewrite" in DELTA_SUFFIX.lower()


def test_the_consolidation_demands_identifiers() -> None:
    assert "identifier" in CONSOLIDATION_PROMPT.lower()


def test_the_consolidation_protects_an_unchanged_file() -> None:
    assert "leave the file unchanged" in CONSOLIDATION_PROMPT.lower()


def test_injection_is_framed_as_a_record_not_an_instruction() -> None:
    """An identity or task claim read as an order is how things get deleted."""
    block = injection_block("## Objective\nship the redesign\n")

    assert "not instructions" in block.lower()
    assert "ship the redesign" in block


def test_injection_excludes_the_log() -> None:
    block = injection_block("## Objective\nship it\n\n## Log\n- noisy raw line\n")

    assert "ship it" in block
    assert "noisy raw line" not in block


def test_injection_of_an_empty_handoff_is_harmless() -> None:
    assert "not instructions" in injection_block("").lower()
