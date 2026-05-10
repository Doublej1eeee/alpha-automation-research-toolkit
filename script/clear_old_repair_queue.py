#!/usr/bin/env python
"""Mark existing repair waiting queue as abandoned by user request.

This is intentionally a small operational tool. It does not change future repair
rules; it only stops the current historical backlog from re-entering supply and
lets sync_repair_wait_tags remove platform-visible 1REPAIR tags.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from script.high_grade_repair_engine import (  # noqa: E402
    LIFECYCLE_PATH,
    REPAIR_FAMILY_ACTIONS_PATH,
    update_lifecycle,
)


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return default
    return payload if isinstance(payload, type(default)) else default


def main() -> None:
    lifecycle = update_lifecycle()
    families = lifecycle.get("families") or {}
    now = utc_now()
    existing = load_json(REPAIR_FAMILY_ACTIONS_PATH, {"schema_version": 1, "actions": []})
    actions = existing.get("actions")
    if isinstance(actions, dict):
        rows = list(actions.values())
    elif isinstance(actions, list):
        rows = list(actions)
    else:
        rows = []
    by_family = {
        str(row.get("family_tag") or "").upper(): row
        for row in rows
        if isinstance(row, dict) and str(row.get("family_tag") or "").strip()
    }
    touched = 0
    for family_tag, entry in families.items():
        family_tag = str(family_tag or "").upper()
        if not family_tag:
            continue
        by_family[family_tag] = {
            "family_tag": family_tag,
            "decision": "exhausted",
            "reason": "user_abandoned_existing_repair_backlog",
            "terminal_reasons": ["user_abandoned_existing_repair_backlog"],
            "created_at": by_family.get(family_tag, {}).get("created_at") or now,
            "updated_at": now,
            "previous_status": entry.get("status"),
            "previous_waiting_source_count": entry.get("waiting_source_count"),
            "previous_repair_result_count": entry.get("repair_result_count"),
        }
        entry["status"] = "exhausted"
        entry["terminal_reasons"] = ["user_abandoned_existing_repair_backlog"]
        entry["repair_family_action"] = by_family[family_tag]
        entry["last_seen_at"] = now
        touched += 1
    lifecycle["families"] = families
    sources = lifecycle.get("sources") or {}
    for source_id, source in sources.items():
        source["status"] = "exhausted"
        source["terminal_reasons"] = ["user_abandoned_existing_repair_backlog"]
        source["last_seen_at"] = now
    lifecycle["sources"] = sources
    lifecycle["status_counts"] = {"exhausted": len(families)}
    lifecycle["source_status_counts"] = {"exhausted": len(sources)}
    lifecycle["cleared_existing_repair_at"] = now
    lifecycle["clear_reason"] = "user_abandoned_existing_repair_backlog"
    LIFECYCLE_PATH.parent.mkdir(parents=True, exist_ok=True)
    LIFECYCLE_PATH.write_text(json.dumps(lifecycle, ensure_ascii=False, indent=2), encoding="utf-8")

    payload = {
        "schema_version": 1,
        "updated_at": now,
        "reason": "user_abandoned_existing_repair_backlog",
        "actions": [by_family[key] for key in sorted(by_family)],
    }
    REPAIR_FAMILY_ACTIONS_PATH.parent.mkdir(parents=True, exist_ok=True)
    REPAIR_FAMILY_ACTIONS_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "cleared_families": touched,
                "source_count": len(sources),
                "actions_path": str(REPAIR_FAMILY_ACTIONS_PATH.relative_to(ROOT_DIR)),
                "lifecycle_path": str(LIFECYCLE_PATH.relative_to(ROOT_DIR)),
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
