#!/usr/bin/env python
"""Fetch WorldQuant BRAIN datafields like Data Explorer.

Examples:
    python script/fetch_datafields.py --limit 100
    python script/fetch_datafields.py --category analyst --limit 200
    python script/fetch_datafields.py --search eps --limit 50
    python script/fetch_datafields.py --dataset-id analyst4 --limit 300
"""

from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path
from time import sleep
from urllib.parse import urlencode

from brain_client import (
    BASE_URL,
    CREDENTIALS_FILE,
    load_credentials,
    login,
    log_rate_limit_event,
)


ROOT_DIR = Path(__file__).resolve().parents[1]
DATAFIELDS_DIR = ROOT_DIR / "crawler" / "datafields"
DATASETS_DIR = DATAFIELDS_DIR / "datasets"
RAW_DIR = DATAFIELDS_DIR / "raw"
CATEGORIES_DIR = DATAFIELDS_DIR / "categories"
SUMMARIES_DIR = DATAFIELDS_DIR / "summaries"

PAGE_SIZE = 50
DATAFIELD_PAGE_SLEEP_SECONDS = 1


def normalize_search_scope(args: argparse.Namespace) -> dict:
    return {
        "instrumentType": args.instrument_type,
        "region": args.region,
        "delay": args.delay,
        "universe": args.universe,
    }


def build_params(
    search_scope: dict,
    dataset_id: str | None,
    category: str | None,
    search: str | None,
    limit: int,
    offset: int,
    min_coverage: float | None = None,
    max_alpha_count: int | None = None,
    max_user_count: int | None = None,
    order: str | None = None,
) -> dict:
    params = {
        "instrumentType": search_scope["instrumentType"],
        "region": search_scope["region"],
        "delay": search_scope["delay"],
        "universe": search_scope["universe"],
        "limit": limit,
        "offset": offset,
    }
    if dataset_id:
        params["dataset.id"] = dataset_id
    if category:
        params["category"] = category
    if search:
        params["search"] = search
    # BRAIN's data-field endpoint currently rejects coverage>= query params in
    # this API path for some accounts. Keep coverage as a local post-filter in
    # submit_template_loop/build_template_field_inventory instead.
    if max_alpha_count is not None:
        params["alphaCount<="] = max_alpha_count
    if max_user_count is not None:
        params["userCount<="] = max_user_count
    if order:
        params["order"] = order
    return params


def build_dataset_params(
    search_scope: dict,
    category: str | None,
    limit: int,
    offset: int,
    min_coverage: float | None = None,
    max_alpha_count: int | None = None,
    max_user_count: int | None = None,
    order: str | None = None,
) -> dict:
    params = {
        "instrumentType": search_scope["instrumentType"],
        "region": search_scope["region"],
        "delay": search_scope["delay"],
        "universe": search_scope["universe"],
        "limit": limit,
        "offset": offset,
    }
    if category:
        params["category"] = category
    # See build_params: coverage is filtered locally after fetch.
    if max_alpha_count is not None:
        params["alphaCount<="] = max_alpha_count
    if max_user_count is not None:
        params["userCount<="] = max_user_count
    if order:
        params["order"] = order
    return params


def fetch_page(session, params: dict) -> dict:
    retry_count = 0
    while True:
        response = session.get(f"{BASE_URL}/data-fields", params=params)
        if response.status_code == 429:
            retry_count += 1
            retry_seconds = min(3 * retry_count, 30)
            log_rate_limit_event(
                kind="fetch_datafields",
                status_code=response.status_code,
                retry_seconds=retry_seconds,
                attempt=retry_count,
                context={"params": params},
            )
            print(f"Rate limited while fetching datafields, retrying in {retry_seconds}s...")
            sleep(retry_seconds)
            continue
        if response.status_code != 200:
            query = urlencode(params)
            raise RuntimeError(f"Datafield fetch failed ({response.status_code}): {query}\n{response.text}")
        return response.json()


def fetch_dataset_page(session, params: dict) -> dict:
    retry_count = 0
    while True:
        response = session.get(f"{BASE_URL}/data-sets", params=params)
        if response.status_code == 429:
            retry_count += 1
            retry_seconds = min(3 * retry_count, 30)
            log_rate_limit_event(
                kind="fetch_datasets",
                status_code=response.status_code,
                retry_seconds=retry_seconds,
                attempt=retry_count,
                context={"params": params},
            )
            print(f"Rate limited while fetching datasets, retrying in {retry_seconds}s...")
            sleep(retry_seconds)
            continue
        if response.status_code != 200:
            query = urlencode(params)
            raise RuntimeError(f"Dataset fetch failed ({response.status_code}): {query}\n{response.text}")
        return response.json()


def fetch_datasets(
    session,
    search_scope: dict,
    categories: list[str],
    limit_per_category: int = 20,
    min_coverage: float | None = None,
    max_alpha_count: int | None = None,
    max_user_count: int | None = None,
    order: str | None = None,
) -> list[dict]:
    datasets_by_id: dict[str, dict] = {}
    for category in categories:
        category_value = category or None
        first_params = build_dataset_params(
            search_scope=search_scope,
            category=category_value,
            limit=PAGE_SIZE,
            offset=0,
            min_coverage=min_coverage,
            max_alpha_count=max_alpha_count,
            max_user_count=max_user_count,
            order=order,
        )
        first_payload = fetch_dataset_page(session, first_params)
        total_count = int(first_payload.get("count") or 0)
        effective_limit = total_count if limit_per_category <= 0 else min(limit_per_category, total_count or limit_per_category)
        rows = list(first_payload.get("results") or [])
        for offset in range(PAGE_SIZE, effective_limit, PAGE_SIZE):
            params = build_dataset_params(
                search_scope=search_scope,
                category=category_value,
                limit=PAGE_SIZE,
                offset=offset,
                min_coverage=min_coverage,
                max_alpha_count=max_alpha_count,
                max_user_count=max_user_count,
                order=order,
            )
            payload = fetch_dataset_page(session, params)
            batch = payload.get("results") or []
            if not batch:
                break
            rows.extend(batch)
            sleep(DATAFIELD_PAGE_SLEEP_SECONDS)
        for row in rows[:effective_limit]:
            dataset_id = row.get("id")
            if dataset_id:
                datasets_by_id[dataset_id] = row
        label = category_value or "<all>"
        print(f"Dataset search category={label} count={len(rows[:effective_limit])} total={total_count}")
        sleep(DATAFIELD_PAGE_SLEEP_SECONDS)
    return list(datasets_by_id.values())


def fetch_fields_from_datasets(
    session,
    search_scope: dict,
    dataset_ids: list[str],
    per_dataset_limit: int,
    datasets_by_id: dict[str, dict] | None = None,
    min_coverage: float | None = None,
    max_alpha_count: int | None = None,
    max_user_count: int | None = None,
    order: str | None = None,
) -> list[dict]:
    records_by_id: dict[str, dict] = {}
    datasets_by_id = datasets_by_id or {}
    for dataset_id in dataset_ids:
        rows = fetch_datafields(
            session=session,
            search_scope=search_scope,
            dataset_id=dataset_id,
            category=None,
            search=None,
            limit=per_dataset_limit,
            min_coverage=min_coverage,
            max_alpha_count=max_alpha_count,
            max_user_count=max_user_count,
            order=order,
        )
        for row in rows:
            field_id = row.get("id")
            if field_id:
                dataset_meta = datasets_by_id.get(dataset_id) or {}
                if dataset_meta:
                    dataset = dict(row.get("dataset") or {})
                    for key in [
                        "description",
                        "category",
                        "subcategory",
                        "name",
                        "id",
                        "region",
                        "delay",
                    ]:
                        if key in dataset_meta and key not in dataset:
                            dataset[key] = dataset_meta.get(key)
                    row["dataset"] = dataset
                records_by_id[field_id] = row
        print(f"Dataset fields dataset={dataset_id} count={len(rows)} unique_total={len(records_by_id)}")
        sleep(DATAFIELD_PAGE_SLEEP_SECONDS)
    return list(records_by_id.values())


def fetch_datafields(
    session,
    search_scope: dict,
    dataset_id: str | None = None,
    category: str | None = None,
    search: str | None = None,
    limit: int | None = None,
    min_coverage: float | None = None,
    max_alpha_count: int | None = None,
    max_user_count: int | None = None,
    order: str | None = None,
) -> list[dict]:
    first_params = build_params(
        search_scope=search_scope,
        dataset_id=dataset_id,
        category=category,
        search=search,
        limit=PAGE_SIZE,
        offset=0,
        min_coverage=min_coverage,
        max_alpha_count=max_alpha_count,
        max_user_count=max_user_count,
        order=order,
    )
    first_payload = fetch_page(session, first_params)
    total_count = int(first_payload.get("count") or 0)
    effective_limit = total_count if limit is None else min(limit, total_count or limit)

    results = list(first_payload.get("results") or [])
    print(f"Datafield search total count: {total_count}")

    for offset in range(PAGE_SIZE, effective_limit, PAGE_SIZE):
        params = build_params(
            search_scope=search_scope,
            dataset_id=dataset_id,
            category=category,
            search=search,
            limit=PAGE_SIZE,
            offset=offset,
            min_coverage=min_coverage,
            max_alpha_count=max_alpha_count,
            max_user_count=max_user_count,
            order=order,
        )
        payload = fetch_page(session, params)
        batch = payload.get("results") or []
        if not batch:
            break
        results.extend(batch)
        sleep(DATAFIELD_PAGE_SLEEP_SECONDS)

    return results[:effective_limit]


def slim_record(record: dict) -> dict:
    dataset = record.get("dataset") or {}
    category = record.get("category") or {}
    subcategory = record.get("subcategory") or {}
    return {
        "id": record.get("id"),
        "description": record.get("description"),
        "dataset_id": dataset.get("id"),
        "dataset_name": dataset.get("name"),
        "dataset_description": dataset.get("description"),
        "category_id": category.get("id"),
        "category_name": category.get("name"),
        "subcategory_id": subcategory.get("id"),
        "subcategory_name": subcategory.get("name"),
        "type": record.get("type"),
        "coverage": record.get("coverage"),
        "alphaCount": record.get("alphaCount"),
        "userCount": record.get("userCount"),
    }


def build_output_stem(
    explicit_name: str | None,
    search_scope: dict,
    dataset_id: str | None,
    category: str | None,
    search: str | None,
    record_count: int,
) -> str:
    if explicit_name:
        return explicit_name
    parts = [
        category,
        dataset_id,
        search,
        str(record_count),
        search_scope["region"].lower(),
        str(search_scope["delay"]),
        search_scope["universe"].lower(),
    ]
    return "_".join(part for part in parts if part)


def save_outputs(records: list[dict], stem: str, metadata: dict) -> tuple[Path, Path, Path]:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    CATEGORIES_DIR.mkdir(parents=True, exist_ok=True)
    SUMMARIES_DIR.mkdir(parents=True, exist_ok=True)

    json_path = RAW_DIR / f"{stem}.json"
    txt_path = CATEGORIES_DIR / f"{stem}.txt"
    csv_path = SUMMARIES_DIR / f"{stem}.csv"

    payload = {
        "meta": metadata,
        "count": len(records),
        "results": records,
    }
    json_path.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    txt_path.write_text(
        "\n".join(record["id"] for record in records if record.get("id")) + "\n",
        encoding="utf-8",
    )

    rows = [slim_record(record) for record in records]
    fieldnames = sorted({key for row in rows for key in row.keys()}) if rows else ["id"]
    with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    return json_path, txt_path, csv_path


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch BRAIN datafields like Data Explorer.")
    parser.add_argument("--instrument-type", default="EQUITY")
    parser.add_argument("--region", default="USA")
    parser.add_argument("--delay", type=int, default=1)
    parser.add_argument("--universe", default="TOP3000")
    parser.add_argument("--limit", type=int, help="Maximum number of fields to fetch. Default: fetch all matched fields.")
    parser.add_argument("--dataset-id", help="Dataset id filter, e.g. analyst4")
    parser.add_argument("--category", help="Category id like analyst, fundamental, sentiment")
    parser.add_argument("--search", help="Search keyword")
    parser.add_argument("--min-coverage", type=float, help="API-side minimum coverage filter.")
    parser.add_argument("--max-alpha-count", type=int, help="API-side maximum alphaCount filter.")
    parser.add_argument("--max-user-count", type=int, help="API-side maximum userCount filter.")
    parser.add_argument(
        "--order",
        help="API order parameter, e.g. coverage, -coverage, alphaCount, -alphaCount.",
    )
    parser.add_argument("--name", help="Output stem override")
    parser.add_argument(
        "--discover-datasets",
        action="store_true",
        help="Discover datasets first, then fetch fields from selected datasets.",
    )
    parser.add_argument(
        "--dataset-categories",
        default="fundamental,analyst,model,news,alternative,sentiment",
        help="Comma-separated categories for --discover-datasets.",
    )
    parser.add_argument("--max-datasets", type=int, default=10)
    parser.add_argument("--per-dataset-limit", type=int, default=250)
    args = parser.parse_args()

    if not any([args.dataset_id, args.category, args.search]):
        print("No dataset/category/search filter provided. Fetching the entire search scope can be very large.")

    username, password = load_credentials(CREDENTIALS_FILE)
    session = login(username, password)
    search_scope = normalize_search_scope(args)
    if args.discover_datasets:
        categories = [item.strip() for item in args.dataset_categories.split(",") if item.strip()]
        datasets = fetch_datasets(
            session=session,
            search_scope=search_scope,
            categories=categories,
            limit_per_category=max(args.max_datasets, 1),
            min_coverage=args.min_coverage,
            max_alpha_count=args.max_alpha_count,
            max_user_count=args.max_user_count,
            order=args.order,
        )
        dataset_ids = [row["id"] for row in datasets if row.get("id")][: args.max_datasets]
        records = fetch_fields_from_datasets(
            session=session,
            search_scope=search_scope,
            dataset_ids=dataset_ids,
            per_dataset_limit=args.per_dataset_limit,
            datasets_by_id={row.get("id"): row for row in datasets if row.get("id")},
            min_coverage=args.min_coverage,
            max_alpha_count=args.max_alpha_count,
            max_user_count=args.max_user_count,
            order=args.order,
        )
        DATASETS_DIR.mkdir(parents=True, exist_ok=True)
        dataset_path = DATASETS_DIR / f"{args.name or 'discovered_datasets'}.json"
        dataset_path.write_text(
            json.dumps({"count": len(dataset_ids), "results": datasets}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    else:
        records = fetch_datafields(
            session=session,
            search_scope=search_scope,
            dataset_id=args.dataset_id,
            category=args.category,
            search=args.search,
            limit=args.limit,
            min_coverage=args.min_coverage,
            max_alpha_count=args.max_alpha_count,
            max_user_count=args.max_user_count,
            order=args.order,
        )

    metadata = {
        "search_scope": search_scope,
        "dataset_id": args.dataset_id,
        "category": args.category,
        "search": args.search,
        "limit": args.limit,
        "min_coverage": args.min_coverage,
        "max_alpha_count": args.max_alpha_count,
        "max_user_count": args.max_user_count,
        "order": args.order,
    }
    stem = build_output_stem(
        explicit_name=args.name,
        search_scope=search_scope,
        dataset_id=args.dataset_id,
        category=args.category,
        search=args.search,
        record_count=len(records),
    )
    json_path, txt_path, csv_path = save_outputs(records, stem, metadata)

    print(f"Fetched {len(records)} datafields")
    print(f"Raw JSON: {json_path.relative_to(ROOT_DIR)}")
    print(f"Field list: {txt_path.relative_to(ROOT_DIR)}")
    print(f"CSV summary: {csv_path.relative_to(ROOT_DIR)}")


if __name__ == "__main__":
    main()
