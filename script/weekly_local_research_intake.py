#!/usr/bin/env python
"""Weekly local-only research intake.

This script is intentionally local-only. It fetches and analyzes paper/report
full text on the workstation, then deletes analyzed full text after writing a
processed ledger. Cloud machines should consume generated raw-alpha assets, not
run this crawler.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from pathlib import Path
import sys


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

try:
    from script.fetch_us_research_resources import (  # noqa: E402
        count_sec_raw_reports,
        count_usable_paper_texts,
        run_once as fetch_run_once,
    )
    from script.extract_research_alpha_families import run as extract_run  # noqa: E402
    from script.promote_research_families_to_raw_alpha import promote as promote_run  # noqa: E402
    from script.local_similarity_prescreen import build as similarity_prescreen_build  # noqa: E402
    from script.raw_family_diversity_audit import build as diversity_audit_build, write_outputs as diversity_audit_write  # noqa: E402
    IMPORT_ERROR = None
except ModuleNotFoundError as exc:
    IMPORT_ERROR = exc
    count_sec_raw_reports = None
    count_usable_paper_texts = None
    fetch_run_once = None
    extract_run = None
    promote_run = None
    similarity_prescreen_build = None
    diversity_audit_build = None
    diversity_audit_write = None


SUMMARY_DIR = ROOT_DIR / "temp" / "weekly_research_intake"


def sunday_guard(force: bool) -> None:
    if force:
        return
    if datetime.now().weekday() != 6:
        raise SystemExit("Weekly research intake only runs on Sunday. Use --force to override.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Local Sunday research intake; do not run on cloud.")
    parser.add_argument("--paper-target", type=int, default=100)
    parser.add_argument("--report-target", type=int, default=100)
    parser.add_argument("--lookback-days", type=int, default=30)
    parser.add_argument("--promotion-limit", type=int, default=80)
    parser.add_argument("--force", action="store_true", help="Run even when today is not Sunday.")
    parser.add_argument("--skip-fetch", action="store_true", help="Skip network fetch and only run downstream local pipeline.")
    parser.add_argument("--health-check", action="store_true", help="Print local intake prerequisites and exit without mutating state.")
    args = parser.parse_args()

    sunday_guard(args.force)

    if IMPORT_ERROR is not None:
        summary = {
            "root": str(ROOT_DIR),
            "local_only": True,
            "ready": False,
            "error": f"{type(IMPORT_ERROR).__name__}: {IMPORT_ERROR}",
            "message": "weekly_local_research_intake.py requires local-only research crawler modules; do not run this on cloud.",
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        if args.health_check:
            return
        raise SystemExit(2)

    before = {
        "paper_texts": count_usable_paper_texts(),
        "sec_reports": count_sec_raw_reports(),
    }
    if args.health_check:
        summary = {
            "root": str(ROOT_DIR),
            "summary_dir": str(SUMMARY_DIR),
            "started_inventory": before,
            "processed_ledger_exists": (ROOT_DIR / "memory" / "processed_research_sources.jsonl").exists(),
            "crawler_dir_exists": (ROOT_DIR / "crawler").exists(),
            "alpha_generation_dir_exists": (ROOT_DIR / "alpha_generation").exists(),
        }
        print(json.dumps(summary, ensure_ascii=False, indent=2))
        return

    paper_target = max(args.paper_target, before["paper_texts"])
    report_target = max(args.report_target, before["sec_reports"])
    if args.skip_fetch:
        fetch_summary = {
            "papers_fetched": 0,
            "paper_texts_added": 0,
            "sec_reports_added": 0,
            "reports_downloaded": 0,
            "skipped": True,
        }
    else:
        fetch_summary = fetch_run_once(
            lookback_days=args.lookback_days,
            target_paper_texts=paper_target,
            target_sec_reports=report_target,
        ).__dict__
    extraction_summary = extract_run(limit_papers=max(args.paper_target * 2, 200), limit_sec=max(args.report_target * 2, 200))
    promotion_summary = promote_run(limit=args.promotion_limit, dry_run=False)
    similarity_summary = similarity_prescreen_build(threshold=0.62)
    diversity_audit = diversity_audit_build()
    diversity_audit_write(diversity_audit, update_cluster_state=True)
    after = {
        "paper_texts": count_usable_paper_texts(),
        "sec_reports": count_sec_raw_reports(),
    }
    summary = {
        "started_inventory": before,
        "target_inventory": {
            "paper_texts": paper_target,
            "sec_reports": report_target,
        },
        "fetch_summary": fetch_summary,
        "extraction_summary": extraction_summary,
        "promotion_summary": promotion_summary,
        "similarity_summary": {
            "family_count": similarity_summary.get("family_count"),
            "similar_pair_count": similarity_summary.get("similar_pair_count"),
            "family_similarity_risk": similarity_summary.get("family_similarity_risk"),
        },
        "raw_family_diversity_audit": {
            "family_count": diversity_audit.get("family_count"),
            "status_counts": diversity_audit.get("status_counts"),
            "similar_pair_count": diversity_audit.get("similar_pair_count"),
        },
        "finished_inventory": after,
    }
    SUMMARY_DIR.mkdir(parents=True, exist_ok=True)
    out = SUMMARY_DIR / "latest_summary.json"
    out.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
