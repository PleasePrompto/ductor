#!/usr/bin/env python3
"""Extract keyframes and transcribe audio from video files.

Requires: ffmpeg + ffprobe (system packages).
Optional:  openai-whisper or whisper.cpp (for audio transcription).

Usage:
    python tools/telegram_tools/process_video.py --file /path/to/video.mp4
    python tools/telegram_tools/process_video.py --file /path/to/video.mp4 --max-frames 5
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

_TELEGRAM_FILES = Path(
    os.environ.get("DUCTOR_HOME", str(Path.home() / ".ductor"))
).expanduser() / "workspace" / "telegram_files"

_MAX_FRAMES_DEFAULT = 8
_MAX_FRAMES_HARD = 16
_FRAME_QUALITY = 2  # ffmpeg -q:v (1=best, 31=worst)


def _check_dependencies() -> str | None:
    """Return error message if ffmpeg/ffprobe are missing, else None."""
    missing = [cmd for cmd in ("ffmpeg", "ffprobe") if not shutil.which(cmd)]
    if missing:
        return f"Missing system dependencies: {', '.join(missing)}. Install with: sudo apt install ffmpeg"
    return None


def _probe(path: Path) -> dict | None:
    """Run ffprobe and return parsed JSON, or None on failure."""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "quiet",
                "-print_format", "json",
                "-show_format", "-show_streams",
                str(path),
            ],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None
    if result.returncode != 0:
        return None
    try:
        return json.loads(result.stdout)
    except json.JSONDecodeError:
        return None


def _parse_probe(data: dict) -> dict:
    """Extract useful metadata from ffprobe output."""
    fmt = data.get("format", {})
    duration = float(fmt.get("duration", 0))
    streams = data.get("streams", [])

    video_stream = next((s for s in streams if s.get("codec_type") == "video"), None)
    audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)

    info: dict[str, object] = {
        "duration_seconds": round(duration, 2),
        "has_video": video_stream is not None,
        "has_audio": audio_stream is not None,
        "size_bytes": int(fmt.get("size", 0)),
    }

    if video_stream:
        info["width"] = video_stream.get("width")
        info["height"] = video_stream.get("height")
        info["video_codec"] = video_stream.get("codec_name")
        nb = video_stream.get("nb_frames")
        info["total_frames"] = int(nb) if nb and nb != "N/A" else None

    if audio_stream:
        info["audio_codec"] = audio_stream.get("codec_name")

    return info


def _compute_frame_count(duration: float, max_frames: int) -> int:
    """Decide how many frames to extract based on video length."""
    if duration <= 0:
        return 1
    if duration <= 3:
        return min(max(1, int(duration)), max_frames)
    if duration <= 30:
        return min(max(2, int(duration / 4)), max_frames)
    return min(max(3, int(duration / 12)), max_frames)


def _extract_frames(path: Path, out_dir: Path, count: int, duration: float) -> list[str]:
    """Extract evenly-spaced frames as JPGs. Returns list of frame paths."""
    if duration <= 0:
        fps_filter = "select=eq(n\\,0)"
    else:
        interval = duration / max(count, 1)
        fps_filter = f"fps=1/{interval:.2f}"

    pattern = out_dir / "frame_%03d.jpg"
    with contextlib.suppress(subprocess.TimeoutExpired):
        subprocess.run(
            [
                "ffmpeg", "-v", "quiet",
                "-i", str(path),
                "-vf", fps_filter,
                "-frames:v", str(count),
                "-q:v", str(_FRAME_QUALITY),
                str(pattern),
            ],
            capture_output=True,
            timeout=60,
            check=False,
        )

    frames = sorted(out_dir.glob("frame_*.jpg"))
    return [str(f) for f in frames[:count]]


def _extract_audio(path: Path, out_dir: Path) -> Path | None:
    """Extract audio track as OGG. Returns path or None if no audio."""
    audio_path = out_dir / "audio.ogg"
    try:
        result = subprocess.run(
            [
                "ffmpeg", "-v", "quiet",
                "-i", str(path),
                "-vn", "-acodec", "libvorbis", "-q:a", "4",
                str(audio_path),
            ],
            capture_output=True,
            timeout=60,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None
    if result.returncode != 0 or not audio_path.exists() or audio_path.stat().st_size == 0:
        audio_path.unlink(missing_ok=True)
        return None
    return audio_path


def _transcribe(audio_path: Path) -> str | None:
    """Transcribe audio using whisper CLI. Returns text or None."""
    whisper_bin = shutil.which("whisper")
    if not whisper_bin:
        return None
    try:
        result = subprocess.run(
            [
                whisper_bin, str(audio_path),
                "--model", "small",
                "--output_format", "txt",
                "--output_dir", str(audio_path.parent),
            ],
            capture_output=True,
            text=True,
            timeout=300,
            check=False,
        )
    except subprocess.TimeoutExpired:
        return None
    if result.returncode != 0:
        return None

    txt_file = audio_path.parent / f"{audio_path.stem}.txt"
    if txt_file.exists():
        text = txt_file.read_text(encoding="utf-8").strip()
        txt_file.unlink(missing_ok=True)
        return text if text else None
    return None


def _cleanup(out_dir: Path, *, keep_frames: bool = False) -> None:
    """Remove temporary extraction directory."""
    if keep_frames:
        for f in out_dir.glob("audio.*"):
            f.unlink(missing_ok=True)
        return
    shutil.rmtree(out_dir, ignore_errors=True)


def process(path: Path, max_frames: int) -> dict:
    """Main processing pipeline. Returns result dict."""
    dep_error = _check_dependencies()
    if dep_error:
        return {"error": dep_error}

    probe_data = _probe(path)
    if not probe_data:
        return {"error": f"ffprobe failed to read: {path.name}"}

    meta = _parse_probe(probe_data)
    if not meta["has_video"]:
        return {"error": "No video stream found in file", "metadata": meta}

    duration = float(meta["duration_seconds"])
    frame_count = _compute_frame_count(duration, max_frames)

    out_dir = path.parent / f".{path.stem}_processing"
    out_dir.mkdir(exist_ok=True)

    result: dict[str, object] = {"file": path.name, "metadata": meta}

    frames = _extract_frames(path, out_dir, frame_count, duration)
    result["frames"] = frames
    result["frames_extracted"] = len(frames)

    if meta["has_audio"]:
        audio_path = _extract_audio(path, out_dir)
        if audio_path:
            transcript = _transcribe(audio_path)
            if transcript:
                result["transcript"] = transcript
            else:
                result["transcript_note"] = (
                    "Audio extracted but transcription failed. "
                    "Ensure openai-whisper is installed: pip install openai-whisper"
                )
        else:
            result["transcript_note"] = "Audio extraction failed"
    else:
        result["transcript_note"] = "No audio stream present"

    _cleanup(out_dir, keep_frames=len(frames) > 0)

    if not frames:
        _cleanup(out_dir, keep_frames=False)

    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract frames and transcribe audio from video")
    parser.add_argument("--file", required=True, help="Path to video file")
    parser.add_argument(
        "--max-frames",
        type=int,
        default=_MAX_FRAMES_DEFAULT,
        help=f"Maximum frames to extract (default: {_MAX_FRAMES_DEFAULT}, hard limit: {_MAX_FRAMES_HARD})",
    )
    args = parser.parse_args()

    path = Path(args.file).resolve()
    if not path.is_relative_to(_TELEGRAM_FILES.resolve()):
        print(json.dumps({"error": f"Path outside telegram_files: {path}"}))
        sys.exit(1)
    if not path.exists():
        print(json.dumps({"error": f"File not found: {path}"}))
        sys.exit(1)

    max_frames = min(args.max_frames, _MAX_FRAMES_HARD)
    result = process(path, max_frames)

    print(json.dumps(result, ensure_ascii=False, indent=2))
    if "error" in result:
        sys.exit(1)


if __name__ == "__main__":
    main()
