#!/usr/bin/env python
"""Build a readable BRAIN dataset knowledge base from accessible metadata.

This absorbs the useful Brainiac/reference-project pattern: before generating
or grounding alphas, keep a compact knowledge base of datasets, descriptions,
coverage, usage, example fields, and mechanism fit. It is read-only analysis;
it does not fetch data or change runtime backtesting.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import re
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[1]
DATA_CATALOG_DIR = ROOT_DIR / "result_store" / "data_catalog"
OUTPUT_JSON = DATA_CATALOG_DIR / "dataset_knowledge_base.json"
OUTPUT_MD = DATA_CATALOG_DIR / "dataset_knowledge_base.md"
FALLBACK_DIR = Path("C:/tmp/learning_dataset_knowledge_base")

ATLAS_CANDIDATES = [
    DATA_CATALOG_DIR / "brain_dataset_atlas_extended_20260501.json",
    DATA_CATALOG_DIR / "brain_dataset_atlas.json",
    DATA_CATALOG_DIR / "brain_dataset_atlas_20260501.json",
]

TOKEN_RE = re.compile(r"[A-Za-z][A-Za-z0-9_]+")


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def load_atlas(path: Path | None = None) -> dict[str, Any]:
    if path is not None:
        return load_json(path)
    for candidate in ATLAS_CANDIDATES:
        payload = load_json(candidate)
        if isinstance(payload.get("datasets"), list):
            return payload
    return {}


def tokenize(text: str) -> set[str]:
    return {token.lower() for token in TOKEN_RE.findall(text or "") if len(token) > 2}


def novelty_tier(dataset: dict[str, Any]) -> str:
    alpha = safe_float(dataset.get("median_alpha_count"), default=999999.0)
    users = safe_float(dataset.get("median_user_count"), default=999999.0)
    coverage = safe_float(dataset.get("mean_field_coverage") or dataset.get("coverage"))
    fields = int(safe_float(dataset.get("field_count")))
    if coverage >= 0.65 and fields >= 20 and alpha <= 50 and users <= 20:
        return "cold_high_value"
    if coverage >= 0.55 and alpha <= 150 and users <= 60:
        return "underused"
    if coverage < 0.35:
        return "coverage_risk"
    if alpha >= 1000 or users >= 300:
        return "crowded"
    return "normal"


def top_mechanisms(dataset: dict[str, Any], limit: int = 4) -> list[str]:
    rows = dataset.get("mechanism_matches") or []
    mechanisms = [str(item.get("mechanism_id") or "") for item in rows if item.get("mechanism_id")]
    return mechanisms[:limit]


def dataset_card(dataset: dict[str, Any]) -> dict[str, Any]:
    examples = dataset.get("example_fields") or []
    top_terms = dataset.get("top_field_terms") or {}
    if isinstance(top_terms, dict):
        terms = list(top_terms.keys())[:18]
    else:
        terms = []
    return {
        "id": dataset.get("id"),
        "name": dataset.get("name"),
        "category": dataset.get("category"),
        "description": dataset.get("description"),
        "coverage": dataset.get("coverage"),
        "mean_field_coverage": dataset.get("mean_field_coverage"),
        "field_count": dataset.get("field_count"),
        "median_alpha_count": dataset.get("median_alpha_count"),
        "median_user_count": dataset.get("median_user_count"),
        "available_scopes": dataset.get("available_scopes") or [],
        "exploration_score": dataset.get("exploration_score"),
        "novelty_tier": novelty_tier(dataset),
        "mechanism_matches": top_mechanisms(dataset),
        "top_field_terms": terms,
        "example_fields": [
            {
                "id": field.get("id"),
                "description": field.get("description"),
                "type": field.get("type"),
                "coverage": field.get("coverage"),
                "alphaCount": field.get("alphaCount"),
                "userCount": field.get("userCount"),
            }
            for field in examples[:12]
            if isinstance(field, dict)
        ],
    }


def sort_cards(cards: list[dict[str, Any]]) -> list[dict[str, Any]]:
    tier_rank = {
        "cold_high_value": 0,
        "underused": 1,
        "normal": 2,
        "crowded": 3,
        "coverage_risk": 4,
    }
    return sorted(
        cards,
        key=lambda row: (
            tier_rank.get(str(row.get("novelty_tier")), 9),
            -safe_float(row.get("exploration_score")),
            -safe_float(row.get("mean_field_coverage") or row.get("coverage")),
            str(row.get("id") or ""),
        ),
    )


def render_markdown(cards: list[dict[str, Any]], atlas: dict[str, Any]) -> str:
    tier_counts = Counter(str(card.get("novelty_tier") or "unknown") for card in cards)
    lines = [
        "# BRAIN Dataset Knowledge Base",
        "",
        f"- Dataset count: {len(cards)}",
        f"- Field count: {atlas.get('field_count')}",
        f"- Source pattern: {atlas.get('source_pattern')}",
        f"- Novelty tiers: {dict(tier_counts)}",
        "",
        "## Priority Dataset Cards",
        "",
    ]
    for card in cards:
        examples = ", ".join(str(field.get("id") or "") for field in card.get("example_fields") or [])
        mechanisms = ", ".join(card.get("mechanism_matches") or [])
        terms = ", ".join(card.get("top_field_terms") or [])
        lines.extend(
            [
                f"### {card.get('id')} - {card.get('name')}",
                "",
                f"- Category: {card.get('category')}",
                f"- Novelty tier: {card.get('novelty_tier')}",
                f"- Coverage: dataset={card.get('coverage')} mean_field={card.get('mean_field_coverage')}",
                f"- Usage: median_alpha_count={card.get('median_alpha_count')} median_user_count={card.get('median_user_count')}",
                f"- Available scopes: {card.get('available_scopes')}",
                f"- Exploration score: {card.get('exploration_score')}",
                f"- Mechanism fit: {mechanisms or 'none'}",
                f"- Description: {card.get('description')}",
                f"- Top terms: {terms or 'none'}",
                f"- Example fields: {examples or 'none'}",
                "",
            ]
        )
    return "\n".join(lines).strip() + "\n"


def write_outputs(payload: dict[str, Any], cards: list[dict[str, Any]], atlas: dict[str, Any]) -> Path | None:
    for directory, json_path, md_path in [
        (DATA_CATALOG_DIR, OUTPUT_JSON, OUTPUT_MD),
        (FALLBACK_DIR, FALLBACK_DIR / OUTPUT_JSON.name, FALLBACK_DIR / OUTPUT_MD.name),
    ]:
        try:
            directory.mkdir(parents=True, exist_ok=True)
            json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            md_path.write_text(render_markdown(cards, atlas), encoding="utf-8")
            return json_path
        except PermissionError:
            continue
    return None


def build(path: Path | None = None) -> tuple[dict[str, Any], Path | None]:
    atlas = load_atlas(path)
    datasets = atlas.get("datasets") or []
    cards = sort_cards([dataset_card(row) for row in datasets if isinstance(row, dict)])
    payload = {
        "schema_version": 1,
        "source_dataset_count": atlas.get("dataset_count"),
        "source_field_count": atlas.get("field_count"),
        "cards": cards,
    }
    output = write_outputs(payload, cards, atlas)
    return payload, output


def main() -> None:
    parser = argparse.ArgumentParser(description="Build readable dataset knowledge base from dataset atlas.")
    parser.add_argument("--atlas", help="Optional atlas json path.")
    args = parser.parse_args()
    payload, output = build(Path(args.atlas) if args.atlas else None)
    print(f"Dataset cards: {len(payload['cards'])}")
    if output:
        try:
            print(f"JSON: {output.relative_to(ROOT_DIR)}")
        except ValueError:
            print(f"JSON: {output}")
    else:
        print("JSON: <write skipped: permission denied>")


if __name__ == "__main__":
    main()
