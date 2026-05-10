#!/usr/bin/env python
"""Build a reusable BRAIN dataset atlas from fetched dataset/datafield metadata."""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path
import re


ROOT_DIR = Path(__file__).resolve().parents[1]
DATAFIELDS_DIR = ROOT_DIR / "crawler" / "datafields"
DATASET_DIR = DATAFIELDS_DIR / "datasets"
RAW_DIR = DATAFIELDS_DIR / "raw"
ATLAS_DIR = ROOT_DIR / "result_store" / "data_catalog"

TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]+")
STOPWORDS = {
    "and", "are", "for", "from", "into", "such", "that", "the", "this",
    "with", "data", "dataset", "field", "fields", "stock", "stocks",
    "equity", "equities", "company", "companies", "provides", "includes",
    "using", "over", "under", "across", "global", "market", "markets",
}

MECHANISM_TERMS = {
    "attention_revision_congestion": [
        "analyst", "coverage", "estimate", "revision", "forecast", "target",
        "consensus", "sentiment", "news", "attention",
    ],
    "promotion_dispersion_mismatch": [
        "recommendation", "sentiment", "optimism", "dispersion", "estimate",
        "guidance", "uncertainty", "risk", "forecast",
    ],
    "instability_fragility_leadlag": [
        "surprise", "instability", "sentiment", "uncertainty", "dispersion",
        "risk", "volatility", "news", "earnings",
    ],
    "balance_sheet_pressure": [
        "balance", "sheet", "debt", "liability", "asset", "cash", "cashflow",
        "footnote", "goodwill", "deferred", "tax", "financing", "fundamental",
    ],
    "liquidity_microstructure_shock": [
        "liquidity", "volume", "turnover", "risk", "volatility", "beta",
        "correlation", "price", "reaction", "intraday", "news",
    ],
    "narrative_valuation_gap": [
        "narrative", "news", "relevance", "event", "business", "revenue",
        "valuation", "quality", "profitability", "return", "equity",
        "sentiment", "impact",
    ],
    "event_novelty_underreaction": [
        "event", "novelty", "stale", "staleness", "relevance", "news",
        "impact", "sentiment", "volume", "reaction", "price", "atr",
    ],
    "systematic_risk_regime_shift": [
        "systematic", "unsystematic", "idiosyncratic", "beta",
        "correlation", "risk", "regime", "market", "variance",
        "volatility",
    ],
    "footnote_accounting_complexity": [
        "footnote", "deferred", "tax", "accrued", "liability",
        "goodwill", "acquisition", "accounting", "asset", "assets",
    ],
    "credit_recovery_pressure": [
        "credit", "debt", "repayment", "issuance", "interest",
        "distress", "recovery", "premium", "risk", "financing",
    ],
    "mna_price_impact_absorption": [
        "mna", "merger", "acquisition", "deal", "business", "payment",
        "cash", "impact", "event", "volume", "reaction", "sentiment",
    ],
}


def tokenize(text: str) -> list[str]:
    return [
        token.lower()
        for token in TOKEN_RE.findall(text or "")
        if token.lower() not in STOPWORDS and len(token) > 2
    ]


def load_json(path: Path) -> dict:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def category_from_dataset(row: dict) -> str:
    category = row.get("category")
    if isinstance(category, dict):
        return str(category.get("id") or category.get("name") or "")
    return str(category or "")


def slim_dataset(row: dict) -> dict:
    meta = row.get("_catalog_meta") if isinstance(row.get("_catalog_meta"), dict) else {}
    search_scope = meta.get("search_scope") if isinstance(meta.get("search_scope"), dict) else {}
    return {
        "id": row.get("id"),
        "name": row.get("name"),
        "description": row.get("description"),
        "category": category_from_dataset(row),
        "region": row.get("region") or search_scope.get("region"),
        "delay": row.get("delay") if row.get("delay") is not None else search_scope.get("delay"),
        "universe": row.get("universe") or search_scope.get("universe"),
        "coverage": row.get("coverage"),
        "available_scopes": [search_scope] if search_scope else [],
    }


def collect_datasets(pattern: str) -> dict[str, dict]:
    datasets: dict[str, dict] = {}
    for path in sorted(DATASET_DIR.glob(pattern)):
        payload = load_json(path)
        meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
        for row in payload.get("results") or []:
            dataset_id = str(row.get("id") or "").strip()
            if dataset_id:
                enriched = dict(row)
                enriched["_catalog_meta"] = meta
                slim = slim_dataset(enriched)
                existing = datasets.get(dataset_id)
                if existing:
                    scopes = existing.setdefault("available_scopes", [])
                    for scope in slim.get("available_scopes") or []:
                        if scope not in scopes:
                            scopes.append(scope)
                    continue
                datasets[dataset_id] = slim
    return datasets


def collect_fields(pattern: str) -> dict[str, list[dict]]:
    fields_by_dataset: dict[str, list[dict]] = defaultdict(list)
    seen_by_dataset: dict[str, set[str]] = defaultdict(set)
    for path in sorted(RAW_DIR.glob(pattern)):
        payload = load_json(path)
        meta = payload.get("meta") if isinstance(payload.get("meta"), dict) else {}
        search_scope = meta.get("search_scope") if isinstance(meta.get("search_scope"), dict) else {}
        for row in payload.get("results") or []:
            dataset = row.get("dataset") or {}
            dataset_id = str(dataset.get("id") or "").strip()
            field_id = str(row.get("id") or "").strip()
            if dataset_id and field_id and field_id not in seen_by_dataset[dataset_id]:
                enriched = dict(row)
                enriched["_catalog_scope"] = search_scope
                fields_by_dataset[dataset_id].append(enriched)
                seen_by_dataset[dataset_id].add(field_id)
    return fields_by_dataset


def score_mechanisms(dataset: dict, fields: list[dict]) -> list[dict]:
    text_parts = [
        str(dataset.get("id") or ""),
        str(dataset.get("name") or ""),
        str(dataset.get("description") or ""),
        str(dataset.get("category") or ""),
    ]
    for field in fields[:400]:
        text_parts.extend(
            [
                str(field.get("id") or ""),
                str(field.get("description") or ""),
                str(field.get("type") or ""),
            ]
        )
    tokens = Counter(tokenize(" ".join(text_parts)))
    scored = []
    for mechanism, terms in MECHANISM_TERMS.items():
        score = sum(tokens.get(term, 0) for term in terms)
        if score:
            scored.append({"mechanism_id": mechanism, "score": score})
    return sorted(scored, key=lambda item: (-item["score"], item["mechanism_id"]))


def safe_float(value, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def median(values: list[float]) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


def dataset_exploration_score(
    dataset: dict,
    field_count: int,
    mean_field_coverage: float | None,
    median_alpha_count: float | None,
    median_user_count: float | None,
    mechanism_matches: list[dict],
) -> float:
    dataset_coverage = safe_float(dataset.get("coverage"))
    coverage = mean_field_coverage if mean_field_coverage is not None else dataset_coverage
    coverage_score = min(max(coverage, 0.0), 1.0) * 3.0
    breadth_score = min(field_count / 400.0, 2.0)
    mechanism_score = min(len(mechanism_matches) * 0.35, 1.4)
    alpha_penalty = min(1.2, safe_float(median_alpha_count) / 2500.0)
    user_penalty = min(1.0, safe_float(median_user_count) / 800.0)
    return round(coverage_score + breadth_score + mechanism_score - alpha_penalty - user_penalty, 6)


def dataset_markdown_summary(dataset: dict, fields: list[dict], mechanism_matches: list[dict]) -> str:
    example_fields = ", ".join(str(field.get("id") or "") for field in fields[:8] if field.get("id"))
    mechanisms = ", ".join(str(item.get("mechanism_id") or "") for item in mechanism_matches[:4])
    return (
        f"{dataset.get('id')} | {dataset.get('name')} | category={dataset.get('category')} | "
        f"coverage={dataset.get('coverage')} | mechanisms={mechanisms or 'none'} | "
        f"examples={example_fields}"
    )


def build_atlas(pattern: str) -> dict:
    datasets = collect_datasets(pattern)
    fields_by_dataset = collect_fields(pattern)
    rows = []
    for dataset_id, dataset in sorted(datasets.items()):
        fields = fields_by_dataset.get(dataset_id, [])
        field_tokens = Counter()
        field_types = Counter()
        field_prefixes = Counter()
        alpha_counts = []
        user_counts = []
        coverage_values = []
        field_scopes: list[dict] = []
        for field in fields:
            field_id = str(field.get("id") or "")
            field_prefixes[field_id.split("_", 1)[0]] += 1
            field_types[str(field.get("type") or "UNKNOWN")] += 1
            field_tokens.update(tokenize(f"{field_id} {field.get('description') or ''}"))
            for source, target in [
                (field.get("alphaCount"), alpha_counts),
                (field.get("userCount"), user_counts),
                (field.get("coverage"), coverage_values),
            ]:
                try:
                    if source is not None:
                        target.append(float(source))
                except Exception:
                    pass
            scope = field.get("_catalog_scope")
            if isinstance(scope, dict) and scope and scope not in field_scopes:
                field_scopes.append(scope)
        mean_field_coverage = round(sum(coverage_values) / len(coverage_values), 6) if coverage_values else None
        median_alpha_count = median(alpha_counts)
        median_user_count = median(user_counts)
        mechanism_matches = score_mechanisms(dataset, fields)[:5]
        exploration_score = dataset_exploration_score(
            dataset=dataset,
            field_count=len(fields),
            mean_field_coverage=mean_field_coverage,
            median_alpha_count=median_alpha_count,
            median_user_count=median_user_count,
            mechanism_matches=mechanism_matches,
        )
        rows.append(
            {
                **dataset,
                "field_count": len(fields),
                "field_type_counts": dict(field_types.most_common()),
                "common_field_prefixes": dict(field_prefixes.most_common(8)),
                "top_field_terms": dict(field_tokens.most_common(30)),
                "mean_field_coverage": mean_field_coverage,
                "median_alpha_count": median_alpha_count,
                "median_user_count": median_user_count,
                "available_scopes": dataset.get("available_scopes") or field_scopes,
                "exploration_score": exploration_score,
                "mechanism_matches": mechanism_matches,
                "markdown_summary": dataset_markdown_summary(dataset, fields, mechanism_matches),
                "example_fields": [
                    {
                        "id": field.get("id"),
                        "description": field.get("description"),
                        "type": field.get("type"),
                        "coverage": field.get("coverage"),
                        "alphaCount": field.get("alphaCount"),
                        "userCount": field.get("userCount"),
                    }
                    for field in fields[:8]
                ],
            }
        )
    rows.sort(key=lambda row: (-safe_float(row.get("exploration_score")), str(row.get("id") or "")))
    return {
        "schema_version": 1,
        "source_pattern": pattern,
        "dataset_count": len(rows),
        "field_count": sum(row["field_count"] for row in rows),
        "datasets": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build BRAIN dataset atlas from fetched metadata.")
    parser.add_argument("--pattern", default="accessible_catalog_20260501_*.json")
    parser.add_argument("--name", default="brain_dataset_atlas")
    args = parser.parse_args()
    atlas = build_atlas(args.pattern)
    ATLAS_DIR.mkdir(parents=True, exist_ok=True)
    output = ATLAS_DIR / f"{args.name}.json"
    output.write_text(json.dumps(atlas, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Dataset atlas: {output.relative_to(ROOT_DIR)}")
    print(f"Datasets: {atlas['dataset_count']}")
    print(f"Fields: {atlas['field_count']}")
    for row in atlas["datasets"]:
        matches = ",".join(item["mechanism_id"] for item in row.get("mechanism_matches") or [])
        print(f"- {row.get('id')} | fields={row.get('field_count')} | category={row.get('category')} | mechanisms={matches}")


if __name__ == "__main__":
    main()
