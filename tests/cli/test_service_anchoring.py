"""The CLI service anchors prompts whenever it overrides the working directory.

Testing the helper is not enough: the bug was that the helper did not exist at
the point where it mattered. These drive the service's own decision — does this
request get anchored, and does an unbound one stay untouched.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from ductor_bot.cli.service import CLIService

WORKSPACE = "/home/ductor/.ductor/workspace"
PROJECT = "/home/ductor/IT/SalamIntlPolyclinic/wp-website"


def _service(override: str | None) -> CLIService:
    """A service with only what ``anchor`` touches.

    __init__ builds a process registry and provider factory; none of that is
    involved in deciding whether a prompt needs anchoring.
    """
    service = object.__new__(CLIService)
    service._config = SimpleNamespace(working_dir=WORKSPACE, docker_container=None)
    service._working_dir_resolver = (lambda _r: override) if override else None
    return service


def _request() -> SimpleNamespace:
    return SimpleNamespace(process_label="main", chat_id=-100, topic_id=7, transport="tg")


def test_a_bound_conversation_gets_absolute_paths() -> None:
    service = _service(PROJECT)
    out = service.anchor("Use tools/agent_tools/send_message.py now", _request())
    assert out == f"Use {WORKSPACE}/tools/agent_tools/send_message.py now"


def test_memory_instructions_cannot_resolve_into_the_project() -> None:
    """The damaging case: bot memory written into the user's git repository."""
    service = _service(PROJECT)
    out = service.anchor("update memory_system/MAINMEMORY.md silently", _request())
    assert PROJECT not in out
    assert out.startswith(f"update {WORKSPACE}/memory_system/")


def test_an_unbound_conversation_is_left_alone() -> None:
    """Relative paths are correct when cwd really is the workspace."""
    service = _service(None)
    text = "Use tools/agent_tools/send_message.py now"
    assert service.anchor(text, _request()) == text


def test_a_binding_that_equals_the_workspace_changes_nothing() -> None:
    service = _service(WORKSPACE)
    text = "Use tools/agent_tools/send_message.py"
    assert service.anchor(text, _request()) == text


def test_docker_mode_does_not_anchor() -> None:
    """Docker mode never applies the cwd override, so cwd is still the workspace."""
    service = _service(PROJECT)
    service._config.docker_container = "ductor-sandbox"
    text = "Use tools/agent_tools/send_message.py"
    assert service.anchor(text, _request()) == text


@pytest.mark.parametrize("value", [None, ""])
def test_empty_prompts_are_passed_through(value) -> None:
    assert _service(PROJECT).anchor(value, _request()) == value


def test_the_effective_working_dir_is_what_the_cli_will_use() -> None:
    assert _service(PROJECT)._effective_working_dir(_request()) == PROJECT
    assert _service(None)._effective_working_dir(_request()) == WORKSPACE
