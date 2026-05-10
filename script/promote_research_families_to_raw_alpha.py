#!/usr/bin/env python
"""Promote extracted research families into the production raw-alpha pool.

The extractor writes a catalog of mechanisms learned from local papers/reports.
This script turns validated catalog rows into structured ``ALPHA:`` seed lines
that the cloud rotation bridge already understands.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from script.raw_alpha_rotation import load_raw_alpha_pool  # noqa: E402
from script.raw_family_diversity_audit import seed_profile, similarity  # noqa: E402
from script.template_validator import validate_template_payload  # noqa: E402


CATALOG_PATH = ROOT_DIR / "result_store" / "index" / "research_alpha_family_catalog.jsonl"
RAW_ALPHA_PATH = ROOT_DIR / "alpha_generation" / "raw_alpha_ai.md"
SUMMARY_DIR = ROOT_DIR / "temp" / "research_alpha_promotion"
DEFAULT_PROMOTION_SIMILARITY_THRESHOLD = 0.85

MECHANISM_MAP = {
    "analyst": "attention_revision_congestion",
    "sentiment": "event_novelty_underreaction",
    "event": "event_novelty_underreaction",
    "fundamental": "balance_sheet_pressure",
    "risk": "systematic_risk_regime_shift",
}

MECHANISM_BY_HINT = {
    "revision": "attention_revision_congestion",
    "forecast": "attention_revision_congestion",
    "guidance": "attention_revision_congestion",
    "attention": "event_novelty_underreaction",
    "news": "event_novelty_underreaction",
    "drift": "event_novelty_underreaction",
    "stress": "credit_recovery_pressure",
    "credit": "credit_recovery_pressure",
    "default": "credit_recovery_pressure",
    "liquidity": "liquidity_microstructure_shock",
    "volatility": "liquidity_microstructure_shock",
    "quality": "balance_sheet_pressure",
    "governance": "footnote_accounting_complexity",
}

SEED_FIELDS = {
    "attention_revision_congestion": "analyst_revision",
    "event_novelty_underreaction": "news_event_attention",
    "balance_sheet_pressure": "fundamental_quality_pressure",
    "systematic_risk_regime_shift": "systematic_risk_shift",
    "credit_recovery_pressure": "credit_distress_pressure",
    "liquidity_microstructure_shock": "liquidity_volume_shock",
    "footnote_accounting_complexity": "footnote_accounting_pressure",
}

ANTI_CORRELATION = {
    "attention_revision_congestion": "plain_earnings_revision_momentum",
    "event_novelty_underreaction": "raw_news_sentiment_level",
    "balance_sheet_pressure": "standard_fundamental_quality",
    "systematic_risk_regime_shift": "price_volume_volatility",
    "credit_recovery_pressure": "plain_leverage",
    "liquidity_microstructure_shock": "earnings_revision_momentum",
    "footnote_accounting_complexity": "standard_fundamental_quality",
}

EXPRESSION_FAMILY_BY_PATTERN = [
    ("group_rank(ts_mean(ts_backfill", "smoothed_group_relative_signal"),
    ("-ts_rank", "inverse_rank_pressure"),
    ("ts_rank", "time_series_rank"),
]


@dataclass
class Promotion:
    family: str
    source: str
    mechanism: str
    data_family: str
    expression_family: str
    anti_correlation_target: str
    delay: str
    profile: str
    domain: str
    rationale: str
    fields: str
    expression: str


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def slugify(value: str, limit: int = 96) -> str:
    return re.sub(r"[^A-Za-z0-9_]+", "_", str(value or "")).strip("_").lower()[:limit] or "research_alpha"


def clean_inline(value: Any, limit: int = 420) -> str:
    text = str(value or "")
    text = re.sub(r"[\r\n|]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


def load_catalog(path: Path = CATALOG_PATH) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not path.exists():
        return rows
    with path.open("r", encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            rows.append(row)
    return rows


def existing_raw_families(path: Path = RAW_ALPHA_PATH) -> set[str]:
    return {seed.family for seed in load_raw_alpha_pool(path)}


def existing_raw_sources(path: Path = RAW_ALPHA_PATH) -> set[str]:
    return {seed.source for seed in load_raw_alpha_pool(path) if seed.source}


def choose_mechanism(row: dict[str, Any]) -> str:
    terms = [
        str(row.get("domain") or "").lower(),
        *[str(item).lower() for item in (row.get("mechanisms") or [])],
        *[str(item).lower() for item in ((row.get("field_plan") or {}).get("search_terms") or [])],
        str(row.get("title") or "").lower(),
    ]
    joined = " ".join(terms)
    for hint, mechanism in MECHANISM_BY_HINT.items():
        if hint in joined:
            return mechanism
    return MECHANISM_MAP.get(str(row.get("domain") or "").lower(), "balance_sheet_pressure")


def data_family(row: dict[str, Any], mechanism: str) -> str:
    field_plan = row.get("field_plan") or {}
    pieces = [
        *[str(item) for item in (field_plan.get("dataset_hints") or [])],
        *[str(item) for item in (field_plan.get("search_terms") or [])],
        mechanism,
    ]
    cleaned: list[str] = []
    for piece in pieces:
        token = slugify(piece, limit=32)
        if token and token not in cleaned:
            cleaned.append(token)
    return "+".join(cleaned[:8])


def expression_family(expression: str) -> str:
    lower = expression.lower()
    for pattern, name in EXPRESSION_FAMILY_BY_PATTERN:
        if pattern in lower:
            return name
    return "research_template_rank"


def seed_field_for(row: dict[str, Any], mechanism: str) -> str:
    terms = [str(item).lower() for item in ((row.get("field_plan") or {}).get("search_terms") or [])]
    for term in terms:
        if term in {"eps", "forecast", "estimate", "revision", "analyst"}:
            return "analyst_revision"
        if term in {"news", "tone", "attention", "coverage"}:
            return "news_event_attention"
        if term in {"credit", "default", "distress"}:
            return "credit_distress_pressure"
        if term in {"liquidity", "volume", "volatility"}:
            return "liquidity_volume_shock"
        if term in {"cash", "earnings", "margin", "profitability", "quality"}:
            return "fundamental_quality_pressure"
        if term in {"governance", "compensation", "ownership"}:
            return "footnote_accounting_pressure"
    return SEED_FIELDS.get(mechanism, "research_signal")


def build_expression(row: dict[str, Any], seed_field: str) -> str:
    template = row.get("template") or {}
    expression = str(template.get("expression") or "").strip()
    if "{{FIELD}}" in expression:
        expression = expression.replace("{{FIELD}}", seed_field)
    if not expression:
        expression = f"ts_rank({seed_field}, 126)"
    return expression


def family_name(row: dict[str, Any], existing: set[str]) -> str:
    source = slugify(row.get("source_id") or row.get("title") or "", limit=36)
    base = slugify(f"research_{row.get('source_type')}_{row.get('family_key')}_{source}", limit=110)
    if base not in existing:
        return base
    suffix = 2
    while f"{base}_{suffix}" in existing:
        suffix += 1
    return f"{base}_{suffix}"


def source_key(row: dict[str, Any]) -> str:
    return f"{row.get('source_type')}_{clean_inline(row.get('source_id'), 80)}"


def build_promotion(row: dict[str, Any], existing: set[str], existing_sources: set[str] | None = None) -> Promotion | None:
    if not row.get("validation_valid", False):
        return None
    if row.get("duplicate_of_existing_family", False):
        return None
    source = source_key(row)
    if existing_sources is not None and source in existing_sources:
        return None
    mechanism = choose_mechanism(row)
    seed_field = seed_field_for(row, mechanism)
    expression = build_expression(row, seed_field)
    validation = validate_template_payload(
        Path("<promotion>"),
        {
            "name": "promotion_probe",
            "type": "REGULAR",
            "category": "FUNDAMENTAL",
            "settings": {"language": "FASTEXPR"},
            "expression": expression,
        },
    )
    if not validation.valid:
        return None
    return Promotion(
        family=family_name(row, existing),
        source=source,
        mechanism=mechanism,
        data_family=data_family(row, mechanism),
        expression_family=expression_family(expression),
        anti_correlation_target=ANTI_CORRELATION.get(mechanism, "existing_raw_pool"),
        delay="D1_primary",
        profile="research_catalog_single_slot",
        domain="+".join(
            item
            for item in [
                slugify(row.get("domain"), 32),
                *[slugify(item, 32) for item in (row.get("mechanisms") or [])],
            ]
            if item
        ),
        rationale=clean_inline(f"{row.get('title')}. {row.get('parsed_idea_text')}", 520),
        fields=seed_field,
        expression=expression,
    )


def promotion_profile(item: Promotion) -> dict[str, Any]:
    class _Seed:
        family = item.family
        source = item.source
        profile = item.profile
        mechanism_id = item.mechanism
        data_family = item.data_family
        expression_family = item.expression_family
        anti_correlation_target = item.anti_correlation_target
        domain = item.domain
        fields = [field.strip() for field in item.fields.split(",") if field.strip()]
        expression = item.expression

    return seed_profile(_Seed())  # type: ignore[arg-type]


def nearest_existing_family(item: Promotion, existing_profiles: list[dict[str, Any]]) -> tuple[str, float]:
    profile = promotion_profile(item)
    best_family = ""
    best_score = 0.0
    for existing in existing_profiles:
        score = similarity(profile, existing)
        if score > best_score:
            best_family = str(existing.get("family") or "")
            best_score = score
    return best_family, round(best_score, 6)


def format_alpha_line(item: Promotion) -> str:
    payload = asdict(item)
    return "ALPHA: " + " | ".join(f"{key}={value}" for key, value in payload.items())


def append_promotions(path: Path, promotions: list[Promotion], dry_run: bool) -> None:
    if dry_run or not promotions:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    header = f"\n# Auto-promoted research families - {utc_now()}\n"
    lines = [header, *[format_alpha_line(item) + "\n" for item in promotions]]
    with path.open("a", encoding="utf-8") as fh:
        fh.writelines(lines)


def write_summary(summary: dict[str, Any]) -> None:
    try:
        SUMMARY_DIR.mkdir(parents=True, exist_ok=True)
        (SUMMARY_DIR / "latest_summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    except PermissionError:
        fallback = ROOT_DIR / "result_store" / "index" / "latest_research_alpha_promotion_summary.json"
        try:
            fallback.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        except PermissionError:
            pass


def promote(
    limit: int,
    dry_run: bool = False,
    similarity_threshold: float = DEFAULT_PROMOTION_SIMILARITY_THRESHOLD,
    allow_similar: bool = False,
) -> dict[str, Any]:
    existing = existing_raw_families()
    existing_sources = existing_raw_sources()
    existing_profiles = [seed_profile(seed) for seed in load_raw_alpha_pool(RAW_ALPHA_PATH)]
    promotions: list[Promotion] = []
    merged_hints: list[dict[str, Any]] = []
    skipped = 0
    for row in load_catalog():
        if limit > 0 and len(promotions) >= limit:
            break
        item = build_promotion(row, existing, existing_sources)
        if item is None:
            skipped += 1
            continue
        nearest_family, nearest_similarity = nearest_existing_family(item, existing_profiles)
        if not allow_similar and nearest_similarity >= similarity_threshold:
            skipped += 1
            merged_hints.append(
                {
                    "family": item.family,
                    "source": item.source,
                    "mechanism": item.mechanism,
                    "nearest_existing_family": nearest_family,
                    "nearest_similarity": nearest_similarity,
                    "action": "kept_as_hint_not_promoted",
                    "rationale": item.rationale,
                    "fields": item.fields,
                    "expression_family": item.expression_family,
                    "anti_correlation_target": item.anti_correlation_target,
                }
            )
            continue
        existing.add(item.family)
        existing_sources.add(item.source)
        existing_profiles.append(promotion_profile(item))
        promotions.append(item)
    append_promotions(RAW_ALPHA_PATH, promotions, dry_run=dry_run)
    summary = {
        "promoted": len(promotions),
        "skipped": skipped,
        "dry_run": dry_run,
        "raw_alpha_path": str(RAW_ALPHA_PATH.relative_to(ROOT_DIR)),
        "families": [item.family for item in promotions],
        "similarity_threshold": similarity_threshold,
        "allow_similar": allow_similar,
        "merged_hint_count": len(merged_hints),
        "merged_hints": merged_hints[:200],
    }
    write_summary(summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Promote extracted research families into raw_alpha_ai.md.")
    parser.add_argument("--limit", type=int, default=80, help="Maximum new raw-alpha seeds to append. 0 means no cap.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--similarity-threshold", type=float, default=DEFAULT_PROMOTION_SIMILARITY_THRESHOLD)
    parser.add_argument("--allow-similar", action="store_true", help="Append similar research families anyway.")
    args = parser.parse_args()
    print(
        json.dumps(
            promote(
                limit=args.limit,
                dry_run=args.dry_run,
                similarity_threshold=args.similarity_threshold,
                allow_similar=args.allow_similar,
            ),
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
