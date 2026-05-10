#!/usr/bin/env python
"""Rebuild paper text files from already-downloaded PDFs."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from script.fetch_us_research_resources import (  # noqa: E402
    PAPERS_PDF_DIR,
    PAPERS_TEXT_DIR,
    extract_pdf_text_basic,
)


def rebuild(limit: int, force: bool) -> dict[str, int]:
    PAPERS_TEXT_DIR.mkdir(parents=True, exist_ok=True)
    rewritten = 0
    skipped = 0
    for pdf_path in sorted(PAPERS_PDF_DIR.glob("*.pdf"), key=lambda item: item.stat().st_mtime, reverse=True)[:limit]:
        text_path = PAPERS_TEXT_DIR / f"{pdf_path.stem}.txt"
        if text_path.exists() and not force:
            skipped += 1
            continue
        text = extract_pdf_text_basic(pdf_path.read_bytes()).strip()
        if not text:
            skipped += 1
            continue
        text_path.write_text(text, encoding="utf-8")
        rewritten += 1
    return {"rewritten": rewritten, "skipped": skipped}


def main() -> None:
    parser = argparse.ArgumentParser(description="Rebuild paper text files from downloaded PDFs.")
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    print(json.dumps(rebuild(limit=args.limit, force=args.force), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
