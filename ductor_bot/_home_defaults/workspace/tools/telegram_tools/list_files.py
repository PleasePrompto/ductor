#!/usr/bin/env python3
"""List received Telegram files with optional filtering.

Reads the auto-maintained _index.yaml in the telegram_files directory.

Usage:
    python tools/telegram_tools/list_files.py
    python tools/telegram_tools/list_files.py --type image
    python tools/telegram_tools/list_files.py --date 2025-01-15
    python tools/telegram_tools/list_files.py --limit 5
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None  # type: ignore[assignment]


def _get_base_dir() -> Path:
    """Resolve telegram_files directory relative to workspace/tools/telegram_tools/."""
    return Path(__file__).resolve().parent.parent.parent / "telegram_files"


def main() -> None:
    parser = argparse.ArgumentParser(description="List received Telegram files")
    parser.add_argument("--type", dest="file_type", help="Filter by MIME prefix (image, audio, video, application)")
    parser.add_argument("--date", help="Filter by date (YYYY-MM-DD)")
    parser.add_argument("--limit", type=int, default=20, help="Max results")
    args = parser.parse_args()

    if yaml is None:
        print(json.dumps({"error": "pyyaml not installed (pip install pyyaml)"}))
        sys.exit(1)

    base_dir = _get_base_dir()
    index_path = base_dir / "_index.yaml"

    if not index_path.exists():
        print(json.dumps({"files": [], "total": 0, "note": "No index found. No files received yet."}))
        return

    try:
        data = yaml.safe_load(index_path.read_text(encoding="utf-8")) or {}
    except (yaml.YAMLError, OSError) as exc:
        print(json.dumps({"error": f"Failed to parse _index.yaml: {exc}"}))
        sys.exit(1)
    tree = data.get("tree", {})

    results: list[dict] = []
    for date_str, files in sorted(tree.items(), reverse=True):
        if args.date and date_str != args.date:
            continue
        for f in files:
            if args.file_type and not f.get("type", "").startswith(args.file_type):
                continue
            results.append({
                "date": date_str,
                "name": f["name"],
                "type": f.get("type", "unknown"),
                "size": f.get("size", 0),
                "path": str(base_dir / date_str / f["name"]),
            })
            if len(results) >= args.limit:
                break
        if len(results) >= args.limit:
            break

    print(json.dumps({
        "files": results,
        "total": len(results),
        "index_total": data.get("total_files", 0),
        "last_updated": data.get("last_updated", "unknown"),
    }, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
