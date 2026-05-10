#!/usr/bin/env python
"""End-to-end research pipeline entrypoint."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from script.raw_alpha_builder import draft_output
from script.template_builder import build_variants, write_variant_files
from script.template_validator import validate_template_payload


TEMP_DIR = ROOT_DIR / "temp" / "research_pipeline"


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the local alpha research pipeline from idea to variants.")
    parser.add_argument("--idea", help="Idea text directly")
    parser.add_argument("--idea-file", help="Path to idea text file")
    parser.add_argument("--field-json", help="Optional raw field json for grounding")
    parser.add_argument("--selection-limit", type=int, default=20)
    parser.add_argument("--research-family-key", help="Use a specific extracted research family key")
    parser.add_argument("--strategy", action="append", help="Template builder strategy. Can repeat.")
    parser.add_argument("--output-dir", help="Output directory")
    args = parser.parse_args()

    if not args.idea and not args.idea_file:
        raise SystemExit("Provide --idea or --idea-file")

    idea_text = args.idea or Path(args.idea_file).read_text(encoding="utf-8")
    field_json = Path(args.field_json).resolve() if args.field_json else None
    draft = draft_output(
        idea_text=idea_text,
        field_json=field_json,
        selection_limit=args.selection_limit,
        research_family_key=args.research_family_key,
    )

    template = ((draft.get("expression_drafting") or {}).get("template")) or {}
    validation = validate_template_payload(Path("<pipeline>"), template)
    if not validation.valid:
        print(json.dumps(draft, ensure_ascii=False, indent=2))
        raise SystemExit("Draft template is invalid")

    from script.template_builder import STRATEGIES

    strategy_names = args.strategy or sorted(STRATEGIES.keys())
    variants = build_variants(template, strategy_names)
    valid_variants = [
        variant
        for variant in variants
        if validate_template_payload(Path("<variant>"), variant).valid
    ]

    output_dir = Path(args.output_dir).resolve() if args.output_dir else (TEMP_DIR / "latest")
    output_dir.mkdir(parents=True, exist_ok=True)
    draft_path = output_dir / "draft.json"
    draft_path.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
    variant_paths = write_variant_files(valid_variants, output_dir / "variants")

    print(f"Draft saved: {draft_path}")
    print(f"Variants: {len(valid_variants)}")
    for path in variant_paths:
        print(path)


if __name__ == "__main__":
    main()
