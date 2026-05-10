#!/usr/bin/env python
"""Fetch datafields, apply a template in memory, and submit alphas in a loop.

This keeps the fast template-replacement workflow while preserving the current
submission pipeline:
- simulation submit
- progress polling
- /check preview retry
- platform properties sync
- auto color sync
- result persistence
- experience note persistence

It does not create one YAML file per alpha unless you explicitly use the older
generation scripts.
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Mapping

import yaml


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from brain_client import (  # noqa: E402
    CREDENTIALS_FILE,
    build_alpha_fingerprint,
    load_credentials,
    login,
    submit_alpha_config,
)
from script.batch_runtime import (  # noqa: E402
    MANIFEST_DIR,
    SUMMARIES_DIR,
    build_row,
    display_path,
    load_existing_attempted_fingerprints,
    load_existing_rows,
    load_tested_fingerprints,
    print_leaderboard,
    save_outputs,
    update_manifest_progress,
    utc_now,
)
from script.fetch_datafields import fetch_datafields, normalize_search_scope, slim_record  # noqa: E402
from script.field_selection_engine import rank_field_records  # noqa: E402
from script.learning_query_engine import build_enriched_query  # noqa: E402
from script.template_validator import validate_template_payload  # noqa: E402


TEMPLATES_DIR = ROOT_DIR / "alpha_generation" / "templates"
THREAD_LOCAL = threading.local()
FIELD_PLACEHOLDER_RE = re.compile(r"\{\{(FIELD(?:_[A-Z0-9]+)?)(?:_SLUG)?\}\}")


def slugify(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]+", "_", value).strip("_").lower() or "field"


def load_template(path: Path) -> dict:
    with path.open("r", encoding="utf-8") as f:
        data = yaml.safe_load(f)
    if not isinstance(data, dict):
        raise ValueError(f"Template must be a YAML mapping: {path}")
    return data


def extract_field_slots(obj) -> list[str]:
    seen: set[str] = set()

    def visit(value) -> None:
        if isinstance(value, str):
            for match in FIELD_PLACEHOLDER_RE.finditer(value):
                slot = match.group(1)
                if slot.endswith("_SLUG"):
                    slot = slot[:-5]
                seen.add(slot)
            return
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if isinstance(value, dict):
            for key in ["name", "expression", "description"]:
                if key in value:
                    visit(value.get(key))
            field_selection = value.get("field_selection")
            if isinstance(field_selection, dict):
                for item in field_selection.values():
                    visit(item)

    visit(obj)
    if not seen and isinstance(obj, dict):
        expression = str(obj.get("expression") or "")
        if "{{FIELD}}" in expression:
            seen.add("FIELD")
    return sorted(seen)


def replace_field_map(obj, field_map: Mapping[str, str]):
    slug_map = {f"{slot}_SLUG": slugify(field) for slot, field in field_map.items()}

    if isinstance(obj, str):
        updated = obj
        for slot, value in field_map.items():
            updated = updated.replace(f"{{{{{slot}}}}}", value)
        for slot, value in slug_map.items():
            updated = updated.replace(f"{{{{{slot}}}}}", value)
        return updated
    if isinstance(obj, list):
        return [replace_field_map(item, field_map) for item in obj]
    if isinstance(obj, dict):
        return {
            key: replace_field_map(value, field_map)
            for key, value in obj.items()
        }
    return obj


def replace_placeholders(obj, field: str, field_slug: str):
    return replace_field_map(obj, {"FIELD": field})


def passes_filters(
    record: dict,
    min_coverage: float | None,
    max_alpha_count: int | None,
    max_user_count: int | None,
) -> bool:
    coverage = record.get("coverage")
    alpha_count = record.get("alphaCount")
    user_count = record.get("userCount")

    if min_coverage is not None and coverage is not None and coverage < min_coverage:
        return False
    if max_alpha_count is not None and alpha_count is not None and alpha_count > max_alpha_count:
        return False
    if max_user_count is not None and user_count is not None and user_count > max_user_count:
        return False
    return True


def build_output_stem(args: argparse.Namespace, template_path: Path, record_count: int) -> str:
    if args.name:
        return args.name

    parts = [
        template_path.stem,
        args.category,
        args.dataset_id,
        args.search,
        str(record_count),
        args.region.lower(),
        str(args.delay),
        args.universe.lower(),
    ]
    return "_".join(part for part in parts if part)


def build_configs(
    template: dict,
    records: list[dict],
    rerun: bool,
    existing_attempted_fingerprints: set[str] | None = None,
) -> tuple[list[dict], list[dict]]:
    tested = load_tested_fingerprints() if not rerun else set()
    attempted = existing_attempted_fingerprints or set()
    pending_configs: list[dict] = []
    skipped_records: list[dict] = []

    for record in records:
        field = record["id"]
        field_slug = slugify(field)
        config = replace_placeholders(template, field, field_slug)
        fingerprint = build_alpha_fingerprint(config)
        if fingerprint in tested or fingerprint in attempted:
            skipped_records.append(record)
            continue
        pending_configs.append(
            {
                "field": field,
                "field_slug": field_slug,
                "record": record,
                "config": config,
                "fingerprint": fingerprint,
            }
        )

    return pending_configs, skipped_records

def submit_generated_config(
    item: dict,
    template_name: str,
    batch_name: str,
    check_retries: int,
    check_retry_delay_seconds: int,
    sync_platform_properties: bool,
    sync_platform_color: bool,
) -> dict:
    if not hasattr(THREAD_LOCAL, "session"):
        username, password = load_credentials(CREDENTIALS_FILE)
        THREAD_LOCAL.session = login(username, password)

    session = THREAD_LOCAL.session
    field = item["field"]
    source_label = f"{template_name}:{field}"
    source_file = f"<memory:{template_name}:{field}>"
    return submit_alpha_config(
        session=session,
        config=item["config"],
        source_label=source_label,
        source_file=source_file,
        fallback_name=item["field_slug"],
        check_retries=check_retries,
        check_retry_delay_seconds=check_retry_delay_seconds,
        sync_platform_properties=sync_platform_properties,
        sync_platform_color=sync_platform_color,
        batch_name=batch_name,
        storage_mode="light",
    )


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Fetch datafields, apply a template in memory, and submit them in a loop."
    )
    parser.add_argument("template", help="Template file under alpha_generation/templates or full path")
    parser.add_argument("--field-file", help="Optional txt file with one field id per line")
    parser.add_argument("--instrument-type", default="EQUITY")
    parser.add_argument("--region", default="USA")
    parser.add_argument("--delay", type=int, default=1)
    parser.add_argument("--universe", default="TOP3000")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--dataset-id")
    parser.add_argument("--category")
    parser.add_argument("--search")
    parser.add_argument("--min-coverage", type=float)
    parser.add_argument("--max-alpha-count", type=int)
    parser.add_argument("--max-user-count", type=int)
    parser.add_argument(
        "--selection-query",
        help="Optional extra semantic hint for field selection, e.g. 'earnings revision momentum'",
    )
    parser.add_argument(
        "--selection-limit",
        type=int,
        help="Keep only the top-ranked fields after field selection scoring",
    )
    parser.add_argument(
        "--min-selection-score",
        type=float,
        help="Drop fields below this selection score",
    )
    parser.add_argument("--max-workers", type=int, default=3)
    parser.add_argument("--top", type=int, default=20)
    parser.add_argument("--name", help="Summary / manifest stem override")
    parser.add_argument("--rerun", action="store_true", help="Do not skip fingerprints already tested locally")
    parser.add_argument("--resume", action="store_true", help="Resume the same named batch and skip items already recorded in its summary")
    parser.add_argument("--dry-run", action="store_true", help="Only build configs and manifest, do not submit")
    parser.add_argument("--fast-local-only", action="store_true", help="Skip platform properties/color sync for speed")
    parser.add_argument(
        "--check-retries",
        type=int,
        default=0,
        help="Extra /check retries after simulation. Default: 0 for fast large-batch runs.",
    )
    parser.add_argument("--check-retry-delay", type=int, default=8)
    args = parser.parse_args()

    template_path = Path(args.template)
    if not template_path.exists():
        template_path = TEMPLATES_DIR / args.template
    if not template_path.exists():
        raise FileNotFoundError(f"Template not found: {args.template}")
    template_path = template_path.resolve()

    template = load_template(template_path)
    validation = validate_template_payload(template_path, template)
    if not validation.valid:
        message = "\n".join(validation.errors)
        raise ValueError(f"Template validation failed for {template_path}:\n{message}")
    for warning in validation.warnings:
        print(f"TEMPLATE WARNING | {warning}")

    stem = build_output_stem(args, template_path, args.limit)
    existing_rows = load_existing_rows(stem) if args.resume else []
    existing_attempted_fingerprints = load_existing_attempted_fingerprints(stem) if args.resume else set()

    ranked_decisions = []

    if args.field_file:
        field_file = Path(args.field_file)
        if not field_file.exists():
            raise FileNotFoundError(f"Field file not found: {field_file}")
        field_file = field_file.resolve()
        field_ids = [
            line.strip()
            for line in field_file.read_text(encoding="utf-8").splitlines()
            if line.strip() and not line.strip().startswith("#")
        ]
        records = [{"id": field} for field in field_ids]
        search_scope = {
            "instrumentType": args.instrument_type,
            "region": args.region,
            "delay": args.delay,
            "universe": args.universe,
        }
    else:
        username, password = load_credentials(CREDENTIALS_FILE)
        session = login(username, password)
        search_scope = normalize_search_scope(args)
        raw_records = fetch_datafields(
            session=session,
            search_scope=search_scope,
            dataset_id=args.dataset_id,
            category=args.category,
            search=build_enriched_query(args.search) if args.search else args.search,
            limit=args.limit,
        )
        records = [
            record
            for record in raw_records
            if passes_filters(
                record,
                min_coverage=args.min_coverage,
                max_alpha_count=args.max_alpha_count,
                max_user_count=args.max_user_count,
            )
        ]

        ranked_decisions = rank_field_records(
            records=records,
            template=template,
            selection_query=args.selection_query,
            min_score=args.min_selection_score,
            limit=args.selection_limit,
        )
        if ranked_decisions:
            records = [decision.record for decision in ranked_decisions]
        else:
            records = []

    score_lookup = {
        (decision.record.get("id") or ""): {
            "selection_score": decision.score,
            "selection_reasons": decision.reasons,
        }
        for decision in ranked_decisions
    }

    pending_configs, skipped_records = build_configs(
        template=template,
        records=records,
        rerun=args.rerun,
        existing_attempted_fingerprints=existing_attempted_fingerprints,
    )

    selected_count = len(records)
    print("=" * 60)
    print("Template loop inventory summary")
    print("Template:", template_path.name)
    print("Batch name:", stem)
    if args.field_file:
        print("Field source:", Path(args.field_file).resolve())
        print("Fields loaded:", selected_count)
    else:
        print("Requested fetch limit:", args.limit)
        print("Fetched raw records:", len(raw_records))
        print("Filter-passed records:", len(records) if not ranked_decisions else len([
            record for record in raw_records
            if passes_filters(
                record,
                min_coverage=args.min_coverage,
                max_alpha_count=args.max_alpha_count,
                max_user_count=args.max_user_count,
            )
        ]))
        print("Selected records:", selected_count)
        print("Selection limit:", args.selection_limit)
        print("Min selection score:", args.min_selection_score)
    print("Existing recorded rows:", len(existing_rows))
    print("Already attempted / skipped:", len(skipped_records))
    print("Pending new configs:", len(pending_configs))
    print("Max workers:", max(1, args.max_workers))
    print("=" * 60)

    manifest = {
        "template": str(template_path.relative_to(ROOT_DIR)),
        "search_scope": search_scope,
        "dataset_id": args.dataset_id,
        "category": args.category,
        "search": args.search,
        "selection_query": args.selection_query,
        "selection_limit": args.selection_limit,
        "min_selection_score": args.min_selection_score,
        "requested_limit": args.limit,
        "record_count": len(records),
        "submit_count": len(pending_configs),
        "skipped_count": len(skipped_records),
        "existing_row_count": len(existing_rows),
        "resume": args.resume,
        "dry_run": args.dry_run,
        "fast_local_only": args.fast_local_only,
        "started_at": utc_now(),
        "fields": [
            (
                {
                    **slim_record(record),
                    **score_lookup.get(record.get("id") or "", {}),
                }
                if len(record) > 1
                else {
                    "id": record["id"],
                    **score_lookup.get(record.get("id") or "", {}),
                }
            )
            for record in records
        ],
    }
    rows: list[dict] = list(existing_rows)
    manifest = update_manifest_progress(
        manifest,
        rows,
        total_records=len(records),
        skipped_records=len(skipped_records),
        queued_records=len(pending_configs),
        finished=args.dry_run and not pending_configs,
    )

    if args.dry_run:
        rows.extend(
            [
            {
                "name": item["config"].get("name", item["field_slug"]),
                "field": item["field"],
                "fingerprint": item["fingerprint"],
                "alpha_id": None,
                "rule_color": "WHITE",
                "grade": None,
                "status": "DRY_RUN",
                "stage": None,
                "sharpe": None,
                "fitness": None,
                "returns": None,
                "turnover": None,
                "drawdown": None,
                "margin": None,
                "passed_checks": 0,
                "failed_checks": 0,
                "pending_checks": 0,
                "failed_check_names": "",
                "pending_check_names": "",
                "effective_check_count": 0,
                "result_path": None,
                "experience_path": None,
                "error": "",
            }
            for item in pending_configs
            ]
        )
        manifest = update_manifest_progress(
            manifest,
            rows,
            total_records=len(records),
            skipped_records=len(skipped_records),
            queued_records=len(pending_configs),
            finished=True,
        )
        manifest_path, json_path, csv_path = save_outputs(rows, manifest, stem)
        print(
            f"Dry run ready | New configs: {len(pending_configs)} | "
            f"Existing recorded: {len(existing_rows)} | Skipped: {len(skipped_records)} | "
            f"Template: {template_path.name}"
        )
        print(f"Manifest: {display_path(manifest_path, ROOT_DIR)}")
        print(f"JSON summary: {display_path(json_path, ROOT_DIR)}")
        print(f"CSV summary: {display_path(csv_path, ROOT_DIR)}")
        return

    manifest_path, json_path, csv_path = save_outputs(rows, manifest, stem)
    print(f"Checkpoint initialized: {display_path(manifest_path, ROOT_DIR)}")

    results: list[dict] = []
    with ThreadPoolExecutor(max_workers=max(1, args.max_workers)) as executor:
        future_map = {
            executor.submit(
                submit_generated_config,
                item,
                template_path.name,
                stem,
                args.check_retries,
                args.check_retry_delay,
                not args.fast_local_only,
                not args.fast_local_only,
            ): item
            for item in pending_configs
        }

        for future in as_completed(future_map):
            item = future_map[future]
            alpha_name = item["config"].get("name", item["field_slug"])
            try:
                result = future.result()
                result["field"] = item["field"]
                result["fingerprint"] = item["fingerprint"]
                results.append(result)
            except Exception as exc:
                results.append(
                    {
                        "name": alpha_name,
                        "field": item["field"],
                        "fingerprint": item["fingerprint"],
                        "error": str(exc),
                    }
                )
                print(f"ERROR | {alpha_name} | {exc}")

            rows = list(existing_rows) + [build_row(entry) for entry in results]
            manifest = update_manifest_progress(
                manifest,
                rows,
                total_records=len(records),
                skipped_records=len(skipped_records),
                queued_records=len(pending_configs),
                finished=False,
            )
            save_outputs(rows, manifest, stem)
            print(
                f"Checkpoint saved | Completed {manifest['progress']['completed_records']}/"
                f"{manifest['progress']['queued_records']} | Remaining {manifest['progress']['remaining_records']}"
            )

    rows = list(existing_rows) + [build_row(item) for item in results]
    manifest = update_manifest_progress(
        manifest,
        rows,
        total_records=len(records),
        skipped_records=len(skipped_records),
        queued_records=len(pending_configs),
        finished=True,
    )
    manifest_path, json_path, csv_path = save_outputs(rows, manifest, stem)

    print("=" * 60)
    print(
        f"Template loop completed | New submitted: {len(results)} | "
        f"Existing recorded: {len(existing_rows)} | Total rows now: {len(rows)}"
    )
    print(f"Manifest: {display_path(manifest_path, ROOT_DIR)}")
    print(f"JSON summary: {display_path(json_path, ROOT_DIR)}")
    print(f"CSV summary: {display_path(csv_path, ROOT_DIR)}")
    print_leaderboard(rows, args.top)


if __name__ == "__main__":
    main()
