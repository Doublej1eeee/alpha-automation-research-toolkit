#!/usr/bin/env python
"""Bridge the formal raw alpha pool into rotating supply jobs.

This module keeps two concerns separate:
1. research reading produces high-distinctness raw alpha seeds in raw_alpha_ai.md
2. supply/backtest infrastructure expands a chosen subset of those seeds into
   runnable slot-based template families for a fixed time window

The runtime bridge intentionally emits only temporary supply artifacts under
result_store/supply/templates so the formal raw alpha pool stays human-readable.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import math
import json
from pathlib import Path
import re
from typing import Any

import yaml


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


ALPHA_LINE_RE = re.compile(r"^ALPHA:\s*(.+)$")
FIELD_TOKEN_RE = re.compile(r"\b[A-Za-z_][A-Za-z0-9_]*\b")

ROOT_DIR = Path(__file__).resolve().parents[1]
RAW_ALPHA_POOL_PATH = ROOT_DIR / "alpha_generation" / "raw_alpha_ai.md"
MECHANISM_BANK_PATH = ROOT_DIR / "alpha_generation" / "research_mechanism_bank.yaml"
SUPPLY_TEMPLATE_ROOT = ROOT_DIR / "result_store" / "supply" / "templates" / "raw_alpha_rotation"
DATASET_ATLAS_PATHS = [
    ROOT_DIR / "result_store" / "data_catalog" / "brain_dataset_atlas_extended_20260501.json",
    ROOT_DIR / "result_store" / "data_catalog" / "brain_dataset_atlas.json",
    ROOT_DIR / "result_store" / "data_catalog" / "brain_dataset_atlas_20260501.json",
]
DATASET_KNOWLEDGE_BASE_PATH = ROOT_DIR / "result_store" / "data_catalog" / "dataset_knowledge_base.json"
RAW_FAMILY_ACTIONS_PATH = ROOT_DIR / "result_store" / "analysis" / "raw_family_actions.json"
RAW_FAMILY_CLUSTERS_PATH = ROOT_DIR / "result_store" / "analysis" / "raw_family_clusters.json"
RAW_FAMILY_ARCHIVE_PATH = ROOT_DIR / "result_store" / "analysis" / "raw_family_archive.json"

GENERIC_FIELD_TOKENS = {
    "anl4",
    "snt1",
    "nws12",
    "rp",
    "qfv4",
    "afv4",
    "qfv110",
    "v110",
    "d0",
    "d1",
    "value",
    "values",
    "number",
    "numbers",
    "mean",
    "median",
    "high",
    "low",
    "flag",
    "std",
    "percent",
    "quarterly",
}

RAW_SLOT_BLOCKED_TERMS = [
    "guidance",
    "maximum",
    "minimum",
    "reported",
    "currency",
    "person",
    "item",
    "bk",
    "actual",
    "actuals",
]

MIN_DATASET_COVERAGE_FLOOR = 0.45
MIN_RAW_SLOT_COVERAGE = 0.58
MIN_EVENT_NEWS_SLOT_COVERAGE = 0.65
HIGH_COVERAGE_ANCHOR = 0.75
MEDIUM_COVERAGE_CROWDED_ALPHA_COUNT = 1200.0

SEMANTIC_KEYWORDS = [
    "target",
    "earning",
    "earnings",
    "revision",
    "analyst",
    "coverage",
    "stock",
    "rank",
    "surprise",
    "sentiment",
    "news",
    "sales",
    "revenue",
    "asset",
    "assets",
    "debt",
    "cashflow",
    "equity",
    "ebitda",
    "ebit",
    "eps",
    "fcf",
    "capex",
    "goodwill",
    "buyback",
    "dividend",
    "profit",
]

FIELD_SEMANTIC_TAG_TERMS: dict[str, tuple[str, ...]] = {
    "analyst_revision": ("analyst", "estimate", "revision", "forecast", "target", "recommendation", "eps"),
    "earnings_quality": ("earnings", "income", "profit", "margin", "roe", "roa", "quality", "accrual"),
    "balance_sheet": ("asset", "liability", "debt", "equity", "book", "cash", "inventory", "leverage"),
    "cashflow": ("cashflow", "cash_flow", "free_cash_flow", "fcf", "capex", "operating_cash"),
    "liquidity": ("liquidity", "volume", "turnover", "vwap", "adv", "spread", "trade"),
    "risk_volatility": ("risk", "beta", "volatility", "variance", "correlation", "drawdown", "distress"),
    "sentiment_attention": ("sentiment", "tone", "attention", "buzz", "coverage", "media", "language"),
    "news_event": ("news", "event", "impact", "novelty", "relevance", "article", "story"),
    "valuation": ("valuation", "value", "price", "book", "sales", "earnings_yield", "multiple"),
    "mna_event": ("mna", "merger", "acquisition", "deal", "acquire", "takeover"),
    "credit_pressure": ("credit", "debt", "interest", "repayment", "refinancing", "issuance", "distress"),
}


@dataclass
class RawAlphaSeed:
    family: str
    source: str
    delay: str
    profile: str
    domain: str
    rationale: str
    fields: list[str]
    expression: str
    mechanism_id: str = ""
    data_family: str = ""
    expression_family: str = ""
    anti_correlation_target: str = ""


def slugify(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]+", "_", value).strip("_").lower() or "raw_alpha"


def parse_alpha_line(line: str) -> RawAlphaSeed | None:
    match = ALPHA_LINE_RE.match(line.strip())
    if not match:
        return None
    body = match.group(1)
    parts = [part.strip() for part in body.split(" | ") if part.strip()]
    payload: dict[str, str] = {}
    for part in parts:
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        payload[key.strip()] = value.strip()
    family = payload.get("family", "").strip()
    expression = payload.get("expression", "").strip()
    if not family or not expression:
        return None
    fields = [item.strip() for item in payload.get("fields", "").split(",") if item.strip()]
    return RawAlphaSeed(
        family=family,
        source=payload.get("source", "").strip(),
        delay=payload.get("delay", "").strip(),
        profile=payload.get("profile", "").strip(),
        domain=payload.get("domain", "").strip(),
        rationale=payload.get("rationale", "").strip(),
        fields=fields,
        expression=expression,
        mechanism_id=payload.get("mechanism", payload.get("mechanism_id", "")).strip(),
        data_family=payload.get("data_family", "").strip(),
        expression_family=payload.get("expression_family", "").strip(),
        anti_correlation_target=payload.get("anti_correlation_target", "").strip(),
    )


def load_research_mechanism_bank(path: Path | None = None) -> dict[str, dict[str, Any]]:
    bank_path = path or MECHANISM_BANK_PATH
    if not bank_path.exists():
        return {}
    payload = yaml.safe_load(bank_path.read_text(encoding="utf-8", errors="ignore"))
    if not isinstance(payload, dict):
        return {}
    raw_mechanisms = payload.get("mechanisms") or {}
    if isinstance(raw_mechanisms, list):
        mechanisms = {
            str(item.get("mechanism_id") or item.get("id") or "").strip(): item
            for item in raw_mechanisms
            if isinstance(item, dict)
        }
    elif isinstance(raw_mechanisms, dict):
        mechanisms = {
            str(key).strip(): value
            for key, value in raw_mechanisms.items()
            if isinstance(value, dict)
        }
    else:
        mechanisms = {}
    return {key: value for key, value in mechanisms.items() if key}


def mechanism_for_seed(seed: RawAlphaSeed, bank: dict[str, dict[str, Any]]) -> dict[str, Any]:
    if seed.mechanism_id and seed.mechanism_id in bank:
        return bank[seed.mechanism_id]
    for mechanism_id, card in bank.items():
        families = [str(item) for item in (card.get("raw_alpha_families") or [])]
        if seed.family in families:
            return {**card, "mechanism_id": mechanism_id}
    return {}


def mechanism_id_for_seed(seed: RawAlphaSeed, card: dict[str, Any]) -> str:
    return str(seed.mechanism_id or card.get("mechanism_id") or card.get("id") or seed.family).strip()


def list_value(payload: dict[str, Any], key: str) -> list[str]:
    value = payload.get(key)
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    if isinstance(value, str):
        return [item.strip() for item in value.split(",") if item.strip()]
    return []


def load_dataset_atlas() -> list[dict[str, Any]]:
    for path in DATASET_ATLAS_PATHS:
        if not path.exists():
            continue
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        datasets = payload.get("datasets") if isinstance(payload, dict) else None
        if isinstance(datasets, list):
            return [item for item in datasets if isinstance(item, dict)]
    return []


def load_dataset_knowledge_cards() -> dict[str, dict[str, Any]]:
    if not DATASET_KNOWLEDGE_BASE_PATH.exists():
        return {}
    try:
        payload = json.loads(DATASET_KNOWLEDGE_BASE_PATH.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return {}
    cards = payload.get("cards") if isinstance(payload, dict) else []
    return {
        str(card.get("id") or ""): card
        for card in (cards or [])
        if isinstance(card, dict) and card.get("id")
    }


def load_raw_family_actions() -> dict[str, dict[str, Any]]:
    if not RAW_FAMILY_ACTIONS_PATH.exists():
        return {}
    try:
        payload = json.loads(RAW_FAMILY_ACTIONS_PATH.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return {}
    rows = payload.get("actions") if isinstance(payload, dict) else []
    return {
        str(row.get("family") or ""): row
        for row in (rows or [])
        if isinstance(row, dict) and row.get("family")
    }


def load_raw_family_clusters(path: Path | None = None) -> dict[str, dict[str, Any]]:
    cluster_path = path or RAW_FAMILY_CLUSTERS_PATH
    if not cluster_path.exists():
        return {}
    try:
        payload = json.loads(cluster_path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    rows = payload.get("families")
    if isinstance(rows, dict):
        return {
            str(key): value
            for key, value in rows.items()
            if isinstance(value, dict)
        }
    if isinstance(rows, list):
        return {
            str(row.get("family") or ""): row
            for row in rows
            if isinstance(row, dict) and row.get("family")
        }
    return {}


def tokenize_dataset_text(text: str) -> set[str]:
    return {
        token.lower()
        for token in re.findall(r"[A-Za-z][A-Za-z0-9_]+", text or "")
        if len(token) > 2
    }


def dataset_text(dataset: dict[str, Any]) -> str:
    top_terms = dataset.get("top_field_terms") or {}
    if isinstance(top_terms, dict):
        terms = " ".join(str(key) for key in top_terms.keys())
    else:
        terms = ""
    return " ".join(
        [
            str(dataset.get("id") or ""),
            str(dataset.get("name") or ""),
            str(dataset.get("description") or ""),
            str(dataset.get("category") or ""),
            terms,
        ]
    ).lower()


def field_semantic_tags_for_dataset(
    dataset: dict[str, Any],
    dataset_card: dict[str, Any],
    seed_field: str,
    mechanism_id: str,
) -> list[str]:
    text_parts = [
        seed_field,
        mechanism_id,
        str(dataset.get("id") or ""),
        str(dataset.get("name") or ""),
        str(dataset.get("description") or ""),
        str(dataset.get("category") or ""),
        " ".join(str(item) for item in (dataset_card.get("top_field_terms") or [])),
    ]
    for field in dataset_card.get("example_fields") or []:
        if isinstance(field, dict):
            text_parts.extend([str(field.get("id") or ""), str(field.get("description") or "")])
    text = " ".join(text_parts).lower()
    scored: list[tuple[int, str]] = []
    for tag, terms in FIELD_SEMANTIC_TAG_TERMS.items():
        score = sum(text.count(term) for term in terms)
        if score:
            scored.append((score, tag))
    scored.sort(key=lambda item: (-item[0], item[1]))
    return [tag for _, tag in scored[:5]]


def mechanism_required_semantic_tags(mechanism_id: str) -> list[str]:
    mapping = {
        "attention_revision_congestion": ["analyst_revision", "sentiment_attention"],
        "promotion_dispersion_mismatch": ["analyst_revision", "sentiment_attention"],
        "instability_fragility_leadlag": ["risk_volatility", "sentiment_attention", "earnings_quality"],
        "balance_sheet_pressure": ["balance_sheet", "cashflow", "earnings_quality"],
        "liquidity_microstructure_shock": ["liquidity", "risk_volatility", "news_event"],
        "narrative_valuation_gap": ["news_event", "valuation", "earnings_quality"],
        "event_novelty_underreaction": ["news_event", "sentiment_attention", "liquidity"],
        "systematic_risk_regime_shift": ["risk_volatility"],
        "footnote_accounting_complexity": ["balance_sheet", "earnings_quality"],
        "credit_recovery_pressure": ["credit_pressure", "balance_sheet", "risk_volatility"],
        "mna_price_impact_absorption": ["mna_event", "news_event", "liquidity"],
    }
    return mapping.get(mechanism_id, [])


def mechanism_match_score(dataset: dict[str, Any], mechanism_id: str) -> float:
    for item in dataset.get("mechanism_matches") or []:
        if str(item.get("mechanism_id") or "") == mechanism_id:
            return min(float(item.get("score") or 0) / 20.0, 3.0)
    return 0.0


def dataset_category_allowed(dataset: dict[str, Any], mechanism: dict[str, Any], fallback_categories: list[str]) -> bool:
    category = str(dataset.get("category") or "").strip()
    if not category:
        return False
    allowed = set(list_value(mechanism, "allowed_dataset_categories") or fallback_categories)
    blocked = set(list_value(mechanism, "blocked_dataset_categories"))
    return category in allowed and category not in blocked


def slot_dataset_category_compatible(seed_field: str, slot_category: str, dataset_category: str, mechanism_id: str) -> bool:
    """Prevent broad mechanism allow-lists from overriding the slot's data semantics."""
    seed_lower = str(seed_field or "").lower()
    slot_category = str(slot_category or "").lower()
    dataset_category = str(dataset_category or "").lower()
    mechanism_id = str(mechanism_id or "").lower()
    if not dataset_category:
        return False

    if slot_category == "news":
        if dataset_category in {"news", "sentiment"}:
            return True
        if dataset_category == "model":
            # Price/volume response fields can legitimately come from model data;
            # generic event-attention placeholders should not.
            return any(term in seed_lower for term in ["price", "volume", "vwap", "atr", "return", "liquidity"])
        return False
    if slot_category == "sentiment":
        if dataset_category in {"sentiment", "news", "analyst"}:
            return True
        if dataset_category == "model":
            return any(term in seed_lower for term in ["factor", "model", "risk"])
        return False
    if slot_category == "analyst":
        return dataset_category in {"analyst", "sentiment", "model", "fundamental"}
    if slot_category == "fundamental":
        return dataset_category in {"fundamental", "model", "analyst"}
    if slot_category == "model":
        if mechanism_id in {"event_novelty_underreaction", "mna_price_impact_absorption"}:
            return dataset_category in {"model", "news", "fundamental"}
        return dataset_category in {"model", "fundamental", "analyst"}
    return True


def dataset_matching_scope(
    dataset: dict[str, Any],
    target_delay: int | None = None,
    target_universes: list[str] | None = None,
    target_region: str = "USA",
) -> dict[str, Any] | None:
    scopes = dataset.get("available_scopes") or []
    if not scopes:
        return {}
    target_universe_set = {str(item).upper() for item in (target_universes or []) if str(item)}
    fallback_match: dict[str, Any] | None = None
    for scope in scopes:
        if not isinstance(scope, dict):
            continue
        scope_region = str(scope.get("region") or "").upper()
        scope_delay = scope.get("delay")
        scope_universe = str(scope.get("universe") or "").upper()
        if target_region and scope_region and scope_region != target_region.upper():
            continue
        if target_delay is not None:
            try:
                if int(scope_delay) != int(target_delay):
                    continue
            except Exception:
                continue
        if target_universe_set and scope_universe not in target_universe_set:
            fallback_match = fallback_match or scope
            continue
        return scope
    return fallback_match if not target_universe_set else None


def dataset_effective_coverage(dataset: dict[str, Any], dataset_card: dict[str, Any] | None = None) -> float:
    dataset_card = dataset_card or {}
    values: list[float] = []
    for value in [
        dataset_card.get("mean_field_coverage"),
        dataset_card.get("coverage"),
        dataset.get("mean_field_coverage"),
        dataset.get("coverage"),
    ]:
        try:
            numeric = float(value)
        except Exception:
            continue
        if numeric > 0:
            values.append(numeric)
    if not values:
        return 0.0
    return max(values)


def min_slot_coverage_for_seed(seed: RawAlphaSeed, mechanism_id: str, category: str) -> float:
    seed_text = " ".join([seed.family, seed.domain, seed.data_family, seed.expression_family, category]).lower()
    if mechanism_id in {"event_novelty_underreaction", "mna_price_impact_absorption"}:
        return MIN_EVENT_NEWS_SLOT_COVERAGE
    if any(token in seed_text for token in ["news", "event", "sentiment", "attention"]):
        return MIN_EVENT_NEWS_SLOT_COVERAGE
    return MIN_RAW_SLOT_COVERAGE


def choose_atlas_dataset(
    atlas: list[dict[str, Any]],
    seed: RawAlphaSeed,
    mechanism: dict[str, Any],
    slot_category: str,
    seed_field: str,
    selection_query: str,
    target_delay: int | None = None,
    target_universes: list[str] | None = None,
) -> dict[str, Any] | None:
    candidates = choose_atlas_datasets(
        atlas=atlas,
        seed=seed,
        mechanism=mechanism,
        slot_category=slot_category,
        seed_field=seed_field,
        selection_query=selection_query,
        target_delay=target_delay,
        target_universes=target_universes,
        limit=1,
    )
    return candidates[0] if candidates else None


def choose_atlas_datasets(
    atlas: list[dict[str, Any]],
    seed: RawAlphaSeed,
    mechanism: dict[str, Any],
    slot_category: str,
    seed_field: str,
    selection_query: str,
    target_delay: int | None = None,
    target_universes: list[str] | None = None,
    dataset_cards: dict[str, dict[str, Any]] | None = None,
    family_action: dict[str, Any] | None = None,
    limit: int = 4,
) -> list[dict[str, Any]]:
    if not atlas:
        return []
    dataset_cards = dataset_cards or {}
    mechanism_id = mechanism_id_for_seed(seed, mechanism)
    fallback_categories = [item.strip() for item in dataset_categories_for_category(slot_category).split(",") if item.strip()]
    query_tokens = tokenize_dataset_text(
        " ".join(
            [
                seed.family,
                seed.domain,
                seed.data_family,
                seed.expression_family,
                seed_field,
                selection_query,
            ]
        )
    )
    scored: list[tuple[float, dict[str, Any]]] = []
    seed_field_lower = seed_field.lower()
    tuning = (family_action or {}).get("tuning_actions") if isinstance((family_action or {}).get("tuning_actions"), dict) else {}
    preferred_datasets = {str(item) for item in (tuning.get("preferred_datasets") or []) if str(item)}
    preferred_semantic_tags = {str(item) for item in (tuning.get("preferred_semantic_tags") or []) if str(item)}
    for dataset in atlas:
        if not dataset_category_allowed(dataset, mechanism, fallback_categories):
            continue
        category = str(dataset.get("category") or "")
        if not slot_dataset_category_compatible(seed_field, slot_category, category, mechanism_id):
            continue
        matching_scope = dataset_matching_scope(
            dataset,
            target_delay=target_delay,
            target_universes=target_universes,
            target_region="USA",
        )
        if matching_scope is None:
            continue
        field_count = int(dataset.get("field_count") or 0)
        if field_count <= 0:
            continue
        dataset_id = str(dataset.get("id") or "")
        dataset_card = dataset_cards.get(dataset_id) or {}
        effective_coverage = dataset_effective_coverage(dataset, dataset_card)
        novelty_tier = str(dataset_card.get("novelty_tier") or "")
        min_required_coverage = min_slot_coverage_for_seed(seed, mechanism_id, category)
        if effective_coverage and effective_coverage < MIN_DATASET_COVERAGE_FLOOR:
            continue
        text = dataset_text(dataset)
        dataset_tokens = tokenize_dataset_text(text)
        overlap = len(query_tokens & dataset_tokens)
        score = 0.0
        if category == slot_category:
            score += 4.0
        elif category in fallback_categories:
            score += 1.2
        score += mechanism_match_score(dataset, mechanism_id)
        try:
            score += min(float(dataset.get("exploration_score") or 0) * 0.45, 2.0)
        except Exception:
            pass
        score += min(overlap * 0.35, 4.0)
        score += min(field_count / 120.0, 1.5)
        score += min(effective_coverage * 1.6, 1.6)
        if effective_coverage and effective_coverage < min_required_coverage:
            shortfall = min_required_coverage - effective_coverage
            score -= 6.0 + shortfall * 12.0
        elif effective_coverage >= HIGH_COVERAGE_ANCHOR:
            score += 0.8
        if novelty_tier == "coverage_risk":
            score -= 4.0
        elif novelty_tier == "crowded" and effective_coverage < 0.65:
            score -= 1.2
        if mechanism_id == "balance_sheet_pressure" and category in {"analyst", "sentiment", "news"}:
            score -= 4.0
        median_alpha_count = float(dataset_card.get("median_alpha_count") or 0.0)
        median_user_count = float(dataset_card.get("median_user_count") or 0.0)
        if effective_coverage < 0.62 and median_alpha_count >= MEDIUM_COVERAGE_CROWDED_ALPHA_COUNT:
            score -= 2.2
        if effective_coverage < 0.6 and median_user_count >= 250:
            score -= 1.1
        if mechanism_id == "balance_sheet_pressure":
            if dataset_id == "fundamental6" and any(
                term in seed_field_lower
                for term in ["debt", "asset", "cash_flow", "cashflow", "free_cash_flow", "financing"]
            ):
                score += 3.0
            if dataset_id == "fundamental2" and any(
                term in seed_field_lower
                for term in ["footnote", "goodwill", "tax", "option", "deferred"]
            ):
                score += 3.0
        if mechanism_id == "liquidity_microstructure_shock" and str(dataset.get("id") or "") in {"model51", "news12", "model77"}:
            score += 2.0
            if dataset_id == "model51" and any(term in seed_field_lower for term in ["beta", "vol", "risk", "correlation"]):
                score += 2.0
            if dataset_id == "model77" and any(
                term in seed_field_lower for term in ["volume", "adv", "return", "close", "price", "turnover"]
            ):
                score += 2.0
            if dataset_id == "news12" and any(term in seed_field_lower for term in ["news", "reaction", "session", "minute"]):
                score += 2.0
        if mechanism_id in {"attention_revision_congestion", "promotion_dispersion_mismatch"} and str(dataset.get("id") or "") in {"analyst4", "news18", "sentiment1"}:
            score += 1.8
        if mechanism_id in {"attention_revision_congestion", "promotion_dispersion_mismatch", "instability_fragility_leadlag"}:
            if seed_field_lower.startswith("snt") and dataset_id == "sentiment1":
                score += 5.0
            if seed_field_lower.startswith("anl") and dataset_id == "analyst4":
                score += 5.0
            if any(term in seed_field_lower for term in ["news", "event", "novelty", "relevance"]) and dataset_id in {"news12", "news18"}:
                score += 3.0
        if mechanism_id in {"narrative_valuation_gap", "event_novelty_underreaction"}:
            if any(term in seed_field_lower for term in ["nws18", "rp_", "event", "relevance", "similarity", "nip", "ess", "css"]) and dataset_id == "news18":
                score += 5.0
            if any(term in seed_field_lower for term in ["news_", "ratio_vol", "atr", "vwap", "volume"]) and dataset_id == "news12":
                score += 5.0
            if any(term in seed_field_lower for term in ["perg", "return_equity", "quality", "valuation"]) and dataset_id in {"model77", "fundamental6"}:
                score += 3.0
        if mechanism_id == "systematic_risk_regime_shift":
            if any(term in seed_field_lower for term in ["beta", "correlation", "systematic", "unsystematic"]) and dataset_id == "model51":
                score += 7.0
        if mechanism_id == "footnote_accounting_complexity":
            if seed_field_lower.startswith("fn_") and dataset_id == "fundamental2":
                score += 7.0
            if seed_field_lower in {"assets", "assets_curr"} and dataset_id == "fundamental6":
                score += 5.0
        if mechanism_id == "credit_recovery_pressure":
            if seed_field_lower.startswith("fn_") and dataset_id == "fundamental2":
                score += 5.0
            if any(term in seed_field_lower for term in ["distress", "credit_risk"]) and dataset_id == "model77":
                score += 6.0
        if mechanism_id == "mna_price_impact_absorption":
            if any(term in seed_field_lower for term in ["mna", "rp_", "nws18"]) and dataset_id == "news18":
                score += 6.0
            if any(term in seed_field_lower for term in ["news_", "ratio_vol", "atr"]) and dataset_id == "news12":
                score += 5.0
            if any(term in seed_field_lower for term in ["acquire", "acquisition"]) and dataset_id in {"fundamental2", "fundamental6"}:
                score += 5.0
        if dataset_id in preferred_datasets:
            score += 1.4
        semantic_tags = set(field_semantic_tags_for_dataset(dataset, dataset_card, seed_field, mechanism_id))
        if preferred_semantic_tags:
            score += min(len(semantic_tags & preferred_semantic_tags) * 0.45, 1.8)
        dataset_with_scope = dict(dataset)
        dataset_with_scope["_matched_scope"] = matching_scope
        scored.append((score, dataset_with_scope))
    scored.sort(key=lambda item: (-item[0], str(item[1].get("id") or "")))
    selected: list[dict[str, Any]] = []
    selected_categories: set[str] = set()
    selected_ids: set[str] = set()
    for score, dataset in scored:
        if score <= 0:
            continue
        dataset_id = str(dataset.get("id") or "")
        if not dataset_id or dataset_id in selected_ids:
            continue
        category = str(dataset.get("category") or "")
        if len(selected) >= 1 and category in selected_categories and len(selected) < min(limit, 3):
            continue
        selected.append(dataset)
        selected_ids.add(dataset_id)
        if category:
            selected_categories.add(category)
        if len(selected) >= limit:
            break
    if len(selected) < limit:
        for score, dataset in scored:
            if score <= 0:
                continue
            dataset_id = str(dataset.get("id") or "")
            if not dataset_id or dataset_id in selected_ids:
                continue
            selected.append(dataset)
            selected_ids.add(dataset_id)
            if len(selected) >= limit:
                break
    return selected


def load_raw_alpha_pool(path: Path | None = None) -> list[RawAlphaSeed]:
    source_path = path or RAW_ALPHA_POOL_PATH
    if not source_path.exists():
        return []
    entries: list[RawAlphaSeed] = []
    for line in source_path.read_text(encoding="utf-8", errors="ignore").splitlines():
        entry = parse_alpha_line(line)
        if entry:
            entries.append(entry)
    deduped: dict[str, RawAlphaSeed] = {}
    for entry in entries:
        deduped.setdefault(entry.family, entry)
    return list(deduped.values())


def load_previous_selected_families() -> list[str]:
    supply_path = ROOT_DIR / "result_store" / "supply" / "supply_jobs.json"
    if not supply_path.exists():
        return []
    try:
        payload = json.loads(supply_path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return []
    raw = payload.get("raw_alpha_rotation") if isinstance(payload, dict) else {}
    selected = raw.get("selected_families") if isinstance(raw, dict) else []
    return [str(item) for item in (selected or []) if str(item)]


def load_raw_family_archive() -> dict[str, Any]:
    if not RAW_FAMILY_ARCHIVE_PATH.exists():
        return {"schema_version": 1, "used_families": []}
    try:
        payload = json.loads(RAW_FAMILY_ARCHIVE_PATH.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return {"schema_version": 1, "used_families": []}
    if not isinstance(payload, dict):
        return {"schema_version": 1, "used_families": []}
    payload.setdefault("schema_version", 1)
    payload.setdefault("used_families", [])
    return payload


def save_raw_family_archive(payload: dict[str, Any]) -> None:
    payload["updated_at"] = utc_now()
    RAW_FAMILY_ARCHIVE_PATH.parent.mkdir(parents=True, exist_ok=True)
    RAW_FAMILY_ARCHIVE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def archive_used_raw_families(families: list[str]) -> dict[str, Any]:
    payload = load_raw_family_archive()
    used = [str(item) for item in (payload.get("used_families") or []) if str(item)]
    seen = set(used)
    changed = False
    for family in families:
        family = str(family or "").strip()
        if not family or family in seen:
            continue
        used.append(family)
        seen.add(family)
        changed = True
    payload["used_families"] = used
    if changed:
        save_raw_family_archive(payload)
    return payload


def count_completed_family_alphas(family: str) -> int:
    if not family:
        return 0
    batch_dir = ROOT_DIR / "result_store" / "batches"
    if not batch_dir.exists():
        return 0
    seen: set[str] = set()
    for path in batch_dir.glob(f"*{family}*.jsonl"):
        try:
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except Exception:
            continue
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except Exception:
                continue
            alpha_id = str(payload.get("alpha_id") or ((payload.get("alpha_details") or {}).get("id")) or "")
            if alpha_id:
                seen.add(alpha_id)
    return len(seen)


def family_recent_quality_stats(family: str, recent_limit: int = 1500) -> dict[str, Any]:
    if not family:
        return {"completed": 0}
    batch_dir = ROOT_DIR / "result_store" / "batches"
    if not batch_dir.exists():
        return {"completed": 0}
    rows: list[dict[str, Any]] = []
    for path in batch_dir.glob(f"*{family}*.jsonl"):
        try:
            mtime = path.stat().st_mtime
            lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
        except Exception:
            continue
        for index, line in enumerate(lines):
            try:
                payload = json.loads(line)
            except Exception:
                continue
            payload["_sort_key"] = (mtime, index)
            rows.append(payload)
    rows.sort(key=lambda row: row.get("_sort_key") or (0, 0), reverse=True)
    recent = rows[: max(1, int(recent_limit))]
    grade_counts: dict[str, int] = {}
    high_grade_count = 0
    average_plus_count = 0
    good_plus_count = 0
    for payload in recent:
        details = payload.get("alpha_details") or {}
        grade = str(details.get("grade") or payload.get("grade") or "UNKNOWN").upper()
        grade_counts[grade] = grade_counts.get(grade, 0) + 1
        if grade in {"AVERAGE", "GOOD", "EXCELLENT", "SPECTACULAR"}:
            average_plus_count += 1
        if grade in {"GOOD", "EXCELLENT", "SPECTACULAR"}:
            good_plus_count += 1
        if grade in {"EXCELLENT", "SPECTACULAR"}:
            high_grade_count += 1
    return {
        "completed": len(rows),
        "recent_count": len(recent),
        "recent_grade_counts": grade_counts,
        "recent_average_plus": average_plus_count,
        "recent_good_plus": good_plus_count,
        "recent_high_grade": high_grade_count,
    }


def previous_family_capacity(family: str) -> dict[str, Any]:
    if not family:
        return {}
    supply_path = ROOT_DIR / "result_store" / "supply" / "supply_jobs.json"
    if not supply_path.exists():
        return {}
    try:
        payload = json.loads(supply_path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return {}
    jobs = payload.get("jobs") if isinstance(payload, dict) else []
    if not isinstance(jobs, list):
        return {}
    family_jobs = [
        job for job in jobs
        if isinstance(job, dict) and str(job.get("raw_alpha_family") or "") == family
    ]
    if not family_jobs:
        return {}
    caps = [int(job.get("estimated_field_map_cap") or 0) for job in family_jobs]
    slot_counts: list[int] = []
    min_slot_counts: list[int] = []
    for job in family_jobs:
        inventories = job.get("slot_inventories") or {}
        if not isinstance(inventories, dict):
            continue
        counts: list[int] = []
        for spec in inventories.values():
            if not isinstance(spec, dict):
                continue
            inventory_name = str(spec.get("inventory_name") or "")
            if not inventory_name:
                continue
            inventory_path = ROOT_DIR / "result_store" / "inventories" / f"{inventory_name}.json"
            try:
                inv = json.loads(inventory_path.read_text(encoding="utf-8", errors="ignore"))
            except Exception:
                continue
            count = int(inv.get("selected_record_count") or len(inv.get("fields") or []) or 0)
            counts.append(count)
            slot_counts.append(count)
        if counts:
            min_slot_counts.append(min(counts))
    return {
        "job_count": len(family_jobs),
        "estimated_field_map_cap": max(caps) if caps else 0,
        "rough_capacity": len(family_jobs) * (max(caps) if caps else 0),
        "slot_inventory_counts": slot_counts,
        "min_slot_inventory_count": min(min_slot_counts) if min_slot_counts else None,
        "max_slot_inventory_count": max(slot_counts) if slot_counts else None,
    }


def should_extend_family_after_minimum(
    stats: dict[str, Any],
    min_completed: int,
    max_completed: int,
    high_grade_bonus_completed: int,
    capacity: dict[str, Any] | None = None,
    small_pool_min_slot_count: int = 0,
    small_pool_completed_floor: int = 0,
) -> tuple[bool, str]:
    completed = int(stats.get("completed") or 0)
    capacity = capacity or {}
    min_slot_count = capacity.get("min_slot_inventory_count")
    if (
        small_pool_min_slot_count > 0
        and small_pool_completed_floor > 0
        and min_slot_count is not None
        and int(min_slot_count) <= small_pool_min_slot_count
        and completed >= small_pool_completed_floor
    ):
        return False, "small_candidate_pool_exhausted"
    if completed < min_completed:
        return True, "below_min_completed"
    if completed >= max_completed:
        if int(stats.get("recent_high_grade") or 0) > 0 and completed < high_grade_bonus_completed:
            return True, "high_grade_bonus_extension"
        return False, "max_completed_reached"
    if int(stats.get("recent_high_grade") or 0) > 0:
        return True, "high_grade_bonus_extension"
    if int(stats.get("recent_good_plus") or 0) > 0:
        return True, "recent_good_plus_extension"
    if int(stats.get("recent_average_plus") or 0) > 0 and completed < max(min_completed + 1500, max_completed):
        return True, "recent_average_plus_extension"
    return False, "recent_yield_too_low"


def choose_rotating_raw_alphas(
    entries: list[RawAlphaSeed],
    rotation_hours: int,
    active_count: int,
    min_completed_before_switch: int = 0,
    max_completed_before_switch: int = 8000,
    high_grade_bonus_completed_before_switch: int = 10000,
    recent_quality_window: int = 1500,
    small_pool_min_slot_count: int = 0,
    small_pool_completed_floor: int = 0,
    family_actions: dict[str, dict[str, Any]] | None = None,
    family_clusters: dict[str, dict[str, Any]] | None = None,
    excluded_families: set[str] | None = None,
    allowed_cluster_statuses: set[str] | None = None,
    now: datetime | None = None,
) -> tuple[list[RawAlphaSeed], dict[str, Any]]:
    if not entries:
        return [], {
            "rotation_hours": rotation_hours,
            "active_count": active_count,
            "window_index": 0,
            "total_entries": 0,
            "selected_families": [],
            "excluded_used_families": sorted(excluded_families or set()),
        }
    family_actions = family_actions or {}
    family_clusters = family_clusters or {}
    excluded_families = excluded_families or set()
    allowed_cluster_statuses = allowed_cluster_statuses or {"primary", "candidate", ""}
    ordered_all = sorted(entries, key=lambda item: item.family)
    ordered = [
        entry
        for entry in ordered_all
        if entry.family not in excluded_families
        and not bool((family_actions.get(entry.family) or {}).get("rotation_paused"))
        and str((family_clusters.get(entry.family) or {}).get("status") or "") in allowed_cluster_statuses
    ]
    skipped_retired = [entry.family for entry in ordered_all if entry not in ordered]
    if not ordered:
        ordered = [
            entry
            for entry in ordered_all
            if entry.family not in excluded_families
            and not bool((family_actions.get(entry.family) or {}).get("rotation_paused"))
        ] or ordered_all
        skipped_retired = [entry.family for entry in ordered_all if entry not in ordered]
    now_utc = now or datetime.now(timezone.utc)
    rotation_seconds = max(1, rotation_hours) * 3600
    window_index = int(math.floor(now_utc.timestamp() / rotation_seconds))
    count = max(1, min(active_count, len(ordered)))
    previous_selected = load_previous_selected_families()
    carry_selected: list[RawAlphaSeed] = []
    carry_counts: dict[str, int] = {}
    carry_reasons: dict[str, str] = {}
    carry_quality_stats: dict[str, dict[str, Any]] = {}
    carry_capacity: dict[str, dict[str, Any]] = {}
    if min_completed_before_switch > 0 and previous_selected:
        eligible_by_family = {entry.family: entry for entry in ordered}
        for family in previous_selected[:count]:
            entry = eligible_by_family.get(family)
            if not entry:
                continue
            stats = family_recent_quality_stats(family, recent_limit=recent_quality_window)
            completed = int(stats.get("completed") or count_completed_family_alphas(family))
            capacity = previous_family_capacity(family)
            carry_counts[family] = completed
            should_extend, reason = should_extend_family_after_minimum(
                stats,
                min_completed=min_completed_before_switch,
                max_completed=max_completed_before_switch,
                high_grade_bonus_completed=high_grade_bonus_completed_before_switch,
                capacity=capacity,
                small_pool_min_slot_count=small_pool_min_slot_count,
                small_pool_completed_floor=small_pool_completed_floor,
            )
            carry_reasons[family] = reason
            carry_quality_stats[family] = stats
            carry_capacity[family] = capacity
            if should_extend:
                carry_selected.append(entry)
    if len(carry_selected) >= count:
        return carry_selected[:count], {
            "rotation_hours": rotation_hours,
            "active_count": count,
            "window_index": window_index,
            "total_entries": len(ordered_all),
            "eligible_entries": len(ordered),
            "skipped_retired_families": skipped_retired,
            "cluster_state_enabled": bool(family_clusters),
            "allowed_cluster_statuses": sorted(status or "<unset>" for status in allowed_cluster_statuses),
            "cluster_status_counts": {
                status: sum(1 for row in family_clusters.values() if str(row.get("status") or "") == status)
                for status in sorted({str(row.get("status") or "") for row in family_clusters.values()})
            },
            "selected_families": [entry.family for entry in carry_selected[:count]],
            "excluded_used_families": sorted(excluded_families),
            "rotation_override": "min_completed_before_switch",
            "min_completed_before_switch": min_completed_before_switch,
            "max_completed_before_switch": max_completed_before_switch,
            "high_grade_bonus_completed_before_switch": high_grade_bonus_completed_before_switch,
            "recent_quality_window": recent_quality_window,
            "previous_family_completed_counts": carry_counts,
            "previous_family_carry_reasons": carry_reasons,
            "previous_family_quality_stats": carry_quality_stats,
            "previous_family_capacity": carry_capacity,
        }
    start = (window_index * count) % len(ordered)
    selected = [ordered[(start + offset) % len(ordered)] for offset in range(count)]
    if carry_selected:
        carry_names = {entry.family for entry in carry_selected}
        remainder = [entry for entry in selected if entry.family not in carry_names]
        selected = [*carry_selected, *remainder]
        selected = selected[:count]
    return selected, {
        "rotation_hours": rotation_hours,
        "active_count": count,
        "window_index": window_index,
        "total_entries": len(ordered_all),
        "eligible_entries": len(ordered),
        "skipped_retired_families": skipped_retired,
        "cluster_state_enabled": bool(family_clusters),
        "allowed_cluster_statuses": sorted(status or "<unset>" for status in allowed_cluster_statuses),
        "cluster_status_counts": {
            status: sum(1 for row in family_clusters.values() if str(row.get("status") or "") == status)
            for status in sorted({str(row.get("status") or "") for row in family_clusters.values()})
        },
        "selected_families": [entry.family for entry in selected],
        "excluded_used_families": sorted(excluded_families),
        "rotation_override": "min_completed_before_switch" if carry_selected else "",
        "min_completed_before_switch": min_completed_before_switch,
        "max_completed_before_switch": max_completed_before_switch,
        "high_grade_bonus_completed_before_switch": high_grade_bonus_completed_before_switch,
        "recent_quality_window": recent_quality_window,
        "previous_family_completed_counts": carry_counts,
        "previous_family_carry_reasons": carry_reasons,
        "previous_family_quality_stats": carry_quality_stats,
        "previous_family_capacity": carry_capacity,
    }


def infer_field_category(field: str) -> str:
    token = field.lower()
    if token.startswith("nws") or token.startswith("rp_") or token.startswith("news_"):
        return "news"
    if token.startswith("snt") or any(key in token for key in ["sent", "news", "tone", "coverage", "surprise"]):
        return "sentiment"
    if token.startswith("anl") or any(key in token for key in ["analyst", "target", "forecast", "estimate", "eps"]):
        return "analyst"
    if token.startswith("mdl") or any(key in token for key in ["close", "open", "volume", "vwap", "book", "bid", "ask", "return", "adv", "turnover", "volatility", "beta", "correlation", "systematic", "unsystematic", "distress", "credit_risk"]):
        return "model"
    return "fundamental"


def dataset_categories_for_category(category: str) -> str:
    mapping = {
        "news": "news,sentiment",
        "sentiment": "sentiment,news,analyst",
        "analyst": "analyst,model,fundamental",
        "fundamental": "fundamental,model,analyst",
        "model": "model,analyst,fundamental",
    }
    return mapping.get(category, "fundamental,analyst,model,news,sentiment")


def normalize_job_category(seed: RawAlphaSeed) -> str:
    domain = seed.domain.lower()
    if "news" in seed.data_family.lower() or "event" in domain:
        return "news"
    if "analyst" in domain:
        return "analyst"
    if "sentiment" in domain or "attention" in domain:
        return "sentiment"
    if "risk" in domain or "credit" in domain or "instability" in domain:
        return "relation"
    return "fundamental"


def delay_variants_for_seed(seed: RawAlphaSeed) -> list[int]:
    text = seed.delay.lower()
    if "d0_primary" in text:
        return [0]
    if "d0_optional" in text:
        return [1, 0]
    return [1]


def normalize_settings_profile(value: dict[str, Any], seed: RawAlphaSeed) -> dict[str, list[Any]]:
    delay_values = value.get("delay")
    if delay_values is None:
        delay = delay_variants_for_seed(seed)
    elif isinstance(delay_values, list):
        delay = [int(item) for item in delay_values]
    else:
        delay = [int(delay_values)]
    profile = {
        "delay": delay,
        "neutralization": [str(item) for item in (value.get("neutralization") or []) if str(item)],
        "decay": [int(item) for item in (value.get("decay") or [])],
        "truncation": [float(item) for item in (value.get("truncation") or [])],
        "universe": [str(item) for item in (value.get("universe") or []) if str(item)],
    }
    profile["neutralization"] = profile["neutralization"] or ["INDUSTRY", "SUBINDUSTRY"]
    profile["decay"] = profile["decay"] or [0, 4]
    profile["truncation"] = profile["truncation"] or [0.08]
    profile["universe"] = profile["universe"] or ["TOP3000"]
    return profile


def mechanism_setting_grid(mechanism_id: str, seed: RawAlphaSeed, mechanism: dict[str, Any] | None = None) -> dict[str, list[Any]]:
    """Choose runtime settings from the mechanism card, with conservative fallback profiles."""
    mechanism = mechanism or {}
    profile = mechanism.get("settings_profile")
    if isinstance(profile, dict):
        return normalize_settings_profile(profile, seed)
    mechanism_id = mechanism_id.lower()
    base_delay = delay_variants_for_seed(seed)
    if mechanism_id == "liquidity_microstructure_shock":
        return {
            "delay": base_delay,
            "neutralization": ["SUBINDUSTRY", "INDUSTRY", "MARKET"],
            "decay": [0, 2, 4],
            "truncation": [0.05, 0.08],
            "universe": ["TOP3000", "TOP1000"],
        }
    if mechanism_id == "balance_sheet_pressure":
        return {
            "delay": [1],
            "neutralization": ["SUBINDUSTRY", "INDUSTRY", "SECTOR"],
            "decay": [4, 8, 12],
            "truncation": [0.05, 0.08],
            "universe": ["TOP3000"],
        }
    if mechanism_id == "instability_fragility_leadlag":
        return {
            "delay": base_delay,
            "neutralization": ["SUBINDUSTRY", "INDUSTRY"],
            "decay": [2, 4, 8],
            "truncation": [0.05, 0.08],
            "universe": ["TOP3000", "TOP1000"],
        }
    if mechanism_id == "promotion_dispersion_mismatch":
        return {
            "delay": [1],
            "neutralization": ["SUBINDUSTRY", "INDUSTRY", "SECTOR"],
            "decay": [0, 4, 8],
            "truncation": [0.05, 0.08],
            "universe": ["TOP3000"],
        }
    if mechanism_id == "attention_revision_congestion":
        return {
            "delay": [1],
            "neutralization": ["SUBINDUSTRY", "INDUSTRY"],
            "decay": [0, 2, 4],
            "truncation": [0.05, 0.08],
            "universe": ["TOP3000", "TOP1000"],
        }
    if mechanism_id in {"narrative_valuation_gap", "event_novelty_underreaction"}:
        return {
            "delay": base_delay,
            "neutralization": ["SUBINDUSTRY", "INDUSTRY", "SECTOR"],
            "decay": [2, 4, 8],
            "truncation": [0.05, 0.08],
            "universe": ["TOP3000", "TOP1000"],
        }
    if mechanism_id == "systematic_risk_regime_shift":
        return {
            "delay": [1],
            "neutralization": ["MARKET", "SECTOR", "INDUSTRY"],
            "decay": [0, 2, 4],
            "truncation": [0.05, 0.08],
            "universe": ["TOP3000", "TOP1000"],
        }
    if mechanism_id in {"footnote_accounting_complexity", "credit_recovery_pressure"}:
        return {
            "delay": [1],
            "neutralization": ["SUBINDUSTRY", "INDUSTRY", "SECTOR"],
            "decay": [6, 12, 20],
            "truncation": [0.05, 0.08],
            "universe": ["TOP3000"],
        }
    if mechanism_id == "mna_price_impact_absorption":
        return {
            "delay": base_delay,
            "neutralization": ["SUBINDUSTRY", "INDUSTRY", "MARKET"],
            "decay": [0, 2, 4],
            "truncation": [0.05, 0.08],
            "universe": ["TOP3000", "TOP1000"],
        }
    return {
        "delay": base_delay,
        "neutralization": ["INDUSTRY", "SUBINDUSTRY"],
        "decay": [0, 4],
        "truncation": [0.08],
        "universe": ["TOP3000"],
    }


def mechanism_setting_rationale(mechanism_id: str) -> str:
    mechanism_id = mechanism_id.lower()
    rationales = {
        "liquidity_microstructure_shock": "Liquidity shocks need broader neutralization tests and short decay because turnover and reversal behavior are part of the mechanism.",
        "balance_sheet_pressure": "Balance-sheet pressure is slower and industry-relative, so the grid favors D1, sector/industry/subindustry neutralization, and moderate decay.",
        "instability_fragility_leadlag": "Fragility signals combine earnings and sentiment uncertainty, so the grid keeps industry/subindustry neutralization and moderate smoothing.",
        "promotion_dispersion_mismatch": "Promotion versus dispersion is analyst-relative, so it tests industry/subindustry/sector neutralization with low-to-moderate decay.",
        "attention_revision_congestion": "Revision congestion is analyst/coverage driven, so it stays D1 and tests industry/subindustry neutralization with short decay.",
        "narrative_valuation_gap": "Narrative valuation gap mixes news and quality, so it tests industry/subindustry/sector neutralization and moderate event smoothing.",
        "event_novelty_underreaction": "Event novelty can be noisy and time sensitive, so it uses mechanism delay options with event smoothing and sector-aware neutralization.",
        "systematic_risk_regime_shift": "Systematic risk is market/sector related, so the grid includes market and sector neutralization instead of only subindustry.",
        "footnote_accounting_complexity": "Accounting footnote pressure is slow-moving, so it uses D1, industry-relative neutralization, and longer decay.",
        "credit_recovery_pressure": "Credit refinancing pressure is slow and capital-structure related, so it uses D1, industry-relative neutralization, and longer decay.",
        "mna_price_impact_absorption": "M&A absorption is event-driven and liquidity sensitive, so it keeps short decay and includes market neutralization.",
    }
    return rationales.get(
        mechanism_id,
        "Fallback grid: conservative D1-compatible settings with industry/subindustry neutralization.",
    )


def setting_rationale_for_mechanism(mechanism_id: str, mechanism: dict[str, Any] | None = None) -> str:
    mechanism = mechanism or {}
    profile = mechanism.get("settings_profile")
    if isinstance(profile, dict) and profile.get("rationale"):
        return str(profile.get("rationale")).strip()
    return mechanism_setting_rationale(mechanism_id)


def apply_family_tuning_to_grid(setting_grid: dict[str, list[Any]], family_action: dict[str, Any]) -> dict[str, list[Any]]:
    tuned = {key: list(value) for key, value in setting_grid.items()}
    tuning = family_action.get("tuning_actions") if isinstance(family_action.get("tuning_actions"), dict) else {}
    neutralization_bias = str(tuning.get("neutralization_bias") or "")
    truncation_bias = str(tuning.get("truncation_bias") or "")
    decay_bias = str(tuning.get("decay_bias") or "")
    if neutralization_bias == "broader":
        tuned["neutralization"] = list(dict.fromkeys([*(tuned.get("neutralization") or []), "SECTOR", "MARKET"]))
    if truncation_bias == "tighter":
        current = [float(item) for item in (tuned.get("truncation") or [])]
        tuned["truncation"] = list(dict.fromkeys([0.03, 0.05, *current]))
    if decay_bias == "longer":
        current = [int(item) for item in (tuned.get("decay") or [])]
        tuned["decay"] = list(dict.fromkeys([*current, 12, 20]))
    elif decay_bias == "shorter":
        current = [int(item) for item in (tuned.get("decay") or [])]
        tuned["decay"] = list(dict.fromkeys([0, 2, *current]))
    return tuned


def refine_query_terms(family_action: dict[str, Any]) -> str:
    tuning = family_action.get("tuning_actions") if isinstance(family_action.get("tuning_actions"), dict) else {}
    terms = [str(item) for item in (tuning.get("selection_query_bias_terms") or []) if str(item)]
    avoid = [f"avoid_{item}" for item in (tuning.get("selection_query_avoid_terms") or []) if str(item)]
    return " ".join([*terms, *avoid]).strip()


def field_tokens(field: str) -> list[str]:
    raw_tokens = [token.lower() for token in FIELD_TOKEN_RE.findall(field)]
    expanded: list[str] = []
    for token in raw_tokens:
        expanded.append(token)
        if "_" in token:
            expanded.extend(part for part in token.split("_") if part)
    filtered = [token for token in expanded if token not in {"d0", "d1", "qfv4", "q", "ttm"}]
    return list(dict.fromkeys(filtered)) or [field.lower()]


def required_terms_for_field(field: str, category: str) -> list[str]:
    category_lower = category.lower()
    if category_lower in {"sentiment"}:
        return []
    tokens: list[str] = []
    for token in field_tokens(field):
        if token in GENERIC_FIELD_TOKENS or len(token) < 3:
            continue
        if "_" in token:
            continue
        if re.search(r"\d", token) and token not in {"dcf", "ffo"}:
            continue
        tokens.append(token)
    semantic_terms: list[str] = []
    field_lower = field.lower()
    for keyword in SEMANTIC_KEYWORDS:
        if keyword in field_lower:
            semantic_terms.append(keyword)
    if semantic_terms:
        return list(dict.fromkeys(semantic_terms[:3]))
    if not tokens:
        return []
    return list(dict.fromkeys(tokens[:3]))


def strict_required_terms_for_field(field: str) -> list[str]:
    field_lower = field.lower()
    if field_lower == "distress_risk_measure":
        return ["distress"]
    if field_lower == "credit_risk_premium_indicator":
        return ["credit", "risk"]
    if "systematic_risk" in field_lower:
        return ["systematic", "risk"]
    if "unsystematic_risk" in field_lower:
        return ["unsystematic", "risk"]
    if "correlation" in field_lower:
        return ["correlation"]
    if "beta" in field_lower:
        return ["beta"]
    if "rp_nip" in field_lower:
        return ["news", "impact"]
    if "rp_ess" in field_lower:
        return ["event", "sentiment"]
    if "rp_css" in field_lower:
        return ["composite", "sentiment"]
    return []


def preferred_terms_for_field(field: str, mechanism_id: str) -> list[str]:
    field_lower = field.lower()
    terms: list[str] = []
    if mechanism_id == "credit_recovery_pressure":
        terms.extend(["credit", "risk", "distress", "debt", "repayment", "issuance", "interest"])
    if mechanism_id == "systematic_risk_regime_shift":
        terms.extend(["beta", "correlation", "systematic", "unsystematic", "risk", "variance"])
    if mechanism_id == "footnote_accounting_complexity":
        terms.extend(["deferred", "tax", "accrued", "liability", "goodwill", "footnote"])
    if mechanism_id in {"narrative_valuation_gap", "event_novelty_underreaction", "mna_price_impact_absorption"}:
        terms.extend(["news", "impact", "sentiment", "event", "volume", "price", "mna", "acquisition"])
    terms.extend(strict_required_terms_for_field(field_lower))
    return list(dict.fromkeys(terms))


def blocked_terms_for_field(field: str, category: str) -> list[str]:
    category_lower = category.lower()
    if category_lower in {"sentiment", "model"}:
        tokens = {"actual", "actuals", "currency", "reported"}
    else:
        tokens = set(RAW_SLOT_BLOCKED_TERMS)
    field_lower = field.lower()
    if category_lower in {"analyst", "fundamental", "relation"}:
        tokens.update({"guidance", "reported", "actual", "actuals"})
    if "eps_" in field_lower or field_lower.endswith("_eps") or "_eps_" in field_lower:
        tokens.discard("eps")
    return sorted(tokens)


def constrained_dataset_categories(category: str, mechanism: dict[str, Any]) -> str:
    base = [item.strip() for item in dataset_categories_for_category(category).split(",") if item.strip()]
    allowed = list_value(mechanism, "allowed_dataset_categories")
    blocked = set(list_value(mechanism, "blocked_dataset_categories"))
    # Slot semantics are stricter than mechanism-wide allow-lists. A broad
    # event mechanism may allow news/sentiment globally, but a fundamental
    # placeholder must still be allowed to search fundamental/model fields.
    if category in {"fundamental", "analyst", "model"}:
        categories = [item for item in base if item not in blocked]
    else:
        categories = [item for item in (allowed or base) if item not in blocked]
    if not categories:
        categories = [item for item in base if item not in blocked] or base
    return ",".join(dict.fromkeys(categories))


def build_slot_specs(
    seed: RawAlphaSeed,
    mechanism: dict[str, Any] | None = None,
    dataset_atlas: list[dict[str, Any]] | None = None,
    dataset_cards: dict[str, dict[str, Any]] | None = None,
    dataset_lane_index: int = 0,
    dataset_lane_count: int = 1,
    target_delay: int | None = None,
    target_universes: list[str] | None = None,
    family_action: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    mechanism = mechanism or {}
    dataset_atlas = dataset_atlas or []
    dataset_cards = dataset_cards or {}
    specs: list[dict[str, Any]] = []
    for idx, field in enumerate(seed.fields):
        slot = f"FIELD_{chr(ord('A') + idx)}"
        category = infer_field_category(field)
        search_terms = " ".join(dict.fromkeys([*seed.domain.lower().split("+"), *field_tokens(field)]))
        selection_query = " ".join(
            part for part in [seed.family.replace("_", " "), seed.domain.replace("+", " "), search_terms] if part
        ).strip()
        atlas_datasets = choose_atlas_datasets(
            atlas=dataset_atlas,
            seed=seed,
            mechanism=mechanism,
            slot_category=category,
            seed_field=field,
            selection_query=selection_query,
            target_delay=target_delay,
            target_universes=target_universes,
            dataset_cards=dataset_cards,
            family_action=family_action,
            limit=max(1, dataset_lane_count),
        )
        atlas_dataset = atlas_datasets[dataset_lane_index % len(atlas_datasets)] if atlas_datasets else None
        dataset_id = str((atlas_dataset or {}).get("id") or "")
        matched_scope = (atlas_dataset or {}).get("_matched_scope") or {}
        dataset_card = dataset_cards.get(dataset_id) or {}
        mechanism_id = mechanism_id_for_seed(seed, mechanism)
        effective_coverage = dataset_effective_coverage(atlas_dataset or {}, dataset_card)
        min_required_coverage = min_slot_coverage_for_seed(seed, mechanism_id, category)
        field_semantic_tags = field_semantic_tags_for_dataset(atlas_dataset or {}, dataset_card, field, mechanism_id)
        required_semantic_tags = mechanism_required_semantic_tags(mechanism_id)
        semantic_overlap = [tag for tag in field_semantic_tags if tag in set(required_semantic_tags)]
        card_terms = dataset_card.get("top_field_terms") or []
        if isinstance(card_terms, list):
            card_term_text = " ".join(str(term) for term in card_terms[:10])
        else:
            card_term_text = ""
        dataset_selection_query = " ".join(
            part
            for part in [
                selection_query,
                str((atlas_dataset or {}).get("name") or ""),
                str((atlas_dataset or {}).get("description") or ""),
                card_term_text,
            ]
            if part
        ).strip()
        specs.append(
            {
                "slot": slot,
                "seed_field": field,
                "category": category,
                "dataset_categories": constrained_dataset_categories(category, mechanism),
                "selection_query": dataset_selection_query or selection_query,
                "dataset_id": dataset_id,
                "dataset_name": str((atlas_dataset or {}).get("name") or ""),
                "dataset_description": str((atlas_dataset or {}).get("description") or ""),
                "dataset_matched_scope": matched_scope,
                "dataset_scope_delay": matched_scope.get("delay") if isinstance(matched_scope, dict) else None,
                "dataset_scope_universe": matched_scope.get("universe") if isinstance(matched_scope, dict) else None,
                "dataset_exploration_score": (atlas_dataset or {}).get("exploration_score"),
                "dataset_markdown_summary": str((atlas_dataset or {}).get("markdown_summary") or ""),
                "dataset_novelty_tier": str(dataset_card.get("novelty_tier") or ""),
                "dataset_effective_coverage": effective_coverage,
                "min_coverage": round(min_required_coverage, 4),
                "dataset_median_alpha_count": dataset_card.get("median_alpha_count"),
                "dataset_median_user_count": dataset_card.get("median_user_count"),
                "field_semantic_tags": field_semantic_tags,
                "mechanism_required_semantic_tags": required_semantic_tags,
                "semantic_tag_overlap": semantic_overlap,
            }
        )
    return specs


def event_safe_placeholder(slot: str, mechanism_id: str) -> str:
    return f"{{{{{slot}}}}}"


def build_slotized_expression(
    seed: RawAlphaSeed,
    slot_specs: list[dict[str, Any]],
    mechanism: dict[str, Any] | None = None,
) -> str:
    expression = seed.expression
    mechanism_id = mechanism_id_for_seed(seed, mechanism or {})
    for spec in sorted(slot_specs, key=lambda item: len(str(item["seed_field"])), reverse=True):
        field = str(spec["seed_field"])
        slot = str(spec["slot"])
        expression = re.sub(rf"\b{re.escape(field)}\b", event_safe_placeholder(slot, mechanism_id), expression)
    return expression


def build_slot_selection_template(
    seed: RawAlphaSeed,
    slot_specs: list[dict[str, Any]],
    target_slot: str,
    mechanism: dict[str, Any] | None = None,
) -> dict[str, Any]:
    mechanism = mechanism or {}
    expression = seed.expression
    mechanism_id = mechanism_id_for_seed(seed, mechanism)
    for spec in sorted(slot_specs, key=lambda item: len(str(item["seed_field"])), reverse=True):
        replacement = "{{FIELD}}" if spec["slot"] == target_slot else spec["seed_field"]
        expression = re.sub(rf"\b{re.escape(str(spec['seed_field']))}\b", replacement, expression)
    target_spec = next(spec for spec in slot_specs if spec["slot"] == target_slot)
    target_field = str(target_spec["seed_field"])
    selector_universe = str(target_spec.get("dataset_scope_universe") or "TOP3000")
    selector_delay = int(target_spec.get("dataset_scope_delay") or 1)
    return {
        "name": f"raw_alpha_slot_{seed.family}_{target_slot.lower()}",
        "type": "REGULAR",
        "category": normalize_job_category(seed).upper(),
        "tags": ["raw_alpha", "slot_inventory", seed.family, target_spec["category"], mechanism_id],
        "description": f"Slot inventory selector for {seed.family} {target_slot}",
        "research_mechanism": {
            "mechanism_id": mechanism_id,
            "data_family": seed.data_family or ",".join(list_value(mechanism, "data_families")),
            "expression_family": seed.expression_family or ",".join(list_value(mechanism, "expression_families")),
        },
        "settings": {
            "instrumentType": "EQUITY",
            "region": "USA",
            "universe": selector_universe,
            "delay": selector_delay,
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
        "field_selection": {
            "dataset_hint": target_spec["category"],
            "allowed_types": ["MATRIX"],
            "blocked_types": ["EVENT", "GROUP"],
            "required_terms": required_terms_for_field(target_field, str(target_spec["category"])),
            "required_all_terms": strict_required_terms_for_field(target_field),
            "preferred_terms": preferred_terms_for_field(target_field, mechanism_id),
            "blocked_terms": blocked_terms_for_field(
                target_field,
                str(target_spec["category"]),
            ),
            "prefer_low_usage": True,
        },
        "expression": expression,
    }


def build_base_template(seed: RawAlphaSeed, slot_specs: list[dict[str, Any]], mechanism: dict[str, Any] | None = None) -> dict[str, Any]:
    mechanism = mechanism or {}
    category = normalize_job_category(seed)
    expression = build_slotized_expression(seed, slot_specs, mechanism)
    tag_tokens = [token for token in re.split(r"[^A-Za-z0-9_]+", seed.domain) if token]
    mechanism_id = mechanism_id_for_seed(seed, mechanism)
    expression_family = seed.expression_family or (list_value(mechanism, "expression_families") or [""])[0]
    data_family = seed.data_family or ",".join(list_value(mechanism, "data_families"))
    tags = ["raw_alpha", "research_seed", category, *tag_tokens, seed.family, mechanism_id, expression_family]
    return {
        "name": f"raw_alpha_{seed.family}",
        "type": "REGULAR",
        "category": category.upper(),
        "tags": list(dict.fromkeys(tags)),
        "description": seed.rationale or seed.family,
        "research_mechanism": {
            "mechanism_id": mechanism_id,
            "hypothesis": str(mechanism.get("hypothesis") or ""),
            "data_family": data_family,
            "expression_family": expression_family,
            "anti_correlation_target": seed.anti_correlation_target
            or ",".join(list_value(mechanism, "anti_correlation_targets")),
        },
        "settings": {
            "instrumentType": "EQUITY",
            "region": "USA",
            "universe": "TOP3000",
            "delay": delay_variants_for_seed(seed)[0],
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
        "field_selection": {
            "dataset_hint": slot_specs[0]["category"] if slot_specs else category,
            "allowed_types": ["MATRIX"],
            "blocked_types": ["EVENT", "GROUP"],
            "blocked_terms": list_value(mechanism, "blocked_field_terms"),
            "prefer_low_usage": True,
        },
        "expression": expression,
    }


def write_yaml(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8")


def clear_family_dir(path: Path) -> None:
    if not path.exists():
        return
    for child in path.iterdir():
        if child.is_file():
            child.unlink()


def build_raw_alpha_rotation_jobs(config: dict) -> tuple[list[dict[str, Any]], list[str], dict[str, Any]]:
    rotation_cfg = config.get("raw_alpha_rotation") or {}
    if not rotation_cfg.get("enabled", False):
        return [], [], {}

    source_path = ROOT_DIR / str(rotation_cfg.get("source_file") or RAW_ALPHA_POOL_PATH.relative_to(ROOT_DIR))
    mechanism_bank_path = ROOT_DIR / str(rotation_cfg.get("mechanism_bank_file") or MECHANISM_BANK_PATH.relative_to(ROOT_DIR))
    entries = load_raw_alpha_pool(source_path)
    mechanism_bank = load_research_mechanism_bank(mechanism_bank_path)
    dataset_atlas = load_dataset_atlas()
    dataset_cards = load_dataset_knowledge_cards()
    family_actions = load_raw_family_actions()
    family_clusters = load_raw_family_clusters()
    previous_selected = load_previous_selected_families()
    raw_family_archive = load_raw_family_archive()
    used_families = {str(item) for item in (raw_family_archive.get("used_families") or []) if str(item)}
    excluded_families = set(used_families) - set(previous_selected)
    selected, selection_summary = choose_rotating_raw_alphas(
        entries=entries,
        rotation_hours=int(rotation_cfg.get("rotation_hours", 48)),
        active_count=int(rotation_cfg.get("active_families_per_window", 2)),
        min_completed_before_switch=int(rotation_cfg.get("min_completed_before_switch", 0)),
        max_completed_before_switch=int(rotation_cfg.get("max_completed_before_switch", 8000)),
        high_grade_bonus_completed_before_switch=int(rotation_cfg.get("high_grade_bonus_completed_before_switch", 10000)),
        recent_quality_window=int(rotation_cfg.get("recent_quality_window", 1500)),
        small_pool_min_slot_count=int(rotation_cfg.get("small_pool_min_slot_count", 0)),
        small_pool_completed_floor=int(rotation_cfg.get("small_pool_completed_floor", 0)),
        family_actions=family_actions,
        family_clusters=family_clusters,
        excluded_families=excluded_families,
        allowed_cluster_statuses={
            str(item).strip()
            for item in (rotation_cfg.get("allowed_cluster_statuses") or ["primary", "candidate"])
            if str(item).strip()
        },
    )
    if not selected:
        return [], [], {**selection_summary, "jobs": []}
    retired_previous = [family for family in previous_selected if family not in {seed.family for seed in selected}]
    if retired_previous:
        raw_family_archive = archive_used_raw_families(retired_previous)

    templates_written: list[str] = []
    jobs: list[dict[str, Any]] = []
    per_family_limit = int(rotation_cfg.get("selection_limit_per_slot", 240))
    per_family_fetch_limit = int(rotation_cfg.get("inventory_fetch_limit_per_slot", 1200))
    per_dataset_limit = int(rotation_cfg.get("per_dataset_limit", 220))
    max_datasets = int(rotation_cfg.get("max_datasets", 10))
    max_pair_field_maps = int(rotation_cfg.get("max_pair_field_maps", 1400))
    dataset_lanes_per_family = max(1, int(rotation_cfg.get("dataset_lanes_per_family", 3)))

    family_summaries: list[dict[str, Any]] = []
    for seed in selected:
        family_action = family_actions.get(seed.family) or {}
        action_decision = str(family_action.get("decision") or "").lower()
        action_lane_target = int(family_action.get("dataset_lanes_target") or 0)
        family_lane_count = dataset_lanes_per_family
        if action_decision in {"promote", "refine", "observe"} and action_lane_target > 0:
            family_lane_count = max(1, action_lane_target)
        mechanism = mechanism_for_seed(seed, mechanism_bank)
        mechanism_id = mechanism_id_for_seed(seed, mechanism)
        family_setting_grid = apply_family_tuning_to_grid(mechanism_setting_grid(mechanism_id, seed, mechanism), family_action)
        family_delay_variants = [int(item) for item in family_setting_grid.get("delay") or delay_variants_for_seed(seed)]
        family_universe_variants = list(family_setting_grid.get("universe") or ["TOP3000"])
        expression_family = seed.expression_family or (list_value(mechanism, "expression_families") or [""])[0]
        data_family = seed.data_family or ",".join(list_value(mechanism, "data_families"))
        anti_correlation_target = seed.anti_correlation_target or ",".join(list_value(mechanism, "anti_correlation_targets"))
        family_dir = SUPPLY_TEMPLATE_ROOT / seed.family
        clear_family_dir(family_dir)
        family_job_summaries: list[dict[str, Any]] = []
        for lane_index in range(family_lane_count):
            slot_specs = build_slot_specs(
                seed,
                mechanism,
                dataset_atlas=dataset_atlas,
                dataset_cards=dataset_cards,
                dataset_lane_index=lane_index,
                dataset_lane_count=family_lane_count,
                target_delay=family_delay_variants[0] if family_delay_variants else 1,
                target_universes=family_universe_variants,
                family_action=family_action,
            )
            extra_query_terms = refine_query_terms(family_action)
            if extra_query_terms:
                for spec in slot_specs:
                    spec["selection_query"] = f"{spec.get('selection_query') or ''} {extra_query_terms}".strip()
            lane_dataset_ids = [str(spec.get("dataset_id") or "") for spec in slot_specs if spec.get("dataset_id")]
            if lane_index > 0 and not lane_dataset_ids:
                continue
            lane_suffix = f"lane{lane_index + 1}"
            base_template = build_base_template(seed, slot_specs, mechanism)
            base_template["name"] = f"{base_template['name']}_{lane_suffix}"
            base_relative = (family_dir / f"base_{lane_suffix}.yaml").relative_to(ROOT_DIR)
            write_yaml(ROOT_DIR / base_relative, base_template)
            templates_written.append(str(base_relative).replace("\\", "/"))

            slot_inventory_cfg: dict[str, Any] = {}
            for spec in slot_specs:
                slot_template = build_slot_selection_template(seed, slot_specs, str(spec["slot"]), mechanism)
                slot_template["name"] = f"{slot_template['name']}_{lane_suffix}"
                slot_relative = (family_dir / f"{lane_suffix}_{str(spec['slot']).lower()}_selector.yaml").relative_to(ROOT_DIR)
                write_yaml(ROOT_DIR / slot_relative, slot_template)
                templates_written.append(str(slot_relative).replace("\\", "/"))
                inventory_name = f"raw_{seed.family}_{lane_suffix}_{str(spec['slot']).lower()}"
                slot_inventory_cfg[str(spec["slot"])] = {
                    "inventory_name": inventory_name,
                    "template": str(slot_relative).replace("\\", "/"),
                    "refresh_inventory": True,
                    "disable_related_hydration": True,
                    "rebuild_if_hydrated": True,
                    "inventory_stale_after_minutes": int(rotation_cfg.get("inventory_stale_after_minutes", 240)),
                    "instrument_type": "EQUITY",
                    "region": "USA",
                    "delay": int(spec.get("dataset_scope_delay") or 1),
                    "universe": str(spec.get("dataset_scope_universe") or "TOP3000"),
                    "limit": per_family_fetch_limit,
                    "inventory_fetch_limit": per_family_fetch_limit,
                    "selection_limit": per_family_limit,
                    "discover_datasets": False if str(spec.get("dataset_id") or "") else True,
                    "dataset_categories": str(spec["dataset_categories"]),
                    "dataset_id": str(spec.get("dataset_id") or ""),
                    "dataset_name": str(spec.get("dataset_name") or ""),
                    "max_datasets": max_datasets,
                    "per_dataset_limit": per_dataset_limit,
                    "selection_query": str(spec["selection_query"]),
                    "min_selection_score": rotation_cfg.get("min_selection_score_per_slot"),
                    "min_coverage": spec.get("min_coverage"),
                    "category": str(spec["category"]),
                }

            category = normalize_job_category(seed)
            setting_grid = mechanism_setting_grid(mechanism_id, seed, mechanism)
            setting_grid = apply_family_tuning_to_grid(setting_grid, family_action)
            delay_variants = [int(item) for item in setting_grid.get("delay") or delay_variants_for_seed(seed)]
            global_neutralizations = list(rotation_cfg.get("supply_neutralization_variants") or [])
            mechanism_neutralizations = list(setting_grid.get("neutralization") or ["INDUSTRY", "SUBINDUSTRY"])
            neutralization_variants = list(dict.fromkeys([*mechanism_neutralizations, *global_neutralizations]))
            job = {
            "name": f"raw_seed_{seed.family}_{lane_suffix}",
            "inventory_name": f"raw_{seed.family}",
            "template": str(base_relative).replace("\\", "/"),
            "refresh_inventory": True,
            "disable_related_hydration": True,
            "rebuild_if_hydrated": True,
            "inventory_stale_after_minutes": int(rotation_cfg.get("inventory_stale_after_minutes", 240)),
            "category": category,
            "limit": per_family_fetch_limit,
            "inventory_fetch_limit": per_family_fetch_limit,
            "supply_fetch_limit": int(rotation_cfg.get("supply_fetch_limit", per_family_fetch_limit * 2)),
            "supply_selection_limit": int(rotation_cfg.get("supply_selection_limit", per_family_limit * 2)),
            "discover_datasets": True,
            "dataset_categories": ",".join(
                sorted(
                    {
                        item
                        for spec in slot_specs
                        for item in str(spec.get("dataset_categories") or "").split(",")
                        if item
                    }
                    | {category}
                )
            ),
            "max_datasets": max_datasets,
            "per_dataset_limit": per_dataset_limit,
            "max_pair_field_maps": max_pair_field_maps,
            "supply_neutralization_variants": neutralization_variants,
            "supply_delay_variants": delay_variants,
            "supply_decay_variants": list(rotation_cfg.get("supply_decay_variants") or setting_grid.get("decay") or []),
            "supply_truncation_variants": list(rotation_cfg.get("supply_truncation_variants") or setting_grid.get("truncation") or []),
            "supply_universe_variants": list(rotation_cfg.get("supply_universe_variants") or setting_grid.get("universe") or []),
            "inventory_reuse_min_remaining": int(rotation_cfg.get("inventory_reuse_min_remaining", 240)),
            "selection_query": f"{seed.family.replace('_', ' ')} {seed.domain.replace('+', ' ')}",
            "selection_limit": int(rotation_cfg.get("selection_limit", per_family_limit * 2)),
            "min_selection_score": rotation_cfg.get("min_selection_score"),
            "min_coverage": min((float(spec.get("min_coverage") or 0.0) for spec in slot_specs), default=0.0) or None,
            "max_workers": 1,
            "top": int(rotation_cfg.get("top", 15)),
            "check_retries": 0,
            "fast_local_only": True,
            "slot_inventories": slot_inventory_cfg,
            "raw_alpha_family": seed.family,
            "raw_alpha_source": seed.source,
            "raw_alpha_delay_assessment": seed.delay,
            "raw_alpha_profile": seed.profile,
            "raw_alpha_domain": seed.domain,
            "raw_alpha_rationale": seed.rationale,
            "raw_family_action_decision": action_decision,
            "raw_family_resource_action": str(family_action.get("resource_action") or ""),
            "raw_family_refine_focus": list(family_action.get("refine_focus") or []),
            "mechanism_id": mechanism_id,
            "research_mechanism_id": mechanism_id,
            "data_family": data_family,
            "expression_family": expression_family,
            "anti_correlation_target": anti_correlation_target,
            "mechanism_hypothesis": str(mechanism.get("hypothesis") or ""),
            "setting_rationale": setting_rationale_for_mechanism(mechanism_id, mechanism),
            "blocked_dataset_categories": list_value(mechanism, "blocked_dataset_categories"),
            "allowed_dataset_categories": list_value(mechanism, "allowed_dataset_categories"),
            "field_slot_count": len(slot_specs),
            "estimated_field_map_cap": max_pair_field_maps,
            "dataset_atlas_enabled": bool(dataset_atlas),
            "slot_dataset_plan": [
                {
                    "slot": spec.get("slot"),
                    "seed_field": spec.get("seed_field"),
                    "category": spec.get("category"),
                    "dataset_id": spec.get("dataset_id"),
                    "dataset_name": spec.get("dataset_name"),
                    "dataset_exploration_score": spec.get("dataset_exploration_score"),
                    "dataset_novelty_tier": spec.get("dataset_novelty_tier"),
                    "dataset_effective_coverage": spec.get("dataset_effective_coverage"),
                    "min_coverage": spec.get("min_coverage"),
                    "dataset_matched_scope": spec.get("dataset_matched_scope"),
                    "dataset_scope_delay": spec.get("dataset_scope_delay"),
                    "dataset_scope_universe": spec.get("dataset_scope_universe"),
                    "dataset_median_alpha_count": spec.get("dataset_median_alpha_count"),
                    "dataset_median_user_count": spec.get("dataset_median_user_count"),
                    "field_semantic_tags": spec.get("field_semantic_tags"),
                    "mechanism_required_semantic_tags": spec.get("mechanism_required_semantic_tags"),
                    "semantic_tag_overlap": spec.get("semantic_tag_overlap"),
                    "dataset_markdown_summary": spec.get("dataset_markdown_summary"),
                }
                for spec in slot_specs
            ],
        }
            jobs.append(job)
            family_job_summaries.append(
                {
                "family": seed.family,
                "lane": lane_suffix,
                "source": seed.source,
                "delay": seed.delay,
                "profile": seed.profile,
                "domain": seed.domain,
                "mechanism_id": mechanism_id,
                "data_family": data_family,
                "expression_family": expression_family,
                "anti_correlation_target": anti_correlation_target,
                "slot_count": len(slot_specs),
                "estimated_field_map_cap": max_pair_field_maps,
                "setting_grid": setting_grid,
                "setting_rationale": setting_rationale_for_mechanism(mechanism_id, mechanism),
                "raw_family_action_decision": action_decision,
                "raw_family_resource_action": str(family_action.get("resource_action") or ""),
                "raw_family_refine_focus": list(family_action.get("refine_focus") or []),
                "slot_dataset_plan": [
                    {
                        "slot": spec.get("slot"),
                        "seed_field": spec.get("seed_field"),
                        "category": spec.get("category"),
                        "dataset_id": spec.get("dataset_id"),
                        "dataset_name": spec.get("dataset_name"),
                        "dataset_exploration_score": spec.get("dataset_exploration_score"),
                        "dataset_novelty_tier": spec.get("dataset_novelty_tier"),
                        "dataset_effective_coverage": spec.get("dataset_effective_coverage"),
                        "min_coverage": spec.get("min_coverage"),
                        "dataset_matched_scope": spec.get("dataset_matched_scope"),
                        "dataset_scope_delay": spec.get("dataset_scope_delay"),
                        "dataset_scope_universe": spec.get("dataset_scope_universe"),
                        "dataset_median_alpha_count": spec.get("dataset_median_alpha_count"),
                        "dataset_median_user_count": spec.get("dataset_median_user_count"),
                        "field_semantic_tags": spec.get("field_semantic_tags"),
                        "mechanism_required_semantic_tags": spec.get("mechanism_required_semantic_tags"),
                        "semantic_tag_overlap": spec.get("semantic_tag_overlap"),
                        "dataset_markdown_summary": spec.get("dataset_markdown_summary"),
                    }
                    for spec in slot_specs
                ],
            }
            )
        family_summaries.extend(family_job_summaries)

    summary = {
        **selection_summary,
        "jobs": family_summaries,
        "source_file": str(source_path.relative_to(ROOT_DIR)),
        "mechanism_bank_file": str(mechanism_bank_path.relative_to(ROOT_DIR)),
        "mechanism_count": len(mechanism_bank),
        "dataset_atlas_enabled": bool(dataset_atlas),
        "dataset_atlas_dataset_count": len(dataset_atlas),
        "family_action_count": len(family_actions),
        "family_cluster_count": len(family_clusters),
        "raw_family_archive": {
            "path": str(RAW_FAMILY_ARCHIVE_PATH.relative_to(ROOT_DIR)),
            "used_family_count": len(raw_family_archive.get("used_families") or []),
            "retired_previous_families": retired_previous,
        },
    }
    return jobs, templates_written, summary
