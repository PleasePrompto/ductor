#!/usr/bin/env python3
"""Extract text from PDF and common document formats.

Supports: PDF (via pypdf), plain text, CSV, JSON, Markdown.

Usage:
    python tools/telegram_tools/read_document.py --file /path/to/document.pdf
    python tools/telegram_tools/read_document.py --file /path/to/document.pdf --max-pages 10
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

_TEXT_EXTENSIONS = frozenset({
    ".txt", ".md", ".csv", ".tsv", ".json", ".yaml", ".yml",
    ".xml", ".html", ".htm", ".log", ".ini", ".cfg", ".conf",
    ".py", ".js", ".ts", ".sh", ".bash", ".toml",
})

_MAX_TEXT_CHARS = 100_000
_TELEGRAM_FILES = Path(
    os.environ.get("DUCTOR_HOME", str(Path.home() / ".ductor"))
).expanduser() / "workspace" / "telegram_files"


def _read_pdf(path: Path, max_pages: int) -> dict:
    """Extract text from PDF using pypdf."""
    try:
        from pypdf import PdfReader  # type: ignore[import-untyped]
    except ImportError:
        return {"error": "pypdf not installed (pip install pypdf)"}

    try:
        reader = PdfReader(str(path))
    except Exception as exc:
        return {"error": f"Failed to read PDF: {exc}"}

    total_pages = len(reader.pages)
    pages_to_read = min(total_pages, max_pages)
    text_parts: list[str] = []

    for i in range(pages_to_read):
        page_text = reader.pages[i].extract_text() or ""
        text_parts.append(page_text)

    text = "\n\n---\n\n".join(text_parts)
    if len(text) > _MAX_TEXT_CHARS:
        text = text[:_MAX_TEXT_CHARS] + "\n\n[... truncated]"

    return {
        "text": text,
        "format": "pdf",
        "pages": total_pages,
        "pages_read": pages_to_read,
    }


def _read_text(path: Path) -> dict:
    """Read plain text file."""
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except Exception as exc:
        return {"error": f"Failed to read file: {exc}"}

    if len(text) > _MAX_TEXT_CHARS:
        text = text[:_MAX_TEXT_CHARS] + "\n\n[... truncated]"

    return {
        "text": text,
        "format": path.suffix.lstrip(".") or "txt",
        "size": path.stat().st_size,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract text from documents")
    parser.add_argument("--file", required=True, help="Path to document")
    parser.add_argument("--max-pages", type=int, default=50, help="Max PDF pages to read")
    args = parser.parse_args()

    path = Path(args.file).resolve()
    if not path.is_relative_to(_TELEGRAM_FILES.resolve()):
        print(json.dumps({"error": f"Path outside telegram_files: {path}"}))
        sys.exit(1)
    if not path.exists():
        print(json.dumps({"error": f"File not found: {path}"}))
        sys.exit(1)

    suffix = path.suffix.lower()

    if suffix == ".pdf":
        result = _read_pdf(path, args.max_pages)
    elif suffix in _TEXT_EXTENSIONS:
        result = _read_text(path)
    else:
        result = _read_text(path)
        if "error" not in result:
            result["note"] = f"Treated {suffix} as plain text"

    print(json.dumps(result, ensure_ascii=False, indent=2))
    if "error" in result:
        sys.exit(1)


if __name__ == "__main__":
    main()
