#!/usr/bin/env python
"""Backfill full texts from already-fetched research metadata."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from script.fetch_us_research_resources import (  # noqa: E402
    PAPERS_METADATA_DIR,
    PAPERS_PDF_DIR,
    PAPERS_TEXT_DIR,
    REPORT_SEC_METADATA_DIR,
    REPORT_SEC_RAW_DIR,
    build_user_agent,
    create_session,
    ensure_directories,
    load_json,
    save_pdf_and_text,
    safe_slug,
)


def text_looks_readable(text: str) -> bool:
    if len(text.strip()) < 2000:
        return False
    allowed = 0
    for ch in text[:4000]:
        if ch.isascii() and (ch.isalnum() or ch.isspace() or ch in ".,:;!?-_%/$()[]'\""):
            allowed += 1
    ratio = allowed / max(min(len(text), 4000), 1)
    return ratio >= 0.85


def backfill_papers(limit: int) -> int:
    ensure_directories()
    sess = create_session()
    count = 0
    for path in sorted(PAPERS_METADATA_DIR.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True)[:limit]:
        payload = load_json(path, {})
        source = str(payload.get("source") or "")
        source_id = str(payload.get("id") or "")
        stem = f"{source}_{safe_slug(source_id)}"
        pdf_path = PAPERS_PDF_DIR / f"{stem}.pdf"
        text_path = PAPERS_TEXT_DIR / f"{stem}.txt"
        if pdf_path.exists():
            needs_refresh = (not text_path.exists()) or text_path.stat().st_size < 2000
            if not needs_refresh and text_path.exists():
                sample = text_path.read_text(encoding="utf-8", errors="ignore")
                needs_refresh = not text_looks_readable(sample)
            if needs_refresh:
                try:
                    if save_pdf_and_text(stem, type("Resp", (), {
                        "ok": True,
                        "headers": {"Content-Type": "application/pdf"},
                        "content": pdf_path.read_bytes(),
                    })()):
                        count += 1
                except Exception:
                    pass
            continue
        pdf_url = str(payload.get("pdf_url") or "").strip()
        if not pdf_url:
            continue
        try:
            response = sess.get(pdf_url, timeout=120)
            if save_pdf_and_text(stem, response):
                count += 1
        except Exception:
            continue
    return count


def backfill_sec(limit: int) -> int:
    ensure_directories()
    sess = create_session()
    count = 0
    headers = {
        "User-Agent": build_user_agent(),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    }
    for path in sorted(REPORT_SEC_METADATA_DIR.glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True)[:limit]:
        payload = load_json(path, {})
        ticker = str(payload.get("ticker") or "unknown")
        form = str(payload.get("form") or "unknown").replace("/", "_")
        accession = str(payload.get("accession") or "").replace("-", "")
        filing_url = str(payload.get("filing_url") or "").strip()
        if not filing_url or not accession:
            continue
        raw_path = REPORT_SEC_RAW_DIR / f"{safe_slug(f'{ticker}_{form}_{accession}')}.html"
        if raw_path.exists():
            continue
        try:
            response = sess.get(filing_url, headers=headers, timeout=120)
            if response.ok and response.content:
                raw_path.write_bytes(response.content)
                count += 1
        except Exception:
            continue
    return count


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill full texts for fetched research metadata.")
    parser.add_argument("--paper-limit", type=int, default=60)
    parser.add_argument("--sec-limit", type=int, default=100)
    args = parser.parse_args()

    summary = {
        "papers_downloaded": backfill_papers(args.paper_limit),
        "sec_downloaded": backfill_sec(args.sec_limit),
    }
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
