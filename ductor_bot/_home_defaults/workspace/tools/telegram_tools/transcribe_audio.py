#!/usr/bin/env python3
"""Transcribe audio/voice files to text.

Strategies (tried in order):
1. OpenAI Whisper API (requires OPENAI_API_KEY)
2. Local `whisper` CLI (Python whisper package)
3. Local `whisper-cli` (whisper.cpp)

Usage:
    python tools/telegram_tools/transcribe_audio.py --file /path/to/audio.ogg
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

_TELEGRAM_FILES = Path(
    os.environ.get("DUCTOR_HOME", str(Path.home() / ".ductor"))
).expanduser() / "workspace" / "telegram_files"


def _transcribe_openai(path: Path) -> dict:
    """Transcribe using OpenAI Whisper API."""
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        return {"error": "OPENAI_API_KEY not set"}

    try:
        from openai import OpenAI  # type: ignore[import-untyped]
    except ImportError:
        return {"error": "openai package not installed (pip install openai)"}

    client = OpenAI(api_key=api_key)
    try:
        with path.open("rb") as f:
            result = client.audio.transcriptions.create(
                model="whisper-1",
                file=f,
                response_format="verbose_json",
            )
    except Exception as exc:
        return {"error": f"OpenAI API error: {exc}"}

    return {
        "transcript": result.text,
        "language": getattr(result, "language", None),
        "duration_seconds": getattr(result, "duration", None),
        "method": "openai_whisper_api",
    }


def _transcribe_local_whisper(path: Path) -> dict:
    """Transcribe using local whisper CLI (Python package)."""
    whisper_bin = shutil.which("whisper")
    if not whisper_bin:
        return {"error": "whisper CLI not found"}

    out_dir = path.parent
    try:
        result = subprocess.run(
            [whisper_bin, str(path), "--output_format", "json", "--output_dir", str(out_dir)],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"error": "whisper timed out after 300s"}

    if result.returncode != 0:
        return {"error": f"whisper failed: {result.stderr[:500]}"}

    json_out = out_dir / f"{path.stem}.json"
    if json_out.exists():
        try:
            data = json.loads(json_out.read_text())
        except (json.JSONDecodeError, OSError):
            json_out.unlink(missing_ok=True)
            return {"error": "Failed to parse whisper JSON output"}
        json_out.unlink(missing_ok=True)
        return {
            "transcript": data.get("text", ""),
            "language": data.get("language"),
            "method": "local_whisper",
        }

    return {"transcript": result.stdout.strip(), "method": "local_whisper"}


def _transcribe_whisper_cpp(path: Path) -> dict:
    """Transcribe using whisper.cpp CLI."""
    whisper_cli = shutil.which("whisper-cli")
    if not whisper_cli:
        return {"error": "whisper-cli not found"}

    try:
        result = subprocess.run(
            [whisper_cli, "-f", str(path), "--no-timestamps"],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return {"error": "whisper-cli timed out after 300s"}

    if result.returncode != 0:
        return {"error": f"whisper-cli failed: {result.stderr[:500]}"}

    return {"transcript": result.stdout.strip(), "method": "whisper_cpp"}


def main() -> None:
    parser = argparse.ArgumentParser(description="Transcribe audio/voice to text")
    parser.add_argument("--file", required=True, help="Path to audio file")
    args = parser.parse_args()

    path = Path(args.file).resolve()
    if not path.is_relative_to(_TELEGRAM_FILES.resolve()):
        print(json.dumps({"error": f"Path outside telegram_files: {path}"}))
        sys.exit(1)
    if not path.exists():
        print(json.dumps({"error": f"File not found: {path}"}))
        sys.exit(1)

    strategies = [_transcribe_openai, _transcribe_local_whisper, _transcribe_whisper_cpp]
    errors: list[str] = []

    for strategy in strategies:
        result = strategy(path)
        if "transcript" in result:
            print(json.dumps(result, ensure_ascii=False, indent=2))
            return
        errors.append(result.get("error", "unknown error"))

    print(json.dumps({
        "error": "All transcription methods failed",
        "details": errors,
        "hint": "Install openai (pip install openai) and set OPENAI_API_KEY, "
        "or install whisper locally (pip install openai-whisper)",
    }, ensure_ascii=False, indent=2))
    sys.exit(1)


if __name__ == "__main__":
    main()
