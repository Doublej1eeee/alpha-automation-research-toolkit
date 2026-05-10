#!/usr/bin/env python
"""Lightweight local similarity pre-screen for raw-family candidates.

This is not a replacement for platform self-correlation. It is a cheap local
warning layer based on expression operators, field tokens, mechanism, and
settings. It should be used to prioritize or diagnose candidates, not to mark
platform status.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import re
import sys
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from script.raw_alpha_rotation import load_raw_alpha_pool  # noqa: E402


OUTPUT_PATH = ROOT_DIR / "result_store" / "analysis" / "local_similarity_prescreen.json"
FALLBACK_PATH = Path("C:/tmp/learning_local_similarity_prescreen/local_similarity_prescreen.json")
TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
OPERATORS = {
    "rank", "group_rank", "group_neutralize", "group_zscore", "ts_rank",
    "ts_mean", "ts_backfill", "ts_delta", "ts_std_dev", "ts_corr",
    "ts_zscore", "trade_when", "vector_neut", "regression_neut",
    "winsorize", "normalize", "scale", "zscore", "abs", "log",
}


def tokens(text: str) -> set[str]:
    return {token.lower() for token in TOKEN_RE.findall(text or "")}


def jaccard(a: set[str], b: set[str]) -> float:
    if not a and not b:
        return 0.0
    return len(a & b) / max(len(a | b), 1)


def expression_profile(seed: Any) -> dict[str, Any]:
    expr_tokens = tokens(seed.expression)
    operators = expr_tokens & OPERATORS
    fields = {field.lower() for field in seed.fields}
    non_operator_tokens = expr_tokens - operators
    return {
        "family": seed.family,
        "mechanism_id": seed.mechanism_id,
        "expression_family": seed.expression_family,
        "data_family": seed.data_family,
        "domain_tokens": tokens(seed.domain),
        "field_tokens": set().union(*(tokens(field) for field in fields)) if fields else set(),
        "operators": operators,
        "expression_tokens": non_operator_tokens,
    }


def similarity(a: dict[str, Any], b: dict[str, Any]) -> float:
    score = 0.0
    score += 0.28 * jaccard(a["field_tokens"], b["field_tokens"])
    score += 0.22 * jaccard(a["operators"], b["operators"])
    score += 0.18 * jaccard(a["domain_tokens"], b["domain_tokens"])
    score += 0.12 * jaccard(a["expression_tokens"], b["expression_tokens"])
    if a["mechanism_id"] and a["mechanism_id"] == b["mechanism_id"]:
        score += 0.12
    if a["expression_family"] and a["expression_family"] == b["expression_family"]:
        score += 0.05
    if a["data_family"] and a["data_family"] == b["data_family"]:
        score += 0.03
    return round(min(score, 1.0), 6)


def build(threshold: float = 0.62) -> dict[str, Any]:
    profiles = [expression_profile(seed) for seed in load_raw_alpha_pool()]
    pairs: list[dict[str, Any]] = []
    for i, left in enumerate(profiles):
        for right in profiles[i + 1 :]:
            score = similarity(left, right)
            if score >= threshold:
                pairs.append(
                    {
                        "left": left["family"],
                        "right": right["family"],
                        "similarity": score,
                        "same_mechanism": left["mechanism_id"] == right["mechanism_id"],
                        "same_expression_family": left["expression_family"] == right["expression_family"],
                    }
                )
    pairs.sort(key=lambda row: (-float(row["similarity"]), row["left"], row["right"]))
    family_risk = Counter()
    for pair in pairs:
        family_risk[pair["left"]] += 1
        family_risk[pair["right"]] += 1
    return {
        "schema_version": 1,
        "threshold": threshold,
        "family_count": len(profiles),
        "similar_pair_count": len(pairs),
        "similar_pairs": pairs[:200],
        "family_similarity_risk": dict(family_risk.most_common()),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Local expression/field similarity pre-screen.")
    parser.add_argument("--threshold", type=float, default=0.62)
    args = parser.parse_args()
    payload = build(threshold=args.threshold)
    output = None
    for path in [OUTPUT_PATH, FALLBACK_PATH]:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            output = path
            break
        except PermissionError:
            continue
    print(f"Families: {payload['family_count']}")
    print(f"Similar pairs: {payload['similar_pair_count']}")
    if output:
        try:
            print(f"Output: {output.relative_to(ROOT_DIR)}")
        except ValueError:
            print(f"Output: {output}")
    else:
        print("Output: <write skipped: permission denied>")


if __name__ == "__main__":
    main()
