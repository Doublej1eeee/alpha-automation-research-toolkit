#!/usr/bin/env python
"""Query lightweight result-store indexes.

This keeps result storage file-based, but exposes query patterns that are useful
once the project is running continuously on a cloud server.
"""

from __future__ import annotations

import argparse
from collections import defaultdict
import json
from pathlib import Path
from typing import Iterable


ROOT_DIR = Path(__file__).resolve().parents[1]
INDEX_DIR = ROOT_DIR / "result_store" / "index"
CATALOG_PATH = INDEX_DIR / "template_field_catalog.jsonl"


def _load_latest_rows(path: Path) -> list[dict]:
    if not path.exists():
        return []
    latest_by_alpha: dict[str, dict] = {}
    with path.open("r", encoding="utf-8") as file:
        for line in file:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except Exception:
                continue
            alpha_id = payload.get("alpha_id")
            if alpha_id:
                latest_by_alpha[alpha_id] = payload
    return list(latest_by_alpha.values())


def _passes_filters(row: dict, args: argparse.Namespace) -> bool:
    if args.batch_name and row.get("batch_name") != args.batch_name:
        return False
    if args.template_name and row.get("template_name") != args.template_name:
        return False
    if args.field and row.get("field") != args.field:
        return False
    if args.rule_color and str(row.get("rule_color") or "").upper() != args.rule_color.upper():
        return False
    if args.category and str(row.get("category") or "").upper() != args.category.upper():
        return False
    if args.status and str(row.get("status") or "").upper() != args.status.upper():
        return False
    return True


def _metric(value, fallback: float = -999999.0) -> float:
    try:
        if value is None:
            return fallback
        return float(value)
    except Exception:
        return fallback


def _print_rows(rows: list[dict], top: int) -> None:
    ranked = sorted(
        rows,
        key=lambda row: (
            -_metric(row.get("fitness")),
            -_metric(row.get("sharpe")),
            -_metric(row.get("returns")),
            _metric(row.get("turnover"), fallback=999999.0),
            row.get("alpha_id") or "",
        ),
    )
    for idx, row in enumerate(ranked[:top], start=1):
        print(
            f"{idx:02d}. alpha_id={row.get('alpha_id')} | "
            f"batch={row.get('batch_name')} | "
            f"template={row.get('template_name')} | "
            f"field={row.get('field')} | "
            f"color={row.get('rule_color')} | "
            f"fitness={row.get('fitness')} | "
            f"sharpe={row.get('sharpe')} | "
            f"returns={row.get('returns')} | "
            f"turnover={row.get('turnover')} | "
            f"status={row.get('status')}"
        )


def _group_rows(rows: Iterable[dict], group_by: str) -> list[tuple[str, dict]]:
    buckets: dict[str, list[dict]] = defaultdict(list)
    for row in rows:
        key = str(row.get(group_by) or "<none>")
        buckets[key].append(row)

    summary_rows: list[tuple[str, dict]] = []
    for key, bucket in buckets.items():
        fitness_values = [_metric(item.get("fitness"), fallback=None) for item in bucket]
        fitness_values = [v for v in fitness_values if v is not None]
        sharpe_values = [_metric(item.get("sharpe"), fallback=None) for item in bucket]
        sharpe_values = [v for v in sharpe_values if v is not None]
        summary_rows.append(
            (
                key,
                {
                    "count": len(bucket),
                    "blue_or_better": sum(
                        1
                        for item in bucket
                        if str(item.get("rule_color") or "").upper() in {"BLUE", "PURPLE"}
                    ),
                    "green_or_better": sum(
                        1
                        for item in bucket
                        if str(item.get("rule_color") or "").upper() in {"GREEN", "BLUE", "PURPLE"}
                    ),
                    "avg_fitness": round(sum(fitness_values) / len(fitness_values), 4) if fitness_values else None,
                    "avg_sharpe": round(sum(sharpe_values) / len(sharpe_values), 4) if sharpe_values else None,
                    "best_fitness": max(fitness_values) if fitness_values else None,
                    "best_sharpe": max(sharpe_values) if sharpe_values else None,
                },
            )
        )

    summary_rows.sort(
        key=lambda item: (
            -(item[1].get("blue_or_better") or 0),
            -_metric(item[1].get("best_fitness")),
            -_metric(item[1].get("best_sharpe")),
            item[0],
        )
    )
    return summary_rows


def _print_groups(grouped: list[tuple[str, dict]], top: int) -> None:
    for idx, (key, stats) in enumerate(grouped[:top], start=1):
        print(
            f"{idx:02d}. {key} | "
            f"count={stats['count']} | "
            f"green_or_better={stats['green_or_better']} | "
            f"blue_or_better={stats['blue_or_better']} | "
            f"avg_fitness={stats['avg_fitness']} | "
            f"avg_sharpe={stats['avg_sharpe']} | "
            f"best_fitness={stats['best_fitness']} | "
            f"best_sharpe={stats['best_sharpe']}"
        )


def main() -> None:
    parser = argparse.ArgumentParser(description="Query result_store indexes.")
    parser.add_argument("--batch-name")
    parser.add_argument("--template-name")
    parser.add_argument("--field")
    parser.add_argument("--rule-color")
    parser.add_argument("--category")
    parser.add_argument("--status")
    parser.add_argument("--group-by", choices=["batch_name", "template_name", "field", "rule_color", "category"])
    parser.add_argument("--top", type=int, default=20)
    args = parser.parse_args()

    rows = _load_latest_rows(CATALOG_PATH)
    rows = [row for row in rows if _passes_filters(row, args)]

    print(f"matched_rows={len(rows)}")
    if not rows:
        return

    if args.group_by:
        grouped = _group_rows(rows, args.group_by)
        _print_groups(grouped, args.top)
        return

    _print_rows(rows, args.top)


if __name__ == "__main__":
    main()
