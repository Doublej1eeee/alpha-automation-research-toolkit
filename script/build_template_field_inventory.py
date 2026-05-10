#!/usr/bin/env python
"""Build a large reusable field inventory for one template/job."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import yaml


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from brain_client import CREDENTIALS_FILE, load_credentials, login  # noqa: E402
from script.fetch_datafields import (  # noqa: E402
    fetch_datafields,
    fetch_datasets,
    fetch_fields_from_datasets,
    normalize_search_scope,
    slim_record,
)
from script.field_selection_engine import extract_template_context, rank_field_records, score_field_record  # noqa: E402
from script.learning_query_engine import build_enriched_query  # noqa: E402
from script.submit_template_loop import load_template, passes_filters  # noqa: E402


INVENTORY_DIR = ROOT_DIR / "result_store" / "inventories"


def compatibility_filtered_records(records: list[dict], template_context: dict) -> list[dict]:
    output: list[dict] = []
    for record in records:
        if score_field_record(record, template_context).score < 0:
            continue
        output.append(record)
    return output


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build a template field inventory for large-batch backtesting.")
    parser.add_argument("template", help="Template path")
    parser.add_argument("--name", required=True, help="Inventory output stem")
    parser.add_argument("--instrument-type", default="EQUITY")
    parser.add_argument("--region", default="USA")
    parser.add_argument("--delay", type=int, default=1)
    parser.add_argument("--universe", default="TOP3000")
    parser.add_argument("--fetch-limit", type=int, default=1200)
    parser.add_argument("--dataset-id")
    parser.add_argument("--category")
    parser.add_argument("--search")
    parser.add_argument("--min-coverage", type=float)
    parser.add_argument("--max-alpha-count", type=int)
    parser.add_argument("--max-user-count", type=int)
    parser.add_argument("--field-order")
    parser.add_argument("--selection-query")
    parser.add_argument("--selection-limit", type=int, default=400)
    parser.add_argument("--min-selection-score", type=float)
    parser.add_argument("--discover-datasets", action="store_true")
    parser.add_argument(
        "--dataset-categories",
        default="fundamental,analyst,model,news,alternative,sentiment",
    )
    parser.add_argument("--max-datasets", type=int, default=10)
    parser.add_argument("--per-dataset-limit", type=int, default=250)
    return parser.parse_args()


def build_inventory_payload(args: argparse.Namespace) -> dict:
    template_path = Path(args.template)
    if not template_path.exists():
        template_path = (ROOT_DIR / args.template).resolve()
    else:
        template_path = template_path.resolve()
    template = load_template(template_path)
    template_context = extract_template_context(template, selection_query=args.selection_query)

    username, password = load_credentials(CREDENTIALS_FILE)
    session = login(username, password)

    search_scope = normalize_search_scope(args)
    discovered_datasets: list[dict] = []
    discovered_records: list[dict] = []
    direct_records: list[dict] = []
    if args.discover_datasets:
        categories = [item.strip() for item in str(args.dataset_categories).split(",") if item.strip()]
        discovered_datasets = fetch_datasets(
            session=session,
            search_scope=search_scope,
            categories=categories,
            limit_per_category=max(args.max_datasets, 1),
            min_coverage=args.min_coverage,
            max_alpha_count=args.max_alpha_count,
            max_user_count=args.max_user_count,
            order=args.field_order or "-coverage",
        )
        dataset_ids = [row["id"] for row in discovered_datasets if row.get("id")][: args.max_datasets]
        discovered_records = fetch_fields_from_datasets(
            session=session,
            search_scope=search_scope,
            dataset_ids=dataset_ids,
            per_dataset_limit=args.per_dataset_limit,
            datasets_by_id={row.get("id"): row for row in discovered_datasets if row.get("id")},
            min_coverage=args.min_coverage,
            max_alpha_count=args.max_alpha_count,
            max_user_count=args.max_user_count,
            order=args.field_order or "-coverage",
        )
    direct_category = None if args.dataset_id else args.category
    direct_records = fetch_datafields(
        session=session,
        search_scope=search_scope,
        dataset_id=args.dataset_id,
        category=direct_category,
        search=build_enriched_query(args.search) if args.search else args.search,
        limit=args.fetch_limit,
        min_coverage=args.min_coverage,
        max_alpha_count=args.max_alpha_count,
        max_user_count=args.max_user_count,
        order=args.field_order,
    )
    merged_records: list[dict] = []
    seen_ids: set[str] = set()
    for record in [*discovered_records, *direct_records]:
        record_id = str(record.get("id") or "").strip()
        if not record_id or record_id in seen_ids:
            continue
        seen_ids.add(record_id)
        merged_records.append(record)
    raw_records = merged_records
    filtered_records = [
        record
        for record in raw_records
        if passes_filters(
            record,
            min_coverage=args.min_coverage,
            max_alpha_count=args.max_alpha_count,
            max_user_count=args.max_user_count,
        )
    ]
    compatible_records = compatibility_filtered_records(filtered_records, template_context)
    ranked_decisions = rank_field_records(
        records=compatible_records,
        template=template,
        selection_query=args.selection_query,
        min_score=args.min_selection_score,
        limit=args.selection_limit,
    )
    selected_items = [
        {
            "record": decision.record,
            "score": decision.score,
            "reasons": decision.reasons,
        }
        for decision in ranked_decisions
    ]
    fallback_target = min(
        len(compatible_records),
        max(
            int(args.selection_limit or 0),
            min(80, len(compatible_records)),
        ),
    )
    if len(selected_items) < fallback_target:
        selected_ids = {str(item["record"].get("id") or "") for item in selected_items}
        fallback_records = sorted(
            compatible_records,
            key=lambda record: (
                float(record.get("coverage") or 0),
                -(int(record.get("alphaCount") or 0)),
                -(int(record.get("userCount") or 0)),
            ),
            reverse=True,
        )
        for record in fallback_records:
            record_id = str(record.get("id") or "")
            if not record_id or record_id in selected_ids:
                continue
            compatibility_decision = score_field_record(record, template_context)
            if compatibility_decision.score < 0:
                continue
            selected_items.append(
                {
                    "record": record,
                    "score": compatibility_decision.score,
                    "reasons": [*compatibility_decision.reasons, "fallback_pool_fill"],
                }
            )
            selected_ids.add(record_id)
            if len(selected_items) >= fallback_target:
                break
    if not selected_items and compatible_records:
        emergency_target = min(len(compatible_records), max(20, min(80, int(args.selection_limit or 0) or 80)))
        emergency_records = sorted(
            compatible_records,
            key=lambda record: (
                float(record.get("coverage") or 0),
                -(int(record.get("alphaCount") or 0)),
                -(int(record.get("userCount") or 0)),
            ),
            reverse=True,
        )
        for record in emergency_records[:emergency_target]:
            selected_items.append(
                {
                    "record": record,
                    "score": 0.0,
                    "reasons": ["emergency_pool_fill_after_empty_selection"],
                }
            )
    selected_records = [item["record"] for item in selected_items]
    return {
        "inventory_name": args.name,
        "template": str(template_path.relative_to(ROOT_DIR)),
        "search_scope": search_scope,
        "dataset_id": args.dataset_id,
        "category": args.category,
        "effective_category": direct_category,
        "search": args.search,
        "selection_query": args.selection_query,
        "fetch_limit": args.fetch_limit,
        "field_order": args.field_order,
        "min_coverage": args.min_coverage,
        "max_alpha_count": args.max_alpha_count,
        "max_user_count": args.max_user_count,
        "selection_limit": args.selection_limit,
        "min_selection_score": args.min_selection_score,
        "discover_datasets": args.discover_datasets,
        "dataset_categories": args.dataset_categories,
        "max_datasets": args.max_datasets,
        "per_dataset_limit": args.per_dataset_limit,
        "discovered_dataset_count": len(discovered_datasets),
        "discovered_field_record_count": len(discovered_records),
        "direct_field_record_count": len(direct_records),
        "discovered_datasets": [
            {
                "id": row.get("id"),
                "name": row.get("name"),
                "category": (row.get("category") or {}).get("id") if isinstance(row.get("category"), dict) else row.get("category"),
            }
            for row in discovered_datasets
        ],
        "raw_record_count": len(raw_records),
        "filtered_record_count": len(filtered_records),
        "compatible_record_count": len(compatible_records),
        "selected_record_count": len(selected_records),
        "fields": [
            {
                **slim_record(item["record"]),
                "selection_score": item["score"],
                "selection_reasons": item["reasons"],
            }
            for item in selected_items
        ],
    }


def save_inventory(payload: dict) -> tuple[Path, Path]:
    INVENTORY_DIR.mkdir(parents=True, exist_ok=True)
    stem = payload["inventory_name"]
    json_path = INVENTORY_DIR / f"{stem}.json"
    txt_path = INVENTORY_DIR / f"{stem}.txt"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    txt_path.write_text(
        "\n".join(item["id"] for item in payload["fields"] if item.get("id")) + "\n",
        encoding="utf-8",
    )
    return json_path, txt_path


def main() -> None:
    args = parse_args()
    payload = build_inventory_payload(args)
    json_path, txt_path = save_inventory(payload)
    print("=" * 60)
    print("Inventory build complete")
    print("Inventory:", payload["inventory_name"])
    print("Template:", payload["template"])
    print("Raw records:", payload["raw_record_count"])
    print("Filtered records:", payload["filtered_record_count"])
    print("Selected records:", payload["selected_record_count"])
    print("JSON:", json_path)
    print("TXT:", txt_path)
    print("=" * 60)


if __name__ == "__main__":
    main()
