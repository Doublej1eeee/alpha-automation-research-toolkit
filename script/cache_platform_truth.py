#!/usr/bin/env python
"""Cache platform truth data to local files for dashboard consumption."""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from brain_client import BASE_URL, CREDENTIALS_FILE, load_credentials, login  # noqa: E402

ANALYSIS_DIR = ROOT_DIR / "result_store" / "analysis"
PLATFORM_TRUTH_PATH = ANALYSIS_DIR / "platform_truth_cache.json"

GRADE_TAGS = ["1AVERAGE", "1GOOD", "1EXCELLENT", "1SPECTACULAR", "1REPAIR"]
PAGE_SIZE = 100


def utc_now_text() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def fetch_json(session, endpoint: str) -> dict[str, Any]:
    response = session.get(f"{BASE_URL}{endpoint}")
    response.raise_for_status()
    return response.json()


def fetch_all_tagged(session, tag: str, max_pages: int = 200) -> list[dict[str, Any]]:
    offset = 0
    rows: list[dict[str, Any]] = []
    for _ in range(max_pages):
        payload = fetch_json(session, f"/users/self/alphas?limit={PAGE_SIZE}&offset={offset}&tag={tag}")
        chunk = payload.get("results") or []
        if not isinstance(chunk, list) or not chunk:
            break
        rows.extend(chunk)
        if not payload.get("next"):
            break
        offset += PAGE_SIZE
    return rows


def grade_counts_by_status(rows: list[dict[str, Any]], tag: str) -> dict[str, int]:
    counts = {"UNSUBMITTED": 0, "ACTIVE": 0, "OTHER": 0}
    for row in rows:
        status = str(row.get("status") or "").upper()
        if status in counts:
            counts[status] += 1
        else:
            counts["OTHER"] += 1
    return {
        "tag": tag,
        "total": len(rows),
        "unsubmitted": counts["UNSUBMITTED"],
        "submitted": counts["ACTIVE"],
        "other": counts["OTHER"],
    }


def ids_by_status(rows: list[dict[str, Any]]) -> dict[str, list[str]]:
    ids = {"unsubmitted": [], "submitted": [], "other": []}
    for row in rows:
        alpha_id = str(row.get("id") or "").strip()
        if not alpha_id:
            continue
        status = str(row.get("status") or "").upper()
        if status == "UNSUBMITTED":
            ids["unsubmitted"].append(alpha_id)
        elif status == "ACTIVE":
            ids["submitted"].append(alpha_id)
        else:
            ids["other"].append(alpha_id)
    return ids


def fetch_activity_daily(session, name: str) -> dict[str, Any]:
    payload = fetch_json(session, f"/users/self/activities/{name}?limit=400&offset=0")
    rows = (((payload.get("records") or {}).get("records")) or [])
    daily = []
    for row in rows:
        if isinstance(row, dict):
            date = row.get("date")
            value = row.get("value")
        elif isinstance(row, list | tuple) and len(row) >= 2:
            date = row[0]
            value = row[1]
        else:
            continue
        daily.append({"date": date, "value": int(value or 0)})
    return {
        "summary": {
            "yesterday": payload.get("yesterday") or {},
            "current": payload.get("current") or {},
            "previous": payload.get("previous") or {},
            "ytd": payload.get("ytd") or {},
            "total": payload.get("total") or {},
        },
        "daily": daily,
    }


def build_cache() -> dict[str, Any]:
    username, password = load_credentials(CREDENTIALS_FILE)
    session = login(username, password)
    tag_payloads: dict[str, Any] = {}
    for tag in GRADE_TAGS:
        rows = fetch_all_tagged(session, tag)
        tag_payloads[tag] = {
            "counts": grade_counts_by_status(rows, tag),
            "sample_ids": [str(row.get("id") or "") for row in rows[:20]],
            "ids_by_status": ids_by_status(rows),
        }
    simulations = fetch_activity_daily(session, "simulations")
    submissions = fetch_activity_daily(session, "submissions")
    return {
        "generated_at": utc_now_text(),
        "source": "worldquant_platform_cached_on_cloud",
        "tags": tag_payloads,
        "activities": {
            "simulations": simulations,
            "submissions": submissions,
        },
    }


def main() -> None:
    ANALYSIS_DIR.mkdir(parents=True, exist_ok=True)
    payload = build_cache()
    PLATFORM_TRUTH_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({"ok": True, "path": str(PLATFORM_TRUTH_PATH), "generated_at": payload["generated_at"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
