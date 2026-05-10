#!/usr/bin/env python
"""Print inventory and run-queue summary before continuous backtesting."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
INVENTORY_DIR = ROOT_DIR / "result_store" / "inventories"
SUMMARIES_DIR = ROOT_DIR / "result_store" / "summaries"


def load_summary_count(stem: str) -> int:
    path = SUMMARIES_DIR / f"{stem}.json"
    if not path.exists():
        return 0
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return 0
    return len(payload) if isinstance(payload, list) else 0


def load_batch_count(stem: str) -> int:
    path = ROOT_DIR / "result_store" / "batches" / f"{stem}.jsonl"
    if not path.exists():
        return 0
    try:
        with path.open("r", encoding="utf-8") as file:
            return sum(1 for line in file if line.strip())
    except Exception:
        return 0


def main() -> None:
    parser = argparse.ArgumentParser(description="Show current field inventory counts.")
    parser.add_argument("--inventory", action="append", default=[], help="Inventory stem to report")
    args = parser.parse_args()

    names = args.inventory or [path.stem for path in sorted(INVENTORY_DIR.glob("*.json"))]
    if not names:
        print("No inventories found.")
        return

    print("=" * 72)
    print("Continuous inventory report")
    total_selected = 0
    total_remaining_estimate = 0
    for name in names:
        path = INVENTORY_DIR / f"{name}.json"
        if not path.exists():
            print(f"{name}: missing")
            continue
        payload = json.loads(path.read_text(encoding="utf-8"))
        selected = int(payload.get("selected_record_count") or 0)
        completed = load_summary_count(name)
        attempted = load_batch_count(name)
        remaining = max(selected - completed, 0)
        total_selected += selected
        total_remaining_estimate += remaining
        print(
            f"{name} | selected={selected} | attempted={attempted} | completed={completed} | "
            f"remaining_estimate={remaining} | template={payload.get('template')}"
        )
    print("-" * 72)
    print(f"TOTAL | selected={total_selected} | remaining_estimate={total_remaining_estimate}")
    print("=" * 72)


if __name__ == "__main__":
    main()
