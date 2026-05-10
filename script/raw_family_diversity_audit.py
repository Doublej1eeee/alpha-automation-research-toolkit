#!/usr/bin/env python
"""Audit raw-alpha family distinctness and maintain rotation cluster state.

The audit is intentionally family-level. It does not judge child alphas inside a
48-hour rotation window; those are expected to be similar while the miner searches
for the strongest variant of one raw idea.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from script.raw_alpha_rotation import RawAlphaSeed, load_raw_alpha_pool  # noqa: E402


OUTPUT_PATH = ROOT_DIR / "result_store" / "analysis" / "raw_family_diversity_audit.json"
CLUSTER_STATE_PATH = ROOT_DIR / "result_store" / "analysis" / "raw_family_clusters.json"
FALLBACK_DIR = ROOT_DIR / "temp" / "raw_family_audit"
TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
OPERATOR_RE = re.compile(r"\b([A-Za-z_][A-Za-z0-9_]*)\s*\(")
GENERIC_PROFILE = "research_catalog_single_slot"
PRIMARY_PROFILE = "multi_dataset"
DEFAULT_SIMILARITY_THRESHOLD = 0.62
DEFAULT_MERGE_THRESHOLD = 0.85
DEFAULT_FREEZE_THRESHOLD = 0.95
PRIMARY_SCORE_THRESHOLD = 75
PRIMARY_MIN_FIELD_COUNT = 2
GENERIC_SEED_FIELDS = {
    "analyst_revision",
    "news_event_attention",
    "fundamental_quality_pressure",
    "credit_distress_pressure",
    "liquidity_volume_shock",
    "systematic_risk_shift",
    "footnote_accounting_pressure",
    "research_signal",
}


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def tokens(value: str) -> set[str]:
    return {token.lower() for token in TOKEN_RE.findall(value or "")}


def jaccard(left: set[str], right: set[str]) -> float:
    if not left and not right:
        return 0.0
    return len(left & right) / max(1, len(left | right))


def expression_ops(expression: str) -> list[str]:
    return [match.group(1).lower() for match in OPERATOR_RE.finditer(expression or "")]


def expression_skeleton(expression: str) -> str:
    return ">".join(expression_ops(expression))


def seed_profile(seed: RawAlphaSeed) -> dict[str, Any]:
    fields = [field.strip().lower() for field in seed.fields if field.strip()]
    return {
        "family": seed.family,
        "source": seed.source,
        "profile": seed.profile,
        "mechanism_id": seed.mechanism_id,
        "data_family": seed.data_family,
        "expression_family": seed.expression_family,
        "anti_correlation_target": seed.anti_correlation_target,
        "domain_tokens": tokens(seed.domain),
        "data_tokens": tokens(seed.data_family),
        "field_tokens": set().union(*(tokens(field) for field in fields)) if fields else set(),
        "fields": fields,
        "operators": set(expression_ops(seed.expression)),
        "expression_skeleton": expression_skeleton(seed.expression),
        "expression_tokens": tokens(seed.expression),
    }


def similarity(left: dict[str, Any], right: dict[str, Any]) -> float:
    score = 0.0
    score += 0.24 * jaccard(left["field_tokens"], right["field_tokens"])
    score += 0.18 * jaccard(left["operators"], right["operators"])
    score += 0.16 * jaccard(left["domain_tokens"], right["domain_tokens"])
    score += 0.12 * jaccard(left["data_tokens"], right["data_tokens"])
    score += 0.10 * jaccard(left["expression_tokens"], right["expression_tokens"])
    if left["mechanism_id"] and left["mechanism_id"] == right["mechanism_id"]:
        score += 0.10
    if left["expression_family"] and left["expression_family"] == right["expression_family"]:
        score += 0.05
    if left["anti_correlation_target"] and left["anti_correlation_target"] == right["anti_correlation_target"]:
        score += 0.03
    if left["expression_skeleton"] and left["expression_skeleton"] == right["expression_skeleton"]:
        score += 0.02
    return round(min(score, 1.0), 6)


def family_score(seed: RawAlphaSeed, profile: dict[str, Any], counters: dict[str, Counter[str]]) -> tuple[int, list[str]]:
    score = 0
    reasons: list[str] = []
    field_count = len(profile["fields"])
    generic_fields = [field for field in profile["fields"] if field in GENERIC_SEED_FIELDS]

    if seed.profile == PRIMARY_PROFILE:
        score += 18
        reasons.append("multi_dataset profile")
    elif seed.profile == GENERIC_PROFILE:
        reasons.append("single-slot research profile")

    if counters["mechanism"][seed.mechanism_id] <= 1:
        score += 20
        reasons.append("unique mechanism")
    else:
        score += 8

    if counters["data_family"][seed.data_family] <= 1:
        score += 18
        reasons.append("unique data family")
    else:
        score += 6

    if counters["expression_family"][seed.expression_family] <= 1:
        score += 15
        reasons.append("unique expression family")
    else:
        score += 5

    if counters["anti_correlation_target"][seed.anti_correlation_target] <= 1:
        score += 10
        reasons.append("unique anti-correlation target")
    else:
        score += 3

    if field_count >= 4:
        score += 14
        reasons.append("specific multi-field seed")
    elif field_count >= PRIMARY_MIN_FIELD_COUNT:
        score += 7
    elif generic_fields:
        reasons.append("generic single-field placeholder")

    if counters["expression_skeleton"][profile["expression_skeleton"]] <= 1:
        score += 5
    elif seed.profile == GENERIC_PROFILE:
        reasons.append("common expression skeleton")

    return min(score, 100), reasons


def load_existing_cluster_state(path: Path = CLUSTER_STATE_PATH) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def existing_family_rows(payload: dict[str, Any]) -> dict[str, dict[str, Any]]:
    rows = payload.get("families") if isinstance(payload, dict) else {}
    if isinstance(rows, dict):
        return {str(key): value for key, value in rows.items() if isinstance(value, dict)}
    if isinstance(rows, list):
        return {
            str(row.get("family") or ""): row
            for row in rows
            if isinstance(row, dict) and row.get("family")
        }
    return {}


def primary_anchor_for(profile: dict[str, Any], primary_profiles: list[dict[str, Any]]) -> tuple[str, float]:
    best_family = ""
    best_score = 0.0
    for anchor in primary_profiles:
        score = similarity(profile, anchor)
        if score > best_score:
            best_family = str(anchor["family"])
            best_score = score
    return best_family, round(best_score, 6)


def build(
    similarity_threshold: float = DEFAULT_SIMILARITY_THRESHOLD,
    merge_threshold: float = DEFAULT_MERGE_THRESHOLD,
    freeze_threshold: float = DEFAULT_FREEZE_THRESHOLD,
    preserve_manual_status: bool = True,
) -> dict[str, Any]:
    seeds = load_raw_alpha_pool()
    profiles = [seed_profile(seed) for seed in seeds]
    by_family = {seed.family: seed for seed in seeds}
    profile_by_family = {profile["family"]: profile for profile in profiles}
    counters = {
        "mechanism": Counter(seed.mechanism_id for seed in seeds),
        "data_family": Counter(seed.data_family for seed in seeds),
        "expression_family": Counter(seed.expression_family for seed in seeds),
        "anti_correlation_target": Counter(seed.anti_correlation_target for seed in seeds),
        "field_signature": Counter(",".join(profile["fields"]) for profile in profiles),
        "expression_skeleton": Counter(profile["expression_skeleton"] for profile in profiles),
    }

    scores: dict[str, dict[str, Any]] = {}
    for family, seed in by_family.items():
        score, reasons = family_score(seed, profile_by_family[family], counters)
        scores[family] = {"score": score, "reasons": reasons}

    pairs: list[dict[str, Any]] = []
    risk = Counter()
    for index, left in enumerate(profiles):
        for right in profiles[index + 1 :]:
            score = similarity(left, right)
            if score < similarity_threshold:
                continue
            row = {
                "left": left["family"],
                "right": right["family"],
                "similarity": score,
                "same_mechanism": left["mechanism_id"] == right["mechanism_id"],
                "same_expression_family": left["expression_family"] == right["expression_family"],
                "same_anti_correlation_target": left["anti_correlation_target"] == right["anti_correlation_target"],
            }
            pairs.append(row)
            risk[left["family"]] += 1
            risk[right["family"]] += 1
    pairs.sort(key=lambda row: (-float(row["similarity"]), row["left"], row["right"]))

    primary_profiles = [
        profile
        for profile in profiles
        if scores[str(profile["family"])]["score"] >= PRIMARY_SCORE_THRESHOLD
        and len(profile["fields"]) >= PRIMARY_MIN_FIELD_COUNT
    ]
    if not primary_profiles:
        primary_profiles = [profile for profile in profiles if profile.get("profile") == PRIMARY_PROFILE]

    previous = existing_family_rows(load_existing_cluster_state())
    families: dict[str, dict[str, Any]] = {}
    merged_count = 0
    frozen_count = 0
    primary_count = 0
    candidate_count = 0
    for seed in seeds:
        family = seed.family
        profile = profile_by_family[family]
        score = int(scores[family]["score"])
        anchor, anchor_similarity = primary_anchor_for(profile, [item for item in primary_profiles if item["family"] != family])
        max_pair_similarity = max(
            [
                float(row["similarity"])
                for row in pairs
                if row["left"] == family or row["right"] == family
            ]
            or [0.0]
        )
        if score >= PRIMARY_SCORE_THRESHOLD and seed.profile == PRIMARY_PROFILE:
            status = "primary"
            primary_count += 1
        elif max_pair_similarity >= freeze_threshold and anchor:
            status = "frozen"
            frozen_count += 1
        elif max_pair_similarity >= merge_threshold and anchor:
            status = "merged"
            merged_count += 1
        else:
            status = "candidate"
            candidate_count += 1

        previous_status = str((previous.get(family) or {}).get("status") or "")
        if preserve_manual_status and previous_status in {"primary", "candidate", "merged", "frozen"}:
            status = previous_status

        families[family] = {
            "family": family,
            "status": status,
            "score": score,
            "profile": seed.profile,
            "mechanism_id": seed.mechanism_id,
            "data_family": seed.data_family,
            "expression_family": seed.expression_family,
            "anti_correlation_target": seed.anti_correlation_target,
            "field_count": len(profile["fields"]),
            "field_signature": ",".join(profile["fields"]),
            "expression_skeleton": profile["expression_skeleton"],
            "nearest_primary_family": anchor,
            "nearest_primary_similarity": anchor_similarity,
            "max_similarity": round(max_pair_similarity, 6),
            "similar_pair_count": int(risk[family]),
            "reasons": scores[family]["reasons"],
        }

    status_counts = Counter(row["status"] for row in families.values())
    status_counts.setdefault("primary", primary_count)
    status_counts.setdefault("candidate", candidate_count)
    status_counts.setdefault("merged", merged_count)
    status_counts.setdefault("frozen", frozen_count)

    return {
        "schema_version": 1,
        "generated_at": utc_now(),
        "purpose": "Family-level raw alpha distinctness gate for two-day raw-family rotation.",
        "thresholds": {
            "similarity": similarity_threshold,
            "merge": merge_threshold,
            "freeze": freeze_threshold,
            "primary_score": PRIMARY_SCORE_THRESHOLD,
        },
        "family_count": len(seeds),
        "status_counts": dict(status_counts.most_common()),
        "counters": {
            key: dict(value.most_common())
            for key, value in counters.items()
            if key != "expression_skeleton"
        },
        "similar_pair_count": len(pairs),
        "similar_pairs": pairs[:200],
        "family_similarity_risk": dict(risk.most_common()),
        "families": families,
    }


def write_outputs(payload: dict[str, Any], update_cluster_state: bool = True) -> None:
    output_path = OUTPUT_PATH
    try:
        OUTPUT_PATH.parent.mkdir(parents=True, exist_ok=True)
        OUTPUT_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    except PermissionError:
        FALLBACK_DIR.mkdir(parents=True, exist_ok=True)
        output_path = FALLBACK_DIR / OUTPUT_PATH.name
        output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    if update_cluster_state:
        cluster_payload = {
            "schema_version": 1,
            "generated_at": payload.get("generated_at"),
            "purpose": payload.get("purpose"),
            "thresholds": payload.get("thresholds"),
            "status_counts": payload.get("status_counts"),
            "families": payload.get("families") or {},
        }
        try:
            CLUSTER_STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
            CLUSTER_STATE_PATH.write_text(json.dumps(cluster_payload, ensure_ascii=False, indent=2), encoding="utf-8")
        except PermissionError:
            (FALLBACK_DIR / CLUSTER_STATE_PATH.name).write_text(
                json.dumps(cluster_payload, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
    payload["_written_audit_path"] = str(output_path)


def main() -> None:
    parser = argparse.ArgumentParser(description="Audit raw-family distinctness and update cluster state.")
    parser.add_argument("--similarity-threshold", type=float, default=DEFAULT_SIMILARITY_THRESHOLD)
    parser.add_argument("--merge-threshold", type=float, default=DEFAULT_MERGE_THRESHOLD)
    parser.add_argument("--freeze-threshold", type=float, default=DEFAULT_FREEZE_THRESHOLD)
    parser.add_argument("--no-cluster-update", action="store_true")
    args = parser.parse_args()
    payload = build(
        similarity_threshold=args.similarity_threshold,
        merge_threshold=args.merge_threshold,
        freeze_threshold=args.freeze_threshold,
    )
    write_outputs(payload, update_cluster_state=not args.no_cluster_update)
    print(f"Families: {payload['family_count']}")
    print(f"Status counts: {payload['status_counts']}")
    print(f"Similar pairs: {payload['similar_pair_count']}")
    audit_path = Path(str(payload.get("_written_audit_path") or OUTPUT_PATH))
    try:
        print(f"Audit: {audit_path.relative_to(ROOT_DIR)}")
    except ValueError:
        print(f"Audit: {audit_path}")
    if not args.no_cluster_update:
        print(f"Cluster state: {CLUSTER_STATE_PATH.relative_to(ROOT_DIR)}")


if __name__ == "__main__":
    main()
