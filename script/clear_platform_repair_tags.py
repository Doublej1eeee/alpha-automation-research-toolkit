#!/usr/bin/env python
"""Remove platform-visible 1REPAIR tags from existing alphas.

This is a one-purpose cleanup tool for user-abandoned repair backlogs. It does
not retag grades or families; it only removes 1REPAIR from platform rows that
currently have it.
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys
import time
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from brain_client import BASE_URL, CREDENTIALS_FILE, load_credentials, login  # noqa: E402


REPAIR_TAG = "1REPAIR"
PAGE_SIZE = 100


def fetch_json_with_retry(session, endpoint: str, retries: int = 5) -> dict[str, Any]:
    for attempt in range(retries):
        response = session.get(f"{BASE_URL}{endpoint}")
        if response.status_code == 200:
            return response.json()
        if response.status_code != 429 and "rate limit" not in response.text.lower():
            response.raise_for_status()
        if attempt == retries - 1:
            response.raise_for_status()
        time.sleep(min(90, 8 * (attempt + 1)))
    raise RuntimeError(f"Failed to fetch {endpoint}")


def patch_tags_with_retry(session, alpha_id: str, tags: list[str], retries: int = 5) -> None:
    for attempt in range(retries):
        response = session.patch(f"{BASE_URL}/alphas/{alpha_id}", json={"tags": tags})
        if response.status_code == 200:
            return
        if response.status_code != 429 and "rate limit" not in response.text.lower():
            raise RuntimeError(f"Failed to patch {alpha_id}: {response.status_code} {response.text}")
        if attempt == retries - 1:
            raise RuntimeError(f"Failed to patch {alpha_id}: {response.status_code} {response.text}")
        time.sleep(min(90, 8 * (attempt + 1)))


def fetch_repair_tagged(session, max_pages: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    offset = 0
    for _ in range(max_pages):
        payload = fetch_json_with_retry(
            session,
            f"/users/self/alphas?limit={PAGE_SIZE}&offset={offset}&tag={REPAIR_TAG}",
        )
        chunk = payload.get("results") or []
        if not isinstance(chunk, list) or not chunk:
            break
        rows.extend(row for row in chunk if isinstance(row, dict))
        if not payload.get("next"):
            break
        offset += PAGE_SIZE
    return rows


def alpha_tags_from_row(session, row: dict[str, Any]) -> tuple[str, list[str]]:
    alpha_id = str(row.get("id") or "")
    tags = row.get("tags")
    if alpha_id and isinstance(tags, list):
        return alpha_id, [str(tag) for tag in tags if str(tag).strip()]
    if not alpha_id:
        return "", []
    details = fetch_json_with_retry(session, f"/alphas/{alpha_id}")
    return alpha_id, [str(tag) for tag in (details.get("tags") or []) if str(tag).strip()]


def clear_repair_tags(limit: int, dry_run: bool, max_pages: int) -> dict[str, Any]:
    username, password = load_credentials(CREDENTIALS_FILE)
    session = login(username, password)
    rows = fetch_repair_tagged(session, max_pages=max_pages)
    changed: list[dict[str, Any]] = []
    skipped = 0
    for row in rows:
        if limit and len(changed) >= limit:
            break
        alpha_id, tags = alpha_tags_from_row(session, row)
        if not alpha_id:
            skipped += 1
            continue
        new_tags = [tag for tag in tags if tag.strip().upper() != REPAIR_TAG]
        if new_tags == tags:
            skipped += 1
            continue
        changed.append({"alpha_id": alpha_id, "before": tags, "after": new_tags})
        if not dry_run:
            patch_tags_with_retry(session, alpha_id, new_tags)
            time.sleep(0.4)
    return {
        "platform_repair_tagged_seen": len(rows),
        "changed": len(changed),
        "skipped": skipped,
        "dry_run": dry_run,
        "sample": changed[:12],
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Remove existing platform 1REPAIR tags.")
    parser.add_argument("--limit", type=int, default=200)
    parser.add_argument("--max-pages", type=int, default=50)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    print(clear_repair_tags(limit=args.limit, dry_run=args.dry_run, max_pages=args.max_pages))


if __name__ == "__main__":
    main()
