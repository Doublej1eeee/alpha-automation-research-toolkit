#!/usr/bin/env python
"""Template builder and strategy-based variant generator."""

from __future__ import annotations

import argparse
import copy
import json
from pathlib import Path
import re
import sys

import yaml

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from script.template_similarity import template_hash, template_similarity
from script.template_validator import validate_template_payload


TEMP_OUTPUT_DIR = ROOT_DIR / "temp" / "template_builder"


def load_yaml(path: Path) -> dict:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Invalid yaml mapping: {path}")
    return payload


def slugify(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]+", "_", value).strip("_").lower() or "template"


def strategy_wrap_rank(template: dict) -> dict:
    variant = copy.deepcopy(template)
    expr = str(variant["expression"]).strip()
    if expr.startswith("rank(") or expr.startswith("group_rank("):
        return variant
    variant["expression"] = f"rank({expr})"
    variant["name"] = f"{variant.get('name', 'template')}_rank"
    tags = list(variant.get("tags") or [])
    if "strategy_rank" not in tags:
        tags.append("strategy_rank")
    variant["tags"] = tags
    return variant


def strategy_negate(template: dict) -> dict:
    variant = copy.deepcopy(template)
    expr = str(variant["expression"]).strip()
    if expr.startswith("-"):
        return variant
    variant["expression"] = f"-({expr})"
    variant["name"] = f"{variant.get('name', 'template')}_neg"
    tags = list(variant.get("tags") or [])
    if "strategy_negate" not in tags:
        tags.append("strategy_negate")
    variant["tags"] = tags
    return variant


def strategy_group_rank_industry(template: dict) -> dict:
    variant = copy.deepcopy(template)
    expr = str(variant["expression"]).strip()
    if expr.startswith("group_rank("):
        return variant
    variant["expression"] = f"group_rank({expr}, industry)"
    variant["name"] = f"{variant.get('name', 'template')}_grp_ind"
    tags = list(variant.get("tags") or [])
    if "strategy_group_rank_industry" not in tags:
        tags.append("strategy_group_rank_industry")
    variant["tags"] = tags
    return variant


def strategy_ts_mean_5(template: dict) -> dict:
    variant = copy.deepcopy(template)
    expr = str(variant["expression"]).strip()
    if expr.startswith("ts_mean("):
        return variant
    variant["expression"] = f"ts_mean({expr}, 5)"
    variant["name"] = f"{variant.get('name', 'template')}_tsmean5"
    tags = list(variant.get("tags") or [])
    if "strategy_ts_mean_5" not in tags:
        tags.append("strategy_ts_mean_5")
    variant["tags"] = tags
    return variant


def strategy_ts_rank_60(template: dict) -> dict:
    variant = copy.deepcopy(template)
    expr = str(variant["expression"]).strip()
    if expr.startswith("ts_rank("):
        return variant
    variant["expression"] = f"ts_rank({expr}, 60)"
    variant["name"] = f"{variant.get('name', 'template')}_tsrank60"
    tags = list(variant.get("tags") or [])
    if "strategy_ts_rank_60" not in tags:
        tags.append("strategy_ts_rank_60")
    variant["tags"] = tags
    return variant


STRATEGIES = {
    "rank": strategy_wrap_rank,
    "negate": strategy_negate,
    "group_rank_industry": strategy_group_rank_industry,
    "ts_mean_5": strategy_ts_mean_5,
    "ts_rank_60": strategy_ts_rank_60,
}


def dedupe_variants(templates: list[dict], similarity_threshold: float = 0.96) -> list[dict]:
    kept: list[dict] = []
    seen_hashes: set[str] = set()
    for template in templates:
        expr = str(template.get("expression") or "")
        expr_hash = template_hash(expr)
        if expr_hash in seen_hashes:
            continue
        if any(template_similarity(expr, str(existing.get("expression") or "")) >= similarity_threshold for existing in kept):
            continue
        seen_hashes.add(expr_hash)
        kept.append(template)
    return kept


def build_variants(base_template: dict, strategies: list[str]) -> list[dict]:
    variants = [copy.deepcopy(base_template)]
    for strategy_name in strategies:
        builder = STRATEGIES[strategy_name]
        variant = builder(base_template)
        variants.append(variant)
    return dedupe_variants(variants)


def build_from_raw_alpha_builder(path: Path) -> dict:
    payload = json.loads(path.read_text(encoding="utf-8"))
    template = (((payload.get("expression_drafting") or {}).get("template")) or {})
    if not isinstance(template, dict) or not template:
        raise ValueError(f"No draft template found in {path}")
    return template


def write_variant_files(variants: list[dict], output_dir: Path) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for template in variants:
        name = slugify(str(template.get("name") or "template"))
        path = output_dir / f"{name}.yaml"
        path.write_text(yaml.safe_dump(template, sort_keys=False, allow_unicode=True), encoding="utf-8")
        written.append(path)
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description="Generate strategy-based template variants.")
    parser.add_argument("input", help="Base template yaml or raw_alpha_builder json output")
    parser.add_argument(
        "--strategy",
        action="append",
        choices=sorted(STRATEGIES.keys()),
        help="Strategy to apply. Can be repeated. Default: all",
    )
    parser.add_argument("--output-dir", help="Output directory. Default: temp/template_builder/latest")
    args = parser.parse_args()

    input_path = Path(args.input).resolve()
    if input_path.suffix.lower() == ".json":
        base_template = build_from_raw_alpha_builder(input_path)
    else:
        base_template = load_yaml(input_path)

    strategies = args.strategy or sorted(STRATEGIES.keys())
    variants = build_variants(base_template, strategies)

    valid_variants = []
    for variant in variants:
        result = validate_template_payload(Path("<generated_variant>"), variant)
        if result.valid:
            valid_variants.append(variant)

    output_dir = Path(args.output_dir).resolve() if args.output_dir else (TEMP_OUTPUT_DIR / "latest")
    written = write_variant_files(valid_variants, output_dir)
    print(f"Base template: {input_path}")
    print(f"Strategies: {', '.join(strategies)}")
    print(f"Generated variants: {len(valid_variants)}")
    print(f"Output dir: {output_dir}")
    for path in written:
        print(path)


if __name__ == "__main__":
    main()
