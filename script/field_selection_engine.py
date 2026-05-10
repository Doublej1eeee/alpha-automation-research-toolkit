#!/usr/bin/env python
"""Field selection engine for template-driven alpha generation.

This module borrows the useful direction from worldquant-miner generation_two:
- do not treat datafield fetching as the same thing as field selection
- rank fields by semantic match and metadata quality
- keep only a small, higher-value candidate pool for each template

It intentionally stays lightweight and governance-friendly:
- no giant persistent field pool
- no database dependency
- no automatic memory writes outside normal crawler/result storage
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import math
from pathlib import Path
import re
from typing import Iterable

from script.template_validator import analyze_expression_compatibility


TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")
STOPWORDS = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "by",
    "for",
    "from",
    "in",
    "is",
    "of",
    "on",
    "or",
    "the",
    "to",
    "with",
    "alpha",
    "template",
    "field",
    "rank",
    "group",
}


@dataclass
class FieldSelectionDecision:
    record: dict
    score: float
    reasons: list[str]


def tokenize(text: str | None) -> set[str]:
    if not text:
        return set()
    tokens = {
        token.lower()
        for token in TOKEN_RE.findall(text)
        if token and token.lower() not in STOPWORDS
    }
    return tokens


def extract_template_context(template: dict, selection_query: str | None = None) -> dict:
    field_selection = template.get("field_selection") or {}
    tags = template.get("tags") or []
    category = str(template.get("category") or "").strip()
    settings = template.get("settings") or {}
    expression = str(template.get("expression") or "")
    description = str(template.get("description") or "")
    name = str(template.get("name") or "")
    compatibility_profile = analyze_expression_compatibility(expression)

    text_parts = [
        name,
        description,
        expression,
        selection_query or "",
        " ".join(str(tag) for tag in tags),
        str(field_selection.get("query") or ""),
        str(field_selection.get("dataset_hint") or ""),
        str(field_selection.get("subcategory_hint") or ""),
    ]
    tokens = set()
    for part in text_parts:
        tokens.update(tokenize(part))

    return {
        "category": category.upper(),
        "region": str(settings.get("region") or ""),
        "delay": settings.get("delay"),
        "universe": str(settings.get("universe") or ""),
        "allowed_types": {
            str(item).upper()
            for item in (field_selection.get("allowed_types") or [])
        },
        "blocked_types": {
            str(item).upper()
            for item in (field_selection.get("blocked_types") or [])
        },
        "dataset_hint": str(field_selection.get("dataset_hint") or ""),
        "subcategory_hint": str(field_selection.get("subcategory_hint") or ""),
        "require_event": bool(field_selection.get("require_event")),
        "require_group_field": bool(field_selection.get("require_group_field")),
        "prefer_low_usage": field_selection.get("prefer_low_usage", True),
        "expression_uses_vector_operator": compatibility_profile.uses_vector_operator,
        "expression_uses_ts_backfill": compatibility_profile.uses_ts_backfill,
        "expression_uses_ts_operator": compatibility_profile.uses_ts_operator,
        "expression_uses_cross_sectional_operator": compatibility_profile.uses_cross_sectional_operator,
        "field_used_as_group_key": compatibility_profile.field_used_as_group_key,
        "cross_sectional_result_inside_ts_operator": compatibility_profile.cross_sectional_result_inside_ts_operator,
        "tokens": tokens,
        "query": selection_query or str(field_selection.get("query") or ""),
        "required_terms": {
            str(item).lower()
            for item in (field_selection.get("required_terms") or [])
            if str(item).strip()
        },
        "required_all_terms": {
            str(item).lower()
            for item in (field_selection.get("required_all_terms") or [])
            if str(item).strip()
        },
        "preferred_terms": {
            str(item).lower()
            for item in (field_selection.get("preferred_terms") or [])
            if str(item).strip()
        },
        "blocked_terms": {
            str(item).lower()
            for item in (field_selection.get("blocked_terms") or [])
            if str(item).strip()
        },
    }


def _record_text(record: dict) -> str:
    dataset = record.get("dataset") or {}
    category = record.get("category") or {}
    subcategory = record.get("subcategory") or {}
    return " ".join(
        [
            str(record.get("id") or ""),
            str(record.get("description") or ""),
            str(record.get("type") or ""),
            str(dataset.get("id") or ""),
            str(dataset.get("name") or ""),
            str(dataset.get("description") or ""),
            str(category.get("id") or ""),
            str(category.get("name") or ""),
            str(subcategory.get("id") or ""),
            str(subcategory.get("name") or ""),
        ]
    )


def _dataset_text(record: dict) -> str:
    dataset = record.get("dataset") or {}
    category = record.get("category") or {}
    subcategory = record.get("subcategory") or {}
    return " ".join(
        [
            str(dataset.get("id") or ""),
            str(dataset.get("name") or ""),
            str(dataset.get("description") or ""),
            str(category.get("id") or ""),
            str(category.get("name") or ""),
            str(subcategory.get("id") or ""),
            str(subcategory.get("name") or ""),
        ]
    )


def _safe_float(value, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def _semantic_score(template_tokens: set[str], record_tokens: set[str]) -> float:
    if not template_tokens or not record_tokens:
        return 0.0
    overlap = template_tokens & record_tokens
    if not overlap:
        return 0.0
    precision = len(overlap) / max(len(record_tokens), 1)
    recall = len(overlap) / max(len(template_tokens), 1)
    return (precision * 0.35) + (recall * 0.65)


def _dataset_novelty_bonus(alpha_count: float, user_count: float) -> float:
    # Low-use fields are often better discovery candidates, but do not let
    # missing metadata dominate true semantic fit.
    bonus = 0.0
    if 0 <= alpha_count <= 25:
        bonus += 0.18
    elif 25 < alpha_count <= 100:
        bonus += 0.08
    if 0 <= user_count <= 10:
        bonus += 0.14
    elif 10 < user_count <= 40:
        bonus += 0.06
    return bonus


def _usage_penalty(alpha_count: float, user_count: float, prefer_low_usage: bool) -> float:
    if not prefer_low_usage:
        return 0.0
    # gentle penalty: popular fields still allowed, but less preferred
    alpha_penalty = min(0.25, math.log1p(max(alpha_count, 0.0)) / 40.0)
    user_penalty = min(0.20, math.log1p(max(user_count, 0.0)) / 35.0)
    return alpha_penalty + user_penalty


def score_field_record(record: dict, template_context: dict) -> FieldSelectionDecision:
    reasons: list[str] = []
    score = 0.0

    field_type = str(record.get("type") or "").upper()
    category = (record.get("category") or {}).get("id") or ""
    dataset = record.get("dataset") or {}
    subcategory = record.get("subcategory") or {}
    coverage = _safe_float(record.get("coverage"))
    alpha_count = _safe_float(record.get("alphaCount"))
    user_count = _safe_float(record.get("userCount"))

    allowed_types = template_context["allowed_types"]
    blocked_types = template_context["blocked_types"]
    if allowed_types and field_type not in allowed_types:
        return FieldSelectionDecision(record, -1.0, [f"type blocked by allow-list: {field_type or 'UNKNOWN'}"])
    if blocked_types and field_type in blocked_types:
        return FieldSelectionDecision(record, -1.0, [f"type blocked: {field_type}"])
    if template_context["cross_sectional_result_inside_ts_operator"]:
        return FieldSelectionDecision(record, -1.0, ["template_expression_incompatible"])

    template_category = template_context["category"]
    if template_category and str(category).upper() == template_category:
        score += 1.8
        reasons.append("category_match")
    elif template_category:
        score -= 0.6
        reasons.append("category_mismatch")

    dataset_hint = template_context["dataset_hint"].lower()
    if dataset_hint:
        dataset_text = " ".join(
            [
                str(dataset.get("id") or ""),
                str(dataset.get("name") or ""),
                str(dataset.get("description") or ""),
            ]
        ).lower()
        if dataset_hint in dataset_text:
            score += 0.9
            reasons.append("dataset_hint_match")

    subcategory_hint = template_context["subcategory_hint"].lower()
    if subcategory_hint:
        subcategory_text = " ".join(
            [
                str(subcategory.get("id") or ""),
                str(subcategory.get("name") or ""),
            ]
        ).lower()
        if subcategory_hint in subcategory_text:
            score += 0.6
            reasons.append("subcategory_hint_match")

    record_tokens = tokenize(_record_text(record))
    record_text = _record_text(record).lower()

    required_terms = template_context["required_terms"]
    if required_terms and not (required_terms & record_tokens):
        return FieldSelectionDecision(record, -1.0, ["missing_required_terms"])

    required_all_terms = template_context["required_all_terms"]
    if required_all_terms:
        missing_terms = [
            term
            for term in required_all_terms
            if term not in record_tokens and not re.search(rf"\b{re.escape(term)}\b", record_text)
        ]
        if missing_terms:
            return FieldSelectionDecision(record, -1.0, [f"missing_required_all:{','.join(missing_terms)}"])

    blocked_terms = template_context["blocked_terms"]
    if blocked_terms:
        for term in blocked_terms:
            if term in record_tokens or re.search(rf"\b{re.escape(term)}\b", record_text):
                return FieldSelectionDecision(record, -1.0, [f"blocked_term:{term}"])

    semantic = _semantic_score(template_context["tokens"], record_tokens)
    score += semantic * 4.0
    if semantic > 0:
        reasons.append(f"semantic={semantic:.2f}")

    dataset_tokens = tokenize(_dataset_text(record))
    dataset_semantic = _semantic_score(template_context["tokens"], dataset_tokens)
    if dataset_semantic > 0:
        score += dataset_semantic * 1.6
        reasons.append(f"dataset_semantic={dataset_semantic:.2f}")

    preferred_terms = template_context["preferred_terms"]
    if preferred_terms:
        preferred_hits = [
            term
            for term in sorted(preferred_terms)
            if term in record_tokens or re.search(rf"\b{re.escape(term)}\b", record_text)
        ]
        if preferred_hits:
            score += min(2.4, 0.6 * len(preferred_hits))
            reasons.append(f"preferred={','.join(preferred_hits[:5])}")

    if coverage > 0:
        coverage_boost = min(1.6, coverage * 1.6)
        score += coverage_boost
        reasons.append(f"coverage={coverage:.2f}")
    else:
        score -= 0.4
        reasons.append("missing_coverage")

    if field_type == "MATRIX":
        score += 0.15
        reasons.append("matrix_ok")
    elif field_type == "VECTOR":
        if template_context["expression_uses_vector_operator"]:
            score += 0.1
            reasons.append("vector_operator_present")
        else:
            score -= 0.7
            reasons.append("vector_penalty_no_vec_operator")
    elif field_type == "EVENT":
        if template_context["expression_uses_ts_backfill"]:
            return FieldSelectionDecision(
                record,
                -1.0,
                ["event_incompatible_with_ts_backfill"],
            )
        if template_context["expression_uses_cross_sectional_operator"]:
            return FieldSelectionDecision(
                record,
                -1.0,
                ["event_incompatible_with_cross_sectional_operator"],
            )
        if template_context["require_event"]:
            score += 0.8
            reasons.append("event_required")
        else:
            score -= 0.8
            reasons.append("event_penalty")
    elif field_type == "GROUP":
        if template_context["expression_uses_ts_operator"] and not template_context["field_used_as_group_key"]:
            return FieldSelectionDecision(
                record,
                -1.0,
                ["group_field_incompatible_with_ts_operator"],
            )
        if template_context["require_group_field"] or template_context["field_used_as_group_key"]:
            score += 0.8
            reasons.append("group_field_required")
        else:
            score -= 1.0
            reasons.append("group_field_penalty")

    penalty = _usage_penalty(
        alpha_count=alpha_count,
        user_count=user_count,
        prefer_low_usage=bool(template_context["prefer_low_usage"]),
    )
    if penalty:
        score -= penalty
        reasons.append(f"usage_penalty={penalty:.2f}")

    novelty_bonus = _dataset_novelty_bonus(alpha_count, user_count)
    if novelty_bonus:
        score += novelty_bonus
        reasons.append(f"low_usage_bonus={novelty_bonus:.2f}")

    return FieldSelectionDecision(record=record, score=round(score, 6), reasons=reasons)


def rank_field_records(
    records: Iterable[dict],
    template: dict,
    selection_query: str | None = None,
    min_score: float | None = None,
    limit: int | None = None,
) -> list[FieldSelectionDecision]:
    template_context = extract_template_context(template, selection_query=selection_query)
    decisions = [
        score_field_record(record, template_context)
        for record in records
    ]
    decisions = [decision for decision in decisions if decision.score >= 0]
    if min_score is not None:
        decisions = [decision for decision in decisions if decision.score >= min_score]
    decisions.sort(
        key=lambda item: (
            -item.score,
            -_safe_float(item.record.get("coverage")),
            _safe_float(item.record.get("alphaCount"), default=999999.0),
            item.record.get("id") or "",
        )
    )
    if limit is not None:
        decisions = decisions[:limit]
    return decisions


def _load_template(path: Path) -> dict:
    import yaml

    with path.open("r", encoding="utf-8") as file:
        payload = yaml.safe_load(file)
    if not isinstance(payload, dict):
        raise ValueError(f"Invalid template file: {path}")
    return payload


def _load_records(path: Path) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        rows = payload.get("results") or []
        return rows if isinstance(rows, list) else []
    return payload if isinstance(payload, list) else []


def main() -> None:
    parser = argparse.ArgumentParser(description="Rank fetched datafields for a template.")
    parser.add_argument("template", help="Template yaml path")
    parser.add_argument("field_json", help="Raw field json exported by fetch_datafields.py")
    parser.add_argument("--selection-query")
    parser.add_argument("--limit", type=int, default=30)
    parser.add_argument("--min-score", type=float)
    args = parser.parse_args()

    template = _load_template(Path(args.template))
    records = _load_records(Path(args.field_json))
    decisions = rank_field_records(
        records=records,
        template=template,
        selection_query=args.selection_query,
        min_score=args.min_score,
        limit=args.limit,
    )
    for idx, decision in enumerate(decisions, start=1):
        record = decision.record
        print(
            f"{idx:02d}. {record.get('id')} | "
            f"score={decision.score:.3f} | "
            f"coverage={record.get('coverage')} | "
            f"alphaCount={record.get('alphaCount')} | "
            f"dataset={(record.get('dataset') or {}).get('id')} | "
            f"reasons={';'.join(decision.reasons)}"
        )


if __name__ == "__main__":
    main()
