"""Single source for the ``DUCTOR_*`` environment passed to agent CLI turns.

Used by both the headless ``-p`` path (``executor.build_subprocess_env``) and
the interactive REPL path (``repl_pool._claude_env``) so the two can never
drift. Pure: it never reads ``os.environ`` (keeping the REPL's ``env -i``
allowlist model intact) — all values are passed in by the caller, which lets
docker-mode supply container paths.
"""

from __future__ import annotations

from pathlib import Path


def build_ductor_env(  # noqa: PLR0913
    *,
    agent_name: str,
    interagent_port: int,
    transport: str,
    chat_id: object,
    topic_id: object,
    ductor_home: Path,
    shared_memory_path: Path,
    transcribe_command: str = "",
    video_transcribe_command: str = "",
) -> dict[str, str]:
    """Return the ``DUCTOR_*`` env vars for an agent CLI turn."""
    env: dict[str, str] = {
        "DUCTOR_AGENT_NAME": agent_name,
        "DUCTOR_AGENT_ROLE": "main" if agent_name == "main" else "sub",
        "DUCTOR_INTERAGENT_PORT": str(interagent_port),
        "DUCTOR_TRANSPORT": transport,
        "DUCTOR_HOME": str(ductor_home),
        "DUCTOR_SHARED_MEMORY_PATH": str(shared_memory_path),
    }
    if chat_id:
        env["DUCTOR_CHAT_ID"] = str(chat_id)
    if topic_id:
        env["DUCTOR_TOPIC_ID"] = str(topic_id)
    if transcribe_command:
        env["DUCTOR_TRANSCRIBE_COMMAND"] = transcribe_command
    if video_transcribe_command:
        env["DUCTOR_VIDEO_TRANSCRIBE_COMMAND"] = video_transcribe_command
    return env
