#!/usr/bin/env python
"""Refresh a broader BRAIN dataset knowledge base from accessible metadata.

This script follows the useful reference-project pattern:
1. discover accessible datasets across categories
2. fetch fields from those datasets
3. build atlas
4. build readable knowledge-base cards

It is an offline metadata refresh step. It does not alter backtest runtime.
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
from types import SimpleNamespace
from pathlib import Path
import sys
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from brain_client import CREDENTIALS_FILE, load_credentials, login
from script.build_dataset_atlas import ATLAS_DIR, build_atlas
from script.build_dataset_knowledge_base import build as build_dataset_knowledge_base
from script.fetch_datafields import DATASETS_DIR, fetch_datasets, fetch_fields_from_datasets, normalize_search_scope, save_outputs


DEFAULT_CATEGORIES = [
    "all",
    "fundamental",
    "analyst",
    "model",
    "news",
    "alternative",
    "sentiment",
]


def timestamp_slug() -> str:
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def split_csv(value: str) -> list[str]:
    return [item.strip() for item in str(value or "").split(",") if item.strip()]


def parse_int_csv(value: str) -> list[int]:
    parsed: list[int] = []
    for item in split_csv(value):
        try:
            parsed.append(int(item))
        except ValueError:
            continue
    return parsed


def scope_name(region: str, universe: str, delay: int) -> str:
    return f"{region.lower()}_{universe.lower()}_d{delay}"


def write_dataset_payload(name: str, datasets: list[dict[str, Any]], metadata: dict[str, Any]) -> Path:
    DATASETS_DIR.mkdir(parents=True, exist_ok=True)
    output = DATASETS_DIR / f"{name}.json"
    output.write_text(
        json.dumps(
            {
                "meta": metadata,
                "count": len(datasets),
                "results": datasets,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return output


def main() -> None:
    parser = argparse.ArgumentParser(description="Refresh broad BRAIN dataset knowledge base.")
    parser.add_argument("--instrument-type", default="EQUITY")
    parser.add_argument("--region", default="USA")
    parser.add_argument("--delays", default="1", help="Comma-separated delays inside the allowed region, e.g. 1,0.")
    parser.add_argument("--universes", default="TOP3000", help="Comma-separated universes inside the allowed region.")
    parser.add_argument("--categories", default=",".join(DEFAULT_CATEGORIES))
    parser.add_argument("--max-datasets-per-category", type=int, default=200)
    parser.add_argument("--per-dataset-limit", type=int, default=300)
    parser.add_argument("--min-coverage", type=float)
    parser.add_argument("--max-alpha-count", type=int)
    parser.add_argument("--max-user-count", type=int)
    parser.add_argument("--order")
    parser.add_argument("--name-prefix", default="accessible_catalog_full")
    parser.add_argument("--continue-on-error", action="store_true", default=True)
    args = parser.parse_args()

    categories = split_csv(args.categories)
    normalized_categories = [""] if any(item.lower() == "all" for item in categories) else []
    normalized_categories.extend(item for item in categories if item.lower() != "all")
    categories = list(dict.fromkeys(normalized_categories))
    universes = split_csv(args.universes) or ["TOP3000"]
    delays = parse_int_csv(args.delays) or [1]
    username, password = load_credentials(CREDENTIALS_FILE)
    session = login(username, password)

    stamp = timestamp_slug()
    refresh_summary: list[dict[str, Any]] = []
    total_dataset_ids: set[str] = set()
    total_field_ids: set[str] = set()
    dataset_outputs: list[Path] = []

    for universe in universes:
        for delay in delays:
            search_scope = normalize_search_scope(
                SimpleNamespace(
                    instrument_type=args.instrument_type,
                    region=args.region,
                    delay=delay,
                    universe=universe,
                )
            )
            stem = f"{args.name_prefix}_{stamp}_{scope_name(args.region, universe, delay)}"
            metadata = {
                "search_scope": search_scope,
                "categories": categories,
                "max_datasets_per_category": int(args.max_datasets_per_category),
                "per_dataset_limit": int(args.per_dataset_limit),
                "min_coverage": args.min_coverage,
                "max_alpha_count": args.max_alpha_count,
                "max_user_count": args.max_user_count,
                "order": args.order,
            }
            try:
                datasets = fetch_datasets(
                    session=session,
                    search_scope=search_scope,
                    categories=categories,
                    limit_per_category=max(1, min(50, int(args.max_datasets_per_category))),
                    min_coverage=args.min_coverage,
                    max_alpha_count=args.max_alpha_count,
                    max_user_count=args.max_user_count,
                    order=args.order,
                )
                dataset_ids = [str(row.get("id") or "") for row in datasets if row.get("id")]
                fields = fetch_fields_from_datasets(
                    session=session,
                    search_scope=search_scope,
                    dataset_ids=dataset_ids,
                    per_dataset_limit=max(1, int(args.per_dataset_limit)),
                    datasets_by_id={str(row.get("id") or ""): row for row in datasets if row.get("id")},
                    min_coverage=args.min_coverage,
                    max_alpha_count=args.max_alpha_count,
                    max_user_count=args.max_user_count,
                    order=args.order,
                )
                save_outputs(fields, stem, metadata)
                dataset_output = write_dataset_payload(stem, datasets, metadata)
                dataset_outputs.append(dataset_output)
                total_dataset_ids.update(dataset_ids)
                total_field_ids.update(str(row.get("id") or "") for row in fields if row.get("id"))
                refresh_summary.append(
                    {
                        "scope": search_scope,
                        "status": "ok",
                        "dataset_count": len(dataset_ids),
                        "field_count": len(fields),
                        "dataset_output": str(dataset_output),
                    }
                )
            except Exception as exc:
                refresh_summary.append(
                    {
                        "scope": search_scope,
                        "status": "error",
                        "error": str(exc),
                    }
                )
                if not args.continue_on_error:
                    raise

    atlas_name = "brain_dataset_atlas_extended_20260501"
    atlas = build_atlas(f"{args.name_prefix}_*.json")
    ATLAS_DIR.mkdir(parents=True, exist_ok=True)
    atlas_output = ATLAS_DIR / f"{atlas_name}.json"
    atlas["refresh_summary"] = refresh_summary
    atlas_output.write_text(json.dumps(atlas, ensure_ascii=False, indent=2), encoding="utf-8")

    kb_payload, kb_output = build_dataset_knowledge_base(atlas_output)

    print(f"Scopes attempted: {len(refresh_summary)}")
    print(f"Scopes ok: {sum(1 for row in refresh_summary if row.get('status') == 'ok')}")
    print(f"Unique datasets fetched: {len(total_dataset_ids)}")
    print(f"Unique fields fetched: {len(total_field_ids)}")
    for row in refresh_summary:
        print(f"- {row.get('scope')} status={row.get('status')} datasets={row.get('dataset_count')} fields={row.get('field_count')} error={row.get('error', '')}")
    print(f"Dataset files: {len(dataset_outputs)}")
    print(f"Atlas: {atlas_output}")
    print(f"Atlas datasets: {atlas.get('dataset_count')}")
    print(f"Atlas fields: {atlas.get('field_count')}")
    print(f"Knowledge cards: {len(kb_payload.get('cards') or [])}")
    if kb_output:
        print(f"Knowledge base: {kb_output}")


if __name__ == "__main__":
    main()
