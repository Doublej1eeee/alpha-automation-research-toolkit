#!/usr/bin/env python
"""Sync platform-visible repair waiting tags for high-grade failed alphas."""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
import time


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from brain_client import (  # noqa: E402
    CREDENTIALS_FILE,
    derive_auto_grade_tag,
    fetch_alpha_details,
    iter_all_result_payloads,
    load_credentials,
    login,
    sanitize_platform_tags,
)
from script.high_grade_repair_engine import (  # noqa: E402
    REPAIR_TAG,
    is_high_grade_failed,
    repair_family_tag,
    update_lifecycle,
)


def fetch_alpha_details_with_retry(session, alpha_id: str, retries: int = 5) -> dict:
    for attempt in range(retries):
        try:
            return fetch_alpha_details(session, alpha_id)
        except RuntimeError as exc:
            message = str(exc)
            if "rate limit" not in message.lower() and "429" not in message:
                raise
            if attempt == retries - 1:
                raise
            time.sleep(min(90, 8 * (attempt + 1)))
    raise RuntimeError(f"Failed to fetch {alpha_id}")


def patch_alpha_tags_with_retry(session, alpha_id: str, tags: list[str], retries: int = 5) -> None:
    for attempt in range(retries):
        response = session.patch(f"https://api.worldquantbrain.com/alphas/{alpha_id}", json={"tags": tags})
        if response.status_code == 200:
            return
        if response.status_code != 429 and "rate limit" not in response.text.lower():
            raise RuntimeError(f"Failed to sync {alpha_id}: {response.status_code} {response.text}")
        if attempt == retries - 1:
            raise RuntimeError(f"Failed to sync {alpha_id}: {response.status_code} {response.text}")
        time.sleep(min(90, 8 * (attempt + 1)))


def is_repair_result(payload: dict) -> bool:
    return (
        "high_grade_repair" in str(payload.get("source_file") or "").lower()
        or "repair_" in str(payload.get("batch_name") or "").lower()
    )


def alpha_id_of(payload: dict) -> str:
    return str(payload.get("alpha_id") or ((payload.get("alpha_details") or {}).get("id")) or "")


def merged_platform_tags(alpha_details: dict, payload: dict, family_tag: str, repair_waiting: bool) -> list[str]:
    existing_tags = [str(tag) for tag in (alpha_details.get("tags") or []) if str(tag).strip()]
    grade_tag = derive_auto_grade_tag(alpha_details)
    merged = sanitize_platform_tags(existing_tags, grade_tag=grade_tag, keep_repair=repair_waiting)
    normalized = {str(tag).upper() for tag in merged}
    if family_tag.upper() not in normalized:
        merged.append(family_tag)
        normalized.add(family_tag.upper())
    if repair_waiting and REPAIR_TAG not in normalized:
        merged.append(REPAIR_TAG)
    return merged


def sync_tags(dry_run: bool = False, limit: int = 200, all_family: bool = False, max_updates: int = 0) -> dict:
    lifecycle = update_lifecycle()
    lifecycle_families = lifecycle.get("families") or {}
    all_payloads = [
        payload
        for payload in iter_all_result_payloads()
        if alpha_id_of(payload)
    ]
    repair_results_by_family: dict[str, list[dict]] = {}
    for payload in all_payloads:
        if is_repair_result(payload):
            repair_results_by_family.setdefault(repair_family_tag(payload), []).append(payload)

    repair_ids: set[str] = set()
    source_ids: set[str] = set()
    source_lifecycle = lifecycle.get("sources") or {}
    for payload in all_payloads:
        alpha_id = alpha_id_of(payload)
        if not is_high_grade_failed(payload) or is_repair_result(payload):
            continue
        source_ids.add(alpha_id)
        status = str((source_lifecycle.get(alpha_id) or {}).get("status") or "")
        if status in {"pending_repair", "repair_running"}:
            repair_ids.add(alpha_id)

    candidate_ids = set(repair_ids) | source_ids | {alpha_id_of(payload) for rows in repair_results_by_family.values() for payload in rows}
    candidates = all_payloads if all_family else [payload for payload in all_payloads if alpha_id_of(payload) in candidate_ids]
    candidates = candidates[:limit]
    username, password = load_credentials(CREDENTIALS_FILE)
    session = login(username, password)
    updated = 0
    skipped = 0
    errors = 0
    rows: list[dict] = []
    for payload in candidates:
        if max_updates and not dry_run and updated >= max_updates:
            break
        alpha_id = alpha_id_of(payload)
        if not alpha_id:
            skipped += 1
            continue
        family_tag = repair_family_tag(payload)
        try:
            details = fetch_alpha_details_with_retry(session, alpha_id)
        except RuntimeError as exc:
            errors += 1
            rows.append(
                {
                    "alpha_id": alpha_id,
                    "family_tag": family_tag,
                    "repair_waiting": alpha_id in repair_ids,
                    "repair_result": is_repair_result(payload),
                    "changed": False,
                    "error": str(exc),
                }
            )
            continue
        existing_tags = [str(tag) for tag in (details.get("tags") or []) if str(tag).strip()]
        merged = merged_platform_tags(details, payload, family_tag, repair_waiting=alpha_id in repair_ids)
        changed = merged != existing_tags
        rows.append(
            {
                "alpha_id": alpha_id,
                "family_tag": family_tag,
                "repair_waiting": alpha_id in repair_ids,
                "repair_result": is_repair_result(payload),
                "changed": changed,
            }
        )
        if not changed:
            skipped += 1
            continue
        if max_updates and updated >= max_updates:
            skipped += 1
            continue
        updated += 1
        if not dry_run:
            patch_alpha_tags_with_retry(session, alpha_id, merged)
            time.sleep(0.6)
    return {
        "candidate_count": len(candidates),
        "repair_waiting_count": len(repair_ids),
        "consumed_family_count": sum(1 for family in repair_results_by_family if repair_results_by_family.get(family)),
        "updated": updated,
        "skipped": skipped,
        "errors": errors,
        "dry_run": dry_run,
        "rows": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync family tags for alphas and 1REPAIR for high-grade failed repair sources.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--max-updates", type=int, default=0, help="Apply at most this many tag updates in one run; 0 means no cap.")
    parser.add_argument("--all-family", action="store_true", help="Sync family tags for all local alphas, not only repair-waiting alphas.")
    args = parser.parse_args()
    summary = sync_tags(dry_run=args.dry_run, limit=args.limit, all_family=args.all_family, max_updates=args.max_updates)
    print(summary)


if __name__ == "__main__":
    main()
