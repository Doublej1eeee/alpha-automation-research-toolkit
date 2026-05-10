#!/usr/bin/env python
"""Ground extracted research alpha families to BRAIN datafields."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from brain_client import CREDENTIALS_FILE, load_credentials, login  # noqa: E402
from script.fetch_datafields import fetch_datafields  # noqa: E402
from script.field_selection_engine import rank_field_records  # noqa: E402


EXTRACTION_PATH = ROOT_DIR / "temp" / "research_alpha_extraction" / "latest_extracted_families.json"
OUTPUT_DIR = ROOT_DIR / "temp" / "research_alpha_grounding"


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def normalize_search_scope() -> dict[str, Any]:
    return {
        "instrumentType": "EQUITY",
        "region": "USA",
        "delay": 1,
        "universe": "TOP3000",
    }


def query_records(session, family: dict[str, Any], limit: int) -> list[dict[str, Any]]:
    field_plan = family.get("field_plan") or {}
    search_terms = field_plan.get("search_terms") or []
    dataset_hints = field_plan.get("dataset_hints") or []
    category_hints = field_plan.get("category_hints") or []
    search_scope = normalize_search_scope()

    records_by_id: dict[str, dict[str, Any]] = {}
    for search_term in search_terms[:5]:
        for category in category_hints[:2] or [None]:
            rows = fetch_datafields(
                session=session,
                search_scope=search_scope,
                dataset_id=None,
                category=category,
                search=search_term,
                limit=min(limit, 120),
            )
            for row in rows:
                field_id = str(row.get("id") or "").strip()
                if field_id:
                    records_by_id[field_id] = row

    if dataset_hints:
        hint_text = " ".join(dataset_hints)
        rows = fetch_datafields(
            session=session,
            search_scope=search_scope,
            dataset_id=None,
            category=None,
            search=hint_text,
            limit=min(limit, 120),
        )
        for row in rows:
            field_id = str(row.get("id") or "").strip()
            if field_id:
                records_by_id[field_id] = row

    return list(records_by_id.values())


def run(limit_families: int, per_family_limit: int) -> dict[str, Any]:
    payload = load_json(EXTRACTION_PATH, {})
    accepted = payload.get("accepted") or []
    accepted = accepted[:limit_families]

    username, password = load_credentials(CREDENTIALS_FILE)
    session = login(username, password)

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    results: list[dict[str, Any]] = []
    for family in accepted:
        records = query_records(session, family, per_family_limit)
        ranked = rank_field_records(
            records=records,
            template=family["template"],
            selection_query=" ".join((family.get("field_plan") or {}).get("search_terms") or []),
            limit=20,
        )
        grounded = [
            {
                "id": item.record.get("id"),
                "description": item.record.get("description"),
                "dataset": (item.record.get("dataset") or {}).get("id"),
                "category": (item.record.get("category") or {}).get("id"),
                "score": item.score,
                "reasons": item.reasons,
            }
            for item in ranked
        ]
        result = {
            "family_key": family.get("family_key"),
            "title": family.get("title"),
            "field_plan": family.get("field_plan"),
            "template": family.get("template"),
            "grounded_fields": grounded,
        }
        results.append(result)
        out_path = OUTPUT_DIR / f"{family.get('template', {}).get('name', family.get('family_key'))}.json"
        out_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

    summary = {
        "families_processed": len(results),
        "output_dir": str(OUTPUT_DIR.relative_to(ROOT_DIR)),
    }
    (OUTPUT_DIR / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Ground extracted research families to BRAIN datafields.")
    parser.add_argument("--limit-families", type=int, default=5)
    parser.add_argument("--per-family-limit", type=int, default=150)
    args = parser.parse_args()
    summary = run(limit_families=args.limit_families, per_family_limit=args.per_family_limit)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
