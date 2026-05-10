#!/usr/bin/env python
"""Raw alpha builder v1.

Pipeline:
1. idea parsing
2. field grounding
3. expression drafting

This is the first concrete implementation of the user's intended
"original alpha builder" and deliberately reuses the newer engineering layers:
- field_selection_engine
- template_validator
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
from pathlib import Path
import re
import sys
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from script.field_selection_engine import rank_field_records
from script.learning_query_engine import build_enriched_query, derive_query_hints, search_learning_sources
from script.template_validator import validate_template_payload


TEMP_DIR = ROOT_DIR / "temp" / "raw_alpha_builder"
RESEARCH_EXTRACTION_PATH = ROOT_DIR / "temp" / "research_alpha_extraction" / "latest_extracted_families.json"
RESEARCH_FAMILY_CATALOG_PATH = ROOT_DIR / "result_store" / "index" / "research_alpha_family_catalog.jsonl"

KEYWORD_GROUPS = {
    "sentiment": {"sentiment", "news", "tone", "underreaction", "reaction", "revision"},
    "analyst": {"analyst", "estimate", "eps", "target", "forecast", "revision"},
    "fundamental": {"fundamental", "valuation", "quality", "balance", "cashflow", "earnings", "yield", "sales"},
    "momentum": {"momentum", "trend", "persistent", "continuation", "acceleration"},
    "reversion": {"reversion", "mean", "revert", "oversold", "overbought"},
    "risk": {"risk", "volatility", "beta", "drawdown", "stability"},
    "ratio_to_price": {"yield", "valuation", "relative", "price", "close"},
}

ARCHETYPE_RULES = {
    "sentiment_momentum": {
        "requires_any": {"sentiment"},
        "bonus_if": {"momentum"},
        "penalty_if": {"analyst"},
        "category": "SENTIMENT",
    },
    "analyst_revision_momentum": {
        "requires_any": {"analyst"},
        "bonus_if": {"momentum", "ratio_to_price"},
        "penalty_if": set(),
        "category": "ANALYST",
    },
    "fundamental_ratio_to_price": {
        "requires_any": {"fundamental", "ratio_to_price"},
        "bonus_if": {"momentum"},
        "penalty_if": {"sentiment"},
        "category": "FUNDAMENTAL",
    },
    "fundamental_level_momentum": {
        "requires_any": {"fundamental"},
        "bonus_if": {"momentum"},
        "penalty_if": {"sentiment"},
        "category": "FUNDAMENTAL",
    },
    "risk_instability": {
        "requires_any": {"risk"},
        "bonus_if": {"reversion"},
        "penalty_if": set(),
        "category": "RELATION",
    },
}

ARCHETYPE_LIBRARY = {
    "sentiment_momentum": {
        "category": "SENTIMENT",
        "template_name": "raw_sentiment_momentum",
        "field_selection": {
            "dataset_hint": "sentiment",
            "blocked_types": ["EVENT"],
            "prefer_low_usage": True,
        },
        "expression": "group_rank(ts_mean(ts_backfill({{FIELD}}, 20), 5), industry)",
        "reason": "适合持续性情绪/新闻语义",
    },
    "analyst_revision_momentum": {
        "category": "ANALYST",
        "template_name": "raw_analyst_revision_momentum",
        "field_selection": {
            "dataset_hint": "analyst",
            "blocked_types": ["EVENT"],
            "prefer_low_usage": True,
        },
        "expression": "group_rank(ts_rank({{FIELD}}, 60), industry)",
        "reason": "适合分析师修正/预期变化语义",
    },
    "fundamental_ratio_to_price": {
        "category": "FUNDAMENTAL",
        "template_name": "raw_fundamental_ratio_to_price",
        "field_selection": {
            "dataset_hint": "fundamental",
            "blocked_types": ["EVENT"],
            "prefer_low_usage": True,
        },
        "expression": "group_rank(ts_rank({{FIELD}} / close, 60), industry)",
        "reason": "适合估值、收益率、相对价格语义",
    },
    "fundamental_level_momentum": {
        "category": "FUNDAMENTAL",
        "template_name": "raw_fundamental_level_momentum",
        "field_selection": {
            "dataset_hint": "fundamental",
            "blocked_types": ["EVENT"],
            "prefer_low_usage": True,
        },
        "expression": "ts_rank({{FIELD}}, 252)",
        "reason": "适合低频慢变量时间序列位置语义",
    },
    "risk_instability": {
        "category": "RELATION",
        "template_name": "raw_risk_instability",
        "field_selection": {
            "dataset_hint": "model",
            "blocked_types": ["EVENT"],
            "prefer_low_usage": True,
        },
        "expression": "-ts_rank({{FIELD}}, 126)",
        "reason": "适合风险/不稳定性越高越差的语义",
    },
}


@dataclass
class ParsedIdea:
    raw_text: str
    tokens: list[str]
    inferred_tags: list[str]
    inferred_category: str
    inferred_archetype: str
    rationale: list[str]


@dataclass
class ResearchFamilyMatch:
    family_key: str
    title: str
    score: int
    family: dict[str, Any]


def _tokenize(text: str) -> list[str]:
    return [token.lower() for token in re.findall(r"[A-Za-z0-9_]+", text)]


def _normalize_token(token: str) -> str:
    token = token.lower().strip()
    if token.endswith("ies") and len(token) > 4:
        return token[:-3] + "y"
    if token.endswith("s") and len(token) > 3:
        return token[:-1]
    return token


def load_research_extraction_payload() -> dict[str, Any]:
    if not RESEARCH_EXTRACTION_PATH.exists():
        return {}
    try:
        return json.loads(RESEARCH_EXTRACTION_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def load_research_family_pool() -> list[dict[str, Any]]:
    families: list[dict[str, Any]] = []
    seen_keys: set[str] = set()

    payload = load_research_extraction_payload()
    for family in payload.get("accepted") or []:
        key = str(family.get("family_key") or "").strip()
        if not key or key in seen_keys:
            continue
        seen_keys.add(key)
        families.append(family)

    if RESEARCH_FAMILY_CATALOG_PATH.exists():
        for line in RESEARCH_FAMILY_CATALOG_PATH.read_text(encoding="utf-8", errors="ignore").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                family = json.loads(line)
            except Exception:
                continue
            key = str(family.get("family_key") or "").strip()
            if not key or key in seen_keys:
                continue
            seen_keys.add(key)
            families.append(family)

    return families


def find_research_family_by_key(family_key: str) -> dict[str, Any] | None:
    wanted = family_key.strip().lower()
    if not wanted:
        return None
    for family in load_research_family_pool():
        key = str(family.get("family_key") or "").strip().lower()
        if key == wanted:
            return family
    return None


def score_research_family_match(idea_text: str, family: dict[str, Any]) -> int:
    idea_tokens = {_normalize_token(token) for token in _tokenize(idea_text)}
    if not idea_tokens:
        return 0
    text_parts = [
        str(family.get("title") or ""),
        str(family.get("family_key") or ""),
        str(family.get("parsed_idea_text") or ""),
        " ".join((family.get("mechanisms") or [])),
        " ".join(((family.get("field_plan") or {}).get("search_terms") or [])),
    ]
    family_tokens = {_normalize_token(token) for token in _tokenize(" ".join(text_parts))}
    overlap = idea_tokens & family_tokens
    score = len(overlap)
    if _normalize_token(str(family.get("domain") or "")) in idea_tokens:
        score += 2
    return score


def choose_research_family(idea_text: str) -> ResearchFamilyMatch | None:
    best: ResearchFamilyMatch | None = None
    for family in load_research_family_pool():
        score = score_research_family_match(idea_text, family)
        if score < 3:
            continue
        match = ResearchFamilyMatch(
            family_key=str(family.get("family_key") or ""),
            title=str(family.get("title") or ""),
            score=score,
            family=family,
        )
        if best is None or match.score > best.score or (
            match.score == best.score and match.family_key < best.family_key
        ):
            best = match
    return best


def parse_idea(text: str) -> ParsedIdea:
    tokens = _tokenize(text)
    token_set = set(tokens)
    matched_groups: list[str] = []
    rationale: list[str] = []
    for name, keywords in KEYWORD_GROUPS.items():
        overlap = sorted(token_set & keywords)
        if overlap:
            matched_groups.append(name)
            rationale.append(f"{name}: {', '.join(overlap)}")

    matched_set = set(matched_groups)
    scored_archetypes: list[tuple[str, int]] = []
    for archetype_name, rule in ARCHETYPE_RULES.items():
        score = 0
        if matched_set & set(rule["requires_any"]):
            score += 3
        score += len(matched_set & set(rule["bonus_if"]))
        score -= len(matched_set & set(rule["penalty_if"]))
        scored_archetypes.append((archetype_name, score))

    scored_archetypes.sort(key=lambda item: (-item[1], item[0]))
    archetype, best_score = scored_archetypes[0]
    category = ARCHETYPE_RULES[archetype]["category"]
    if best_score <= 0:
        archetype = "fundamental_level_momentum"
        category = "FUNDAMENTAL"
        rationale.append("fallback: default to slow-moving fundamental archetype")
    else:
        rationale.append(
            "archetype_scores: " +
            ", ".join(f"{name}={score}" for name, score in scored_archetypes)
        )

    return ParsedIdea(
        raw_text=text,
        tokens=tokens,
        inferred_tags=matched_groups,
        inferred_category=category,
        inferred_archetype=archetype,
        rationale=rationale,
    )


def build_template_from_archetype(parsed: ParsedIdea) -> dict:
    archetype = ARCHETYPE_LIBRARY[parsed.inferred_archetype]
    return {
        "name": archetype["template_name"],
        "type": "REGULAR",
        "category": archetype["category"],
        "tags": parsed.inferred_tags,
        "description": parsed.raw_text.strip(),
        "settings": {
            "instrumentType": "EQUITY",
            "region": "USA",
            "universe": "TOP3000",
            "delay": 1,
            "decay": 0,
            "neutralization": "INDUSTRY",
            "truncation": 0.08,
            "pasteurization": "ON",
            "unitHandling": "VERIFY",
            "nanHandling": "ON",
            "testPeriod": "P1Y",
            "language": "FASTEXPR",
            "visualization": False,
        },
        "field_selection": dict(archetype["field_selection"]),
        "expression": archetype["expression"],
    }


def build_template_from_research_family(family: dict[str, Any]) -> dict:
    template = json.loads(json.dumps(family.get("template") or {}))
    if not template:
        raise ValueError("Research family does not contain a template")
    return template


def load_field_records(path: Path) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        rows = payload.get("results") or []
        return rows if isinstance(rows, list) else []
    return payload if isinstance(payload, list) else []


def draft_output(
    idea_text: str,
    field_json: Path | None,
    selection_limit: int,
    research_family_key: str | None = None,
) -> dict:
    explicit_family = find_research_family_by_key(research_family_key or "")
    if explicit_family is not None:
        research_match = ResearchFamilyMatch(
            family_key=str(explicit_family.get("family_key") or ""),
            title=str(explicit_family.get("title") or ""),
            score=999,
            family=explicit_family,
        )
    else:
        research_match = choose_research_family(idea_text)

    if research_match is not None:
        family = research_match.family
        parsed = parse_idea(str(family.get("parsed_idea_text") or idea_text))
        template = build_template_from_research_family(family)
        field_plan = family.get("field_plan") or {}
        drafting_mode = "research_family"
        archetype_reason = f"Matched extracted research family {research_match.family_key} (score={research_match.score})"
    else:
        parsed = parse_idea(idea_text)
        template = build_template_from_archetype(parsed)
        field_plan = template.get("field_selection") or {}
        drafting_mode = "archetype_fallback"
        archetype_reason = ARCHETYPE_LIBRARY[parsed.inferred_archetype]["reason"]

    validation = validate_template_payload(Path("<generated>"), template)
    learning_hints = derive_query_hints(idea_text, max_hints=8)
    enriched_query = build_enriched_query(idea_text, max_hints=6)
    learning_hits = search_learning_sources(idea_text, max_hits=5)

    grounded_fields = []
    if field_json:
        records = load_field_records(field_json)
        selection_query = enriched_query
        if research_match is not None:
            research_terms = (field_plan.get("search_terms") or [])[:8]
            selection_query = " ".join([idea_text, *research_terms]).strip()
        decisions = rank_field_records(
            records=records,
            template=template,
            selection_query=selection_query,
            limit=selection_limit,
        )
        grounded_fields = [
            {
                "field": decision.record.get("id"),
                "dataset": (decision.record.get("dataset") or {}).get("id"),
                "category": (decision.record.get("category") or {}).get("id"),
                "coverage": decision.record.get("coverage"),
                "alphaCount": decision.record.get("alphaCount"),
                "userCount": decision.record.get("userCount"),
                "score": decision.score,
                "reasons": decision.reasons,
                "description": decision.record.get("description"),
            }
            for decision in decisions
        ]

    return {
        "idea_parsing": {
            "raw_text": parsed.raw_text,
            "tokens": parsed.tokens,
            "inferred_tags": parsed.inferred_tags,
            "inferred_category": parsed.inferred_category,
            "inferred_archetype": parsed.inferred_archetype,
            "rationale": parsed.rationale,
            "drafting_mode": drafting_mode,
            "matched_research_family": research_match.family_key if research_match else None,
        },
        "field_grounding": {
            "field_json": str(field_json) if field_json else None,
            "selection_query": selection_query if field_json else enriched_query,
            "candidate_count": len(grounded_fields),
            "top_candidates": grounded_fields,
            "field_plan": field_plan,
        },
        "learning_guidance": {
            "query_hints": learning_hints,
            "matched_sources": [
                {
                    "path": str(hit.path.relative_to(ROOT_DIR)),
                    "score": hit.score,
                    "snippet": hit.text[:500],
                }
                for hit in learning_hits
            ],
        },
        "expression_drafting": {
            "template": template,
            "validation": {
                "valid": validation.valid,
                "errors": validation.errors,
                "warnings": validation.warnings,
                "operators": validation.operators,
                "identifiers": validation.identifiers,
                "placeholders": validation.placeholders,
            },
            "archetype_reason": archetype_reason,
            "research_family_match": {
                "family_key": research_match.family_key,
                "title": research_match.title,
                "score": research_match.score,
            } if research_match else None,
        },
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build a raw alpha draft from an idea text.")
    parser.add_argument("--idea", help="Idea text directly")
    parser.add_argument("--idea-file", help="Text file containing the idea")
    parser.add_argument("--field-json", help="Optional raw field json for grounding")
    parser.add_argument("--selection-limit", type=int, default=20)
    parser.add_argument("--research-family-key", help="Use a specific extracted research family key")
    parser.add_argument("--output", help="Optional output json path")
    args = parser.parse_args()

    if not args.idea and not args.idea_file:
        raise SystemExit("Provide --idea or --idea-file")

    idea_text = args.idea
    if args.idea_file:
        idea_text = Path(args.idea_file).read_text(encoding="utf-8")

    field_json = Path(args.field_json).resolve() if args.field_json else None
    payload = draft_output(
        idea_text=idea_text or "",
        field_json=field_json,
        selection_limit=args.selection_limit,
        research_family_key=args.research_family_key,
    )

    output_path = Path(args.output).resolve() if args.output else None
    if output_path is None:
        TEMP_DIR.mkdir(parents=True, exist_ok=True)
        output_path = TEMP_DIR / "latest_raw_alpha_builder_output.json"

    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"Saved draft to: {output_path}")
    print(f"Archetype: {payload['idea_parsing']['inferred_archetype']}")
    print(f"Category: {payload['idea_parsing']['inferred_category']}")
    print(f"Grounded fields: {payload['field_grounding']['candidate_count']}")
    print(f"Expression valid: {payload['expression_drafting']['validation']['valid']}")


if __name__ == "__main__":
    main()
