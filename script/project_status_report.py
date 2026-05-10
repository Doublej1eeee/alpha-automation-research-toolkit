#!/usr/bin/env python
"""Build a compact project status report for the alpha automation system."""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[1]

SUPPLY_JOBS = ROOT_DIR / "result_store" / "supply" / "supply_jobs.json"
DATASET_FEEDBACK = ROOT_DIR / "result_store" / "analysis" / "dataset_feedback_report.json"
FAMILY_FEEDBACK = ROOT_DIR / "result_store" / "analysis" / "family_feedback_report.json"
RAW_FAMILY_ACTIONS = ROOT_DIR / "result_store" / "analysis" / "raw_family_actions.json"
JOB_METADATA_ARCHIVE = ROOT_DIR / "result_store" / "analysis" / "job_metadata_archive.json"
DATASET_KB = ROOT_DIR / "result_store" / "data_catalog" / "dataset_knowledge_base.json"
RESULT_INDEX = ROOT_DIR / "result_store" / "index" / "alpha_catalog.jsonl"
OUTPUT_JSON = ROOT_DIR / "result_store" / "analysis" / "project_status_report.json"
OUTPUT_MD = ROOT_DIR / "result_store" / "analysis" / "project_status_report.md"
FALLBACK_DIR = ROOT_DIR / "temp" / "project_status_report"


def load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def load_jsonl(path: Path, limit: int = 0) -> list[dict[str, Any]]:
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
            if isinstance(row, dict):
                rows.append(row)
                if limit and len(rows) >= limit:
                    break
    return rows


def _safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def counter_rows(counter: Counter[str], limit: int = 12) -> list[dict[str, Any]]:
    return [{"name": name, "count": count} for name, count in counter.most_common(limit)]


def result_counts() -> dict[str, Any]:
    latest: dict[str, dict[str, Any]] = {}
    for row in load_jsonl(RESULT_INDEX):
        alpha_id = str(row.get("alpha_id") or "")
        if alpha_id:
            latest[alpha_id] = row

    grade_counts: Counter[str] = Counter()
    color_counts: Counter[str] = Counter()
    pass7 = 0
    pass8 = 0
    self_corr_pending = 0
    self_corr_fail = 0
    self_corr_pass = 0
    submit_ready = 0
    best_rows: list[dict[str, Any]] = []

    for row in latest.values():
        details = row.get("alpha_details") or {}
        is_block = details.get("is") or {}
        checks = is_block.get("checks") or []
        passed = sum(1 for check in checks if str((check or {}).get("result") or "").upper() == "PASS")
        corr = "MISSING"
        for check in checks:
            if str((check or {}).get("name") or "").upper() == "SELF_CORRELATION":
                corr = str((check or {}).get("result") or "UNKNOWN").upper()
                break
        if passed >= 7:
            pass7 += 1
        if passed >= 8:
            pass8 += 1
        if corr == "PENDING":
            self_corr_pending += 1
        elif corr == "FAIL":
            self_corr_fail += 1
        elif corr == "PASS":
            self_corr_pass += 1
        if passed >= 8 and corr == "PASS":
            submit_ready += 1
        grade_counts[str(details.get("grade") or row.get("grade") or "UNKNOWN").upper()] += 1
        color_counts[str(row.get("rule_color") or details.get("color") or "UNKNOWN").upper()] += 1
        best_rows.append(
            {
                "alpha_id": row.get("alpha_id"),
                "name": row.get("name"),
                "grade": details.get("grade"),
                "rule_color": row.get("rule_color"),
                "pass_count": passed,
                "self_corr": corr,
                "sharpe": _safe_float(is_block.get("sharpe")),
                "fitness": _safe_float(is_block.get("fitness")),
            }
        )

    best_rows.sort(key=lambda row: (-int(row.get("pass_count") or 0), -_safe_float(row.get("sharpe")), -_safe_float(row.get("fitness"))))
    return {
        "total_unique_alphas": len(latest),
        "pass7_count": pass7,
        "pass8_count": pass8,
        "submit_ready_count": submit_ready,
        "self_corr": {
            "pass": self_corr_pass,
            "fail": self_corr_fail,
            "pending": self_corr_pending,
        },
        "grade_counts": dict(grade_counts.most_common()),
        "color_counts": dict(color_counts.most_common()),
        "top_candidates": best_rows[:12],
    }


def summarize_supply(supply: dict[str, Any]) -> dict[str, Any]:
    jobs = [job for job in (supply.get("jobs") or []) if isinstance(job, dict)]
    datasets: Counter[str] = Counter()
    semantics: Counter[str] = Counter()
    settings: Counter[str] = Counter()
    families: Counter[str] = Counter()
    modes: Counter[str] = Counter()
    for job in jobs:
        modes[str(job.get("supply_mode") or "unknown")] += 1
        family = str(job.get("raw_alpha_family") or job.get("source_family_key") or "")
        if family:
            families[family] += 1
        settings[str(job.get("settings_cluster") or "unknown")] += 1
        for tag in job.get("field_semantic_tags") or []:
            if str(tag):
                semantics[str(tag)] += 1
        for spec in job.get("slot_dataset_plan") or []:
            if isinstance(spec, dict) and spec.get("dataset_id"):
                datasets[str(spec.get("dataset_id"))] += 1
    return {
        "generation_id": supply.get("generation_id"),
        "job_count": len(jobs),
        "template_count": supply.get("template_count"),
        "raw_rotation_active": supply.get("raw_rotation_active"),
        "modes": dict(modes.most_common()),
        "families": counter_rows(families),
        "datasets": counter_rows(datasets),
        "semantic_tags": counter_rows(semantics),
        "settings_clusters": counter_rows(settings, limit=8),
    }


def summarize_actions(actions_payload: dict[str, Any]) -> dict[str, Any]:
    actions = [row for row in (actions_payload.get("actions") or []) if isinstance(row, dict)]
    decisions = Counter(str(row.get("decision") or "unknown") for row in actions)
    rows = []
    for row in actions:
        tuning = row.get("tuning_actions") if isinstance(row.get("tuning_actions"), dict) else {}
        rows.append(
            {
                "family": row.get("family"),
                "decision": row.get("decision"),
                "resource_action": row.get("resource_action"),
                "priority_delta": row.get("priority_delta"),
                "refine_focus": row.get("refine_focus") or [],
                "preferred_datasets": tuning.get("preferred_datasets") or [],
                "preferred_semantic_tags": tuning.get("preferred_semantic_tags") or tuning.get("selection_query_bias_terms") or [],
            }
        )
    return {
        "family_count": len(actions),
        "decisions": dict(decisions.most_common()),
        "actions": rows,
    }


def summarize_dataset_kb(payload: dict[str, Any]) -> dict[str, Any]:
    cards = [row for row in (payload.get("cards") or []) if isinstance(row, dict)]
    categories = Counter(str(row.get("category") or "unknown") for row in cards)
    return {
        "dataset_count": payload.get("source_dataset_count") or len(cards),
        "field_count": payload.get("source_field_count"),
        "categories": dict(categories.most_common()),
        "datasets": [
            {
                "id": row.get("id"),
                "category": row.get("category"),
                "field_count": row.get("field_count"),
                "novelty_tier": row.get("novelty_tier"),
                "exploration_score": row.get("exploration_score"),
            }
            for row in cards[:20]
        ],
    }


def build_report() -> dict[str, Any]:
    supply = load_json(SUPPLY_JOBS)
    dataset_feedback = load_json(DATASET_FEEDBACK)
    family_feedback = load_json(FAMILY_FEEDBACK)
    raw_actions = load_json(RAW_FAMILY_ACTIONS)
    archive = load_json(JOB_METADATA_ARCHIVE)
    dataset_kb = load_json(DATASET_KB)
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z"),
        "supply": summarize_supply(supply),
        "results": result_counts(),
        "dataset_feedback": {
            "result_count": dataset_feedback.get("result_count"),
            "matched_result_count": dataset_feedback.get("matched_result_count"),
            "unmatched_result_count": dataset_feedback.get("unmatched_result_count"),
            "dataset_count": dataset_feedback.get("dataset_count"),
            "semantic_tag_count": dataset_feedback.get("semantic_tag_count"),
            "settings_cluster_count": dataset_feedback.get("settings_cluster_count"),
            "planned_exposure": dataset_feedback.get("planned_exposure") or {},
        },
        "family_feedback": {
            "family_count": family_feedback.get("family_count"),
            "dataset_feedback": family_feedback.get("dataset_feedback") or {},
        },
        "raw_family_actions": summarize_actions(raw_actions),
        "job_metadata_archive": {
            "job_count": archive.get("job_count"),
            "template_count": archive.get("template_count"),
            "latest_generation_id": archive.get("latest_generation_id"),
            "updated_at": archive.get("updated_at"),
        },
        "dataset_knowledge_base": summarize_dataset_kb(dataset_kb),
    }


def section_rows(title: str, rows: list[dict[str, Any]], name_key: str = "name", count_key: str = "count") -> list[str]:
    lines = [f"## {title}", ""]
    if not rows:
        return lines + ["- <none>", ""]
    for row in rows:
        lines.append(f"- {row.get(name_key)}: {row.get(count_key)}")
    lines.append("")
    return lines


def render_markdown(payload: dict[str, Any]) -> str:
    supply = payload.get("supply") or {}
    results = payload.get("results") or {}
    dataset_feedback = payload.get("dataset_feedback") or {}
    archive = payload.get("job_metadata_archive") or {}
    actions = payload.get("raw_family_actions") or {}
    kb = payload.get("dataset_knowledge_base") or {}
    lines = [
        "# Project Status Report",
        "",
        f"- Generated at: {payload.get('generated_at')}",
        f"- Supply generation: {supply.get('generation_id')} | jobs: {supply.get('job_count')} | templates: {supply.get('template_count')}",
        f"- Raw rotation active: {supply.get('raw_rotation_active')}",
        f"- Unique alphas: {results.get('total_unique_alphas')} | 7pass: {results.get('pass7_count')} | 8pass: {results.get('pass8_count')} | submit-ready: {results.get('submit_ready_count')}",
        f"- Self-corr pass/fail/pending: {(results.get('self_corr') or {}).get('pass')}/{(results.get('self_corr') or {}).get('fail')}/{(results.get('self_corr') or {}).get('pending')}",
        f"- Dataset feedback matched: {dataset_feedback.get('matched_result_count')}/{dataset_feedback.get('result_count')}",
        f"- Job metadata archive: {archive.get('job_count')} jobs | generation {archive.get('latest_generation_id')}",
        f"- Dataset KB: {kb.get('dataset_count')} datasets | {kb.get('field_count')} fields",
        f"- Raw family decisions: {actions.get('decisions')}",
        "",
    ]
    lines.extend(section_rows("Current Families", supply.get("families") or []))
    lines.extend(section_rows("Current Datasets", supply.get("datasets") or []))
    lines.extend(section_rows("Current Semantic Tags", supply.get("semantic_tags") or []))
    lines.append("## Raw Family Actions")
    lines.append("")
    for row in (actions.get("actions") or [])[:16]:
        lines.append(f"### {row.get('family')}")
        lines.append(f"- Decision: {row.get('decision')} | resource: {row.get('resource_action')} | delta: {row.get('priority_delta')}")
        lines.append(f"- Refine focus: {row.get('refine_focus')}")
        lines.append(f"- Preferred datasets: {row.get('preferred_datasets')}")
        lines.append(f"- Preferred semantics: {row.get('preferred_semantic_tags')[:10] if isinstance(row.get('preferred_semantic_tags'), list) else row.get('preferred_semantic_tags')}")
        lines.append("")
    lines.append("## Top Candidates")
    lines.append("")
    for row in (results.get("top_candidates") or [])[:12]:
        lines.append(
            f"- {row.get('alpha_id')} | pass={row.get('pass_count')} | corr={row.get('self_corr')} | "
            f"sharpe={row.get('sharpe')} | fitness={row.get('fitness')} | grade={row.get('grade')}"
        )
    lines.append("")
    return "\n".join(lines).strip() + "\n"


def write_report(payload: dict[str, Any]) -> Path:
    errors: list[str] = []
    for directory, json_path, md_path in [
        (OUTPUT_JSON.parent, OUTPUT_JSON, OUTPUT_MD),
        (FALLBACK_DIR, FALLBACK_DIR / OUTPUT_JSON.name, FALLBACK_DIR / OUTPUT_MD.name),
    ]:
        try:
            directory.mkdir(parents=True, exist_ok=True)
            json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            md_path.write_text(render_markdown(payload), encoding="utf-8")
            return json_path
        except Exception as exc:
            errors.append(f"{json_path}: {type(exc).__name__}: {exc}")
            continue
    raise PermissionError("Could not write project status report to primary or fallback path: " + " | ".join(errors))


def main() -> None:
    parser = argparse.ArgumentParser(description="Build project status report.")
    parser.parse_args()
    payload = build_report()
    output = write_report(payload)
    print(f"Supply generation: {(payload.get('supply') or {}).get('generation_id')}")
    print(f"Unique alphas: {(payload.get('results') or {}).get('total_unique_alphas')}")
    print(f"Dataset feedback matched: {(payload.get('dataset_feedback') or {}).get('matched_result_count')}/{(payload.get('dataset_feedback') or {}).get('result_count')}")
    try:
        print(f"JSON: {output.relative_to(ROOT_DIR)}")
    except ValueError:
        print(f"JSON: {output}")


if __name__ == "__main__":
    main()
