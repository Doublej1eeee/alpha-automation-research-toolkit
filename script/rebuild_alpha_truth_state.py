#!/usr/bin/env python
"""Rebuild the authoritative alpha truth state from stored result payloads."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from brain_client import ALPHA_TRUTH_STATE_FILE, alpha_truth_record, iter_all_result_payloads, utc_now  # noqa: E402


def main() -> None:
    parser = argparse.ArgumentParser(description="Rebuild result_store/analysis/alpha_truth_state.json.")
    parser.add_argument("--limit", type=int, default=0, help="Optional max records to rebuild.")
    args = parser.parse_args()

    records: dict[str, dict] = {}
    for payload in iter_all_result_payloads():
        alpha_id = str(payload.get("alpha_id") or "").strip()
        details = payload.get("alpha_details") or {}
        if not alpha_id or not details:
            continue
        config = {
            "name": payload.get("name"),
            "category": payload.get("category"),
            "tags": payload.get("tags"),
            "color": payload.get("color"),
            "description": payload.get("description"),
            "expression": payload.get("expression"),
            "settings": payload.get("settings"),
        }
        records[alpha_id] = alpha_truth_record(
            alpha_id,
            config,
            details,
            source_file=payload.get("source_file"),
            batch_name=payload.get("batch_name"),
            storage_mode=payload.get("storage_mode") or "light",
        )
        if args.limit and len(records) >= args.limit:
            break
    payload = {
        "schema_version": 1,
        "updated_at": utc_now(),
        "source": "bulk_rebuild_from_result_payloads",
        "alphas": records,
    }
    ALPHA_TRUTH_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    ALPHA_TRUTH_STATE_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"rebuilt_alpha_truth_records={len(records)}")


if __name__ == "__main__":
    main()
