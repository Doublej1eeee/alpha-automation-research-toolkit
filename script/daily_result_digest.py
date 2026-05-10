#!/usr/bin/env python
"""Build a daily digest from batch summaries for long-running backtests."""

from __future__ import annotations

import argparse
import json
from collections import Counter
from datetime import datetime
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
SUMMARIES_DIR = ROOT_DIR / "result_store" / "summaries"
DAILY_DIR = ROOT_DIR / "result_store" / "analysis" / "daily"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Generate a daily digest from summary outputs.")
    parser.add_argument("--date", help="Digest date label, default: local today YYYY-MM-DD")
    parser.add_argument("--top", type=int, default=10, help="Top alpha rows to include")
    return parser.parse_args()


def read_summary_rows() -> list[dict]:
    rows: list[dict] = []
    for path in sorted(SUMMARIES_DIR.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(payload, list):
            continue
        for row in payload:
            if not isinstance(row, dict):
                continue
            item = dict(row)
            item["_summary_file"] = path.name
            rows.append(item)
    return rows


def sort_key(row: dict) -> tuple:
    return (
        1 if row.get("status") == "ERROR" else 0,
        row.get("failed_checks") or 0,
        row.get("pending_checks") or 0,
        -float(row.get("fitness") or -999),
        -float(row.get("sharpe") or -999),
        -float(row.get("returns") or -999),
    )


def build_digest(rows: list[dict], date_label: str, top_n: int) -> dict:
    color_counter = Counter((row.get("rule_color") or "WHITE") for row in rows)
    grade_counter = Counter((row.get("grade") or "UNKNOWN") for row in rows)
    status_counter = Counter((row.get("status") or "UNKNOWN") for row in rows)
    batch_counter = Counter(row.get("_summary_file") or "unknown" for row in rows)

    ranked = sorted(rows, key=sort_key)
    top_rows = []
    for row in ranked[:top_n]:
        top_rows.append(
            {
                "name": row.get("name"),
                "field": row.get("field"),
                "alpha_id": row.get("alpha_id"),
                "rule_color": row.get("rule_color"),
                "grade": row.get("grade"),
                "status": row.get("status"),
                "sharpe": row.get("sharpe"),
                "fitness": row.get("fitness"),
                "returns": row.get("returns"),
                "turnover": row.get("turnover"),
                "failed_checks": row.get("failed_checks"),
                "pending_checks": row.get("pending_checks"),
                "summary_file": row.get("_summary_file"),
            }
        )

    return {
        "date": date_label,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "total_rows": len(rows),
        "status_counts": dict(sorted(status_counter.items())),
        "color_counts": dict(sorted(color_counter.items())),
        "grade_counts": dict(sorted(grade_counter.items())),
        "batch_counts": dict(sorted(batch_counter.items())),
        "top_rows": top_rows,
    }


def render_markdown(digest: dict) -> str:
    lines = [
        f"# Daily Backtest Digest - {digest['date']}",
        "",
        f"- Generated At: {digest['generated_at']}",
        f"- Total Rows: {digest['total_rows']}",
        "",
        "## Status Counts",
    ]
    for key, value in digest["status_counts"].items():
        lines.append(f"- {key}: {value}")

    lines.extend(["", "## Color Counts"])
    for key, value in digest["color_counts"].items():
        lines.append(f"- {key}: {value}")

    lines.extend(["", "## Grade Counts"])
    for key, value in digest["grade_counts"].items():
        lines.append(f"- {key}: {value}")

    lines.extend(["", "## Batch Counts"])
    for key, value in digest["batch_counts"].items():
        lines.append(f"- {key}: {value}")

    lines.extend(["", "## Top Rows"])
    if not digest["top_rows"]:
        lines.append("- none")
        return "\n".join(lines) + "\n"

    for idx, row in enumerate(digest["top_rows"], start=1):
        lines.append(
            f"{idx}. {row['name']} | Field={row['field']} | Color={row['rule_color']} | "
            f"Grade={row['grade']} | Sharpe={row['sharpe']} | Fitness={row['fitness']} | "
            f"Returns={row['returns']} | Failed={row['failed_checks']} | "
            f"Pending={row['pending_checks']} | Batch={row['summary_file']} | AlphaID={row['alpha_id']}"
        )

    return "\n".join(lines) + "\n"


def main() -> None:
    args = parse_args()
    date_label = args.date or datetime.now().strftime("%Y-%m-%d")
    rows = read_summary_rows()
    digest = build_digest(rows, date_label=date_label, top_n=max(1, args.top))

    DAILY_DIR.mkdir(parents=True, exist_ok=True)
    json_path = DAILY_DIR / f"{date_label}.json"
    md_path = DAILY_DIR / f"{date_label}.md"

    json_path.write_text(json.dumps(digest, ensure_ascii=False, indent=2), encoding="utf-8")
    md_path.write_text(render_markdown(digest), encoding="utf-8")

    print(f"Daily digest rows: {len(rows)}")
    print(f"JSON: {json_path}")
    print(f"Markdown: {md_path}")


if __name__ == "__main__":
    main()
