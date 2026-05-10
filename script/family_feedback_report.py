#!/usr/bin/env python
"""Summarize raw-family outcomes and suggest promote/refine/retire decisions.

This is an offline decision layer. It reads result indexes and slot-miner state,
then produces a family-level report without changing backtest runtime.
"""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import sys
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from script.raw_alpha_rotation import load_raw_alpha_pool  # noqa: E402

RESULTS_INDEX = ROOT_DIR / "result_store" / "index" / "alpha_catalog.jsonl"
SUPPLY_JOBS = ROOT_DIR / "result_store" / "supply" / "supply_jobs.json"
SLOT_MINER_DIR = ROOT_DIR / "result_store" / "slot_miner"
OUTPUT_JSON = ROOT_DIR / "result_store" / "analysis" / "family_feedback_report.json"
OUTPUT_MD = ROOT_DIR / "result_store" / "analysis" / "family_feedback_report.md"
ACTION_JSON = ROOT_DIR / "result_store" / "analysis" / "raw_family_actions.json"
DATASET_FEEDBACK_JSON = ROOT_DIR / "result_store" / "analysis" / "dataset_feedback_report.json"
FALLBACK_DIR = Path("C:/tmp/learning_family_feedback_report")

PASS_GATE = 7
FINAL_GATE = 8


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


def load_jsonl(path: Path) -> list[dict[str, Any]]:
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
    return rows


def extract_checks(alpha_details: dict[str, Any]) -> list[dict[str, Any]]:
    submit_preview = ((alpha_details.get("submitPreview") or {}).get("is") or {}).get("checks") or []
    if submit_preview:
        return [check for check in submit_preview if isinstance(check, dict)]
    submit_checks = ((alpha_details.get("is") or {}).get("submitChecks")) or []
    if submit_checks:
        return [check for check in submit_checks if isinstance(check, dict)]
    checks = ((alpha_details.get("is") or {}).get("checks")) or []
    return [check for check in checks if isinstance(check, dict)]


def pass_count(alpha_details: dict[str, Any]) -> int:
    return sum(1 for check in extract_checks(alpha_details) if str(check.get("result") or "").upper() == "PASS")


def failed_checks(alpha_details: dict[str, Any]) -> list[str]:
    return [
        str(check.get("name") or "")
        for check in extract_checks(alpha_details)
        if str(check.get("result") or "").upper() == "FAIL"
    ]


def self_corr_status(alpha_details: dict[str, Any]) -> str:
    for check in extract_checks(alpha_details):
        if str(check.get("name") or "").upper() == "SELF_CORRELATION":
            return str(check.get("result") or "UNKNOWN").upper()
    return "MISSING"


def load_results() -> list[dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for row in load_jsonl(RESULTS_INDEX):
        alpha_id = str(row.get("alpha_id") or "")
        if alpha_id:
            latest[alpha_id] = row
    return list(latest.values())


def load_supply_jobs() -> list[dict[str, Any]]:
    payload = load_json(SUPPLY_JOBS)
    jobs = payload.get("jobs") or []
    return [job for job in jobs if isinstance(job, dict)]


def parse_slot_miner_source(source_file: str) -> dict[str, Any]:
    text = str(source_file or "")
    if not (text.startswith("<slot_miner:") and text.endswith(">")):
        return {}
    body = text[len("<slot_miner:") : -1]
    parts = body.split(":", 3)
    if len(parts) < 3:
        return {}
    payload = {
        "job_name": parts[0],
        "template_file": parts[1],
        "fields": parts[2] if len(parts) >= 3 else "",
    }
    return payload


def family_job_map(jobs: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    mapping: dict[str, dict[str, Any]] = {}
    for job in jobs:
        name = str(job.get("name") or "")
        family = str(job.get("raw_alpha_family") or "")
        if name and family:
            mapping[name] = job
    return mapping


def load_dataset_feedback() -> dict[str, Any]:
    return load_json(DATASET_FEEDBACK_JSON)


def _row_names(rows: list[dict[str, Any]], key: str = "job_count", limit: int = 6) -> list[str]:
    clean = [row for row in rows if isinstance(row, dict) and row.get("name")]
    clean.sort(key=lambda row: (-safe_float(row.get(key)), str(row.get("name") or "")))
    return [str(row.get("name")) for row in clean[:limit]]


def dataset_feedback_hints_for_family(family: str, dataset_feedback: dict[str, Any]) -> dict[str, list[str]]:
    planned = dataset_feedback.get("planned_exposure") if isinstance(dataset_feedback, dict) else {}
    planned = planned if isinstance(planned, dict) else {}
    family_rows = planned.get("raw_families") or []
    family_active = any(str(row.get("name") or "") == family for row in family_rows if isinstance(row, dict))

    hints = {
        "preferred_datasets": [],
        "preferred_semantic_tags": [],
        "preferred_settings_clusters": [],
        "avoid_datasets": [],
        "avoid_semantic_tags": [],
        "avoid_settings_clusters": [],
    }
    if not family_active:
        return hints

    outcome_rows = {
        "datasets": dataset_feedback.get("datasets") or [],
        "semantic_tags": dataset_feedback.get("semantic_tags") or [],
        "settings_clusters": dataset_feedback.get("settings_clusters") or [],
    }
    for key, target in [
        ("datasets", "preferred_datasets"),
        ("semantic_tags", "preferred_semantic_tags"),
        ("settings_clusters", "preferred_settings_clusters"),
    ]:
        winners = [
            row
            for row in outcome_rows[key]
            if isinstance(row, dict)
            and (
                int(row.get("submit_ready_count") or 0) > 0
                or int(row.get("pass8_count") or 0) > 0
                or int(row.get("pass7_count") or 0) > 0
            )
        ]
        if winners:
            hints[target] = _row_names(winners, key="pass7_count", limit=5)

    # Before matched outcomes exist, use planned exposure as a gentle grounding hint.
    if not hints["preferred_datasets"]:
        hints["preferred_datasets"] = _row_names(planned.get("datasets") or [], limit=6)
    if not hints["preferred_semantic_tags"]:
        hints["preferred_semantic_tags"] = _row_names(planned.get("semantic_tags") or [], limit=8)
    if not hints["preferred_settings_clusters"]:
        hints["preferred_settings_clusters"] = _row_names(planned.get("settings_clusters") or [], limit=4)
    return hints


def load_slot_histories() -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    if not SLOT_MINER_DIR.exists():
        return rows
    for path in sorted(SLOT_MINER_DIR.glob("*.json")):
        payload = load_json(path)
        history = payload.get("history") or []
        for event in history:
            if isinstance(event, dict):
                rows.append(event)
    return rows


def raw_family_names() -> set[str]:
    return {seed.family for seed in load_raw_alpha_pool()}


def infer_family_from_result(row: dict[str, Any], known_families: set[str]) -> str:
    tags = [str(tag) for tag in (row.get("tags") or [])]
    for tag in tags:
        if tag in known_families:
            return tag
    text = " ".join(
        [
            str(row.get("name") or ""),
            str(row.get("description") or ""),
            str(row.get("batch_name") or ""),
            str(row.get("source_file") or ""),
        ]
    )
    for family in sorted(known_families, key=len, reverse=True):
        if family in text:
            return family
    return ""


def infer_mechanism_from_result(row: dict[str, Any]) -> str:
    mechanism_ids = {
        "attention_revision_congestion",
        "promotion_dispersion_mismatch",
        "instability_fragility_leadlag",
        "balance_sheet_pressure",
        "liquidity_microstructure_shock",
        "narrative_valuation_gap",
        "event_novelty_underreaction",
        "systematic_risk_regime_shift",
        "footnote_accounting_complexity",
        "credit_recovery_pressure",
        "mna_price_impact_absorption",
    }
    for tag in [str(tag) for tag in (row.get("tags") or [])]:
        if tag in mechanism_ids:
            return tag
    return ""


def decision_for(stats: dict[str, Any]) -> str:
    tested = stats["tested"]
    pass7 = stats["pass7_count"]
    pass8 = stats["pass8_count"]
    submit_ready = stats["submit_ready_count"]
    avg_sharpe = stats["avg_sharpe"]
    corr_fail = stats["self_corr_fail_count"]
    if submit_ready >= 2 or (pass8 >= 2 and avg_sharpe >= 1.2):
        return "promote"
    if tested >= 25 and pass7 == 0:
        return "retire"
    if tested >= 18 and pass7 > 0 and corr_fail >= pass7:
        return "refine"
    if tested >= 18 and avg_sharpe < 0.2 and pass8 == 0:
        return "retire"
    return "refine" if tested >= 8 else "observe"


def action_for(stats: dict[str, Any], dataset_feedback: dict[str, Any] | None = None) -> dict[str, Any]:
    decision = str(stats.get("decision") or decision_for(stats)).lower()
    tested = int(stats.get("tested") or 0)
    pass7 = int(stats.get("pass7_count") or 0)
    pass8 = int(stats.get("pass8_count") or 0)
    submit_ready = int(stats.get("submit_ready_count") or 0)
    corr_fail = int(stats.get("self_corr_fail_count") or 0)
    avg_sharpe = safe_float(stats.get("avg_sharpe"))
    failed_checks = stats.get("failed_checks") or {}
    top_failed_checks = list(failed_checks.keys())[:5] if isinstance(failed_checks, dict) else []

    if decision == "promote":
        resource_action = "expand_lanes_and_keep_settings"
        priority_delta = 1.05
        lane_target = 4
        max_jobs_multiplier = 1.25
    elif decision == "refine":
        resource_action = "refresh_fields_and_diversify_settings"
        priority_delta = 0.22 if pass7 else 0.08
        lane_target = 3
        max_jobs_multiplier = 1.0
    elif decision == "retire":
        resource_action = "pause_rotation_until_new_evidence"
        priority_delta = -2.4
        lane_target = 0
        max_jobs_multiplier = 0.0
    else:
        resource_action = "observe_minimal_rotation"
        priority_delta = 0.05
        lane_target = 2
        max_jobs_multiplier = 0.8

    refine_focus: list[str] = []
    if pass7 and corr_fail >= pass7:
        refine_focus.append("lower_self_correlation")
    if "LOW_SHARPE" in top_failed_checks or avg_sharpe < 0.3:
        refine_focus.append("improve_signal_strength")
    if any("SUB_UNIVERSE" in item for item in top_failed_checks):
        refine_focus.append("adjust_universe_or_neutralization")
    if any("WEIGHT" in item for item in top_failed_checks):
        refine_focus.append("reduce_concentration")
    if not refine_focus and decision == "refine":
        refine_focus.append("try_new_dataset_lane")

    tuning_actions: dict[str, Any] = {
        "prefer_new_dataset_lane": "try_new_dataset_lane" in refine_focus or "lower_self_correlation" in refine_focus,
        "neutralization_bias": "default",
        "truncation_bias": "default",
        "decay_bias": "default",
        "selection_query_bias_terms": [],
        "selection_query_avoid_terms": [],
    }
    if "adjust_universe_or_neutralization" in refine_focus:
        tuning_actions["neutralization_bias"] = "broader"
    if "reduce_concentration" in refine_focus:
        tuning_actions["truncation_bias"] = "tighter"
    if "improve_signal_strength" in refine_focus:
        tuning_actions["selection_query_bias_terms"].extend(["high_signal", "strong_relation"])
    if any("TURNOVER" in item for item in top_failed_checks):
        if "HIGH_TURNOVER" in top_failed_checks:
            tuning_actions["decay_bias"] = "longer"
            tuning_actions["selection_query_avoid_terms"].append("hyper_reactive")
        else:
            tuning_actions["decay_bias"] = "shorter"
            tuning_actions["selection_query_bias_terms"].append("faster_reaction")
    if "lower_self_correlation" in refine_focus:
        tuning_actions["selection_query_bias_terms"].extend(["orthogonal", "alternative_dataset"])
    if "LOW_FITNESS" in top_failed_checks:
        tuning_actions["selection_query_bias_terms"].append("cleaner_signal")
    feedback_hints = dataset_feedback_hints_for_family(str(stats.get("family") or ""), dataset_feedback or {})
    if feedback_hints.get("preferred_semantic_tags"):
        tuning_actions["selection_query_bias_terms"].extend(feedback_hints["preferred_semantic_tags"][:6])
    if feedback_hints.get("preferred_datasets"):
        tuning_actions["preferred_datasets"] = feedback_hints["preferred_datasets"]
    if feedback_hints.get("preferred_settings_clusters"):
        tuning_actions["preferred_settings_clusters"] = feedback_hints["preferred_settings_clusters"]
    tuning_actions["selection_query_bias_terms"] = list(dict.fromkeys(tuning_actions["selection_query_bias_terms"]))
    tuning_actions["selection_query_avoid_terms"] = list(dict.fromkeys(tuning_actions["selection_query_avoid_terms"]))

    return {
        "family": stats.get("family"),
        "decision": decision,
        "resource_action": resource_action,
        "priority_delta": round(priority_delta, 6),
        "dataset_lanes_target": lane_target,
        "max_jobs_multiplier": max_jobs_multiplier,
        "rotation_paused": decision == "retire",
        "refine_focus": refine_focus,
        "tuning_actions": tuning_actions,
        "dataset_feedback_hints": feedback_hints,
        "evidence": {
            "tested": tested,
            "pass7_count": pass7,
            "pass8_count": pass8,
            "submit_ready_count": submit_ready,
            "self_corr_fail_count": corr_fail,
            "avg_sharpe": avg_sharpe,
            "top_failed_checks": top_failed_checks,
        },
    }


def summarize() -> dict[str, Any]:
    jobs = load_supply_jobs()
    name_to_job = family_job_map(jobs)
    known_families = raw_family_names()
    miner_history = load_slot_histories()
    dataset_feedback = load_dataset_feedback()
    attempts_by_family = Counter()
    for event in miner_history:
        family = str(event.get("raw_alpha_family") or "")
        if family:
            attempts_by_family[family] += 1

    families: dict[str, dict[str, Any]] = {}
    for row in load_results():
        source_file = str(row.get("source_file") or "")
        if not source_file:
            continue
        parsed = parse_slot_miner_source(source_file)
        job_name = str(parsed.get("job_name") or "")
        if not job_name:
            job_name = Path(source_file).stem
        job = name_to_job.get(job_name)
        family = str((job or {}).get("raw_alpha_family") or "")
        if not family:
            family = infer_family_from_result(row, known_families)
        if not family:
            continue
        alpha_details = row.get("alpha_details") or {}
        is_block = alpha_details.get("is") or {}
        entry = families.setdefault(
            family,
            {
                "family": family,
                "mechanism_id": str((job or {}).get("mechanism_id") or infer_mechanism_from_result(row)),
                "expression_family": str((job or {}).get("expression_family") or ""),
                "data_family": str((job or {}).get("data_family") or ""),
                "tested": 0,
                "attempted": attempts_by_family.get(family, 0),
                "pass7_count": 0,
                "pass8_count": 0,
                "submit_ready_count": 0,
                "self_corr_fail_count": 0,
                "self_corr_pending_count": 0,
                "rule_colors": Counter(),
                "failed_checks": Counter(),
                "field_clusters": Counter(),
                "settings_clusters": Counter(),
                "sharpe_values": [],
                "fitness_values": [],
                "alpha_ids": [],
            },
        )
        entry["tested"] += 1
        entry["alpha_ids"].append(str(row.get("alpha_id") or ""))
        pc = pass_count(alpha_details)
        if pc >= PASS_GATE:
            entry["pass7_count"] += 1
        if pc >= FINAL_GATE:
            entry["pass8_count"] += 1
        corr = self_corr_status(alpha_details)
        if corr == "FAIL":
            entry["self_corr_fail_count"] += 1
        elif corr == "PENDING":
            entry["self_corr_pending_count"] += 1
        if pc >= FINAL_GATE and corr == "PASS":
            entry["submit_ready_count"] += 1
        entry["rule_colors"][str(row.get("rule_color") or "WHITE").upper()] += 1
        for name in failed_checks(alpha_details):
            entry["failed_checks"][name] += 1
        entry["sharpe_values"].append(safe_float(is_block.get("sharpe")))
        entry["fitness_values"].append(safe_float(is_block.get("fitness")))
        if job:
            entry["field_clusters"][str(job.get("primary_field_cluster") or "unknown")] += 1
            entry["settings_clusters"][str(job.get("settings_cluster") or "unknown")] += 1

    rows: list[dict[str, Any]] = []
    for family, stats in families.items():
        sharpe_values = stats.pop("sharpe_values")
        fitness_values = stats.pop("fitness_values")
        stats["avg_sharpe"] = round(sum(sharpe_values) / len(sharpe_values), 4) if sharpe_values else 0.0
        stats["avg_fitness"] = round(sum(fitness_values) / len(fitness_values), 4) if fitness_values else 0.0
        stats["best_sharpe"] = max(sharpe_values) if sharpe_values else 0.0
        stats["best_fitness"] = max(fitness_values) if fitness_values else 0.0
        stats["rule_colors"] = dict(stats["rule_colors"].most_common())
        stats["failed_checks"] = dict(stats["failed_checks"].most_common())
        stats["field_clusters"] = dict(stats["field_clusters"].most_common())
        stats["settings_clusters"] = dict(stats["settings_clusters"].most_common())
        stats["decision"] = decision_for(stats)
        rows.append(stats)

    rows.sort(
        key=lambda item: (
            {"promote": 0, "refine": 1, "observe": 2, "retire": 3}.get(str(item.get("decision")), 9),
            -int(item.get("submit_ready_count") or 0),
            -int(item.get("pass8_count") or 0),
            -safe_float(item.get("avg_sharpe")),
            str(item.get("family") or ""),
        )
    )
    return {
        "schema_version": 1,
        "family_count": len(rows),
        "families": rows,
        "dataset_feedback": {
            "result_count": dataset_feedback.get("result_count") if isinstance(dataset_feedback, dict) else None,
            "matched_result_count": dataset_feedback.get("matched_result_count") if isinstance(dataset_feedback, dict) else None,
            "job_metadata_archive_count": dataset_feedback.get("job_metadata_archive_count") if isinstance(dataset_feedback, dict) else None,
        },
        "actions": [action_for(row, dataset_feedback) for row in rows],
    }


def render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Family Feedback Report",
        "",
        f"- Family count: {payload.get('family_count')}",
        "",
    ]
    for row in payload.get("families") or []:
        lines.extend(
            [
                f"## {row.get('family')}",
                "",
                f"- Decision: {row.get('decision')}",
                f"- Mechanism: {row.get('mechanism_id')}",
                f"- Expression family: {row.get('expression_family')}",
                f"- Data family: {row.get('data_family')}",
                f"- Attempted/Tested: {row.get('attempted')}/{row.get('tested')}",
                f"- 7pass: {row.get('pass7_count')} | 8pass: {row.get('pass8_count')} | submit_ready: {row.get('submit_ready_count')}",
                f"- Self-corr fail/pending: {row.get('self_corr_fail_count')}/{row.get('self_corr_pending_count')}",
                f"- Avg Sharpe/Fitness: {row.get('avg_sharpe')}/{row.get('avg_fitness')}",
                f"- Best Sharpe/Fitness: {row.get('best_sharpe')}/{row.get('best_fitness')}",
                f"- Rule colors: {row.get('rule_colors')}",
                f"- Failed checks: {row.get('failed_checks')}",
                f"- Field clusters: {row.get('field_clusters')}",
                f"- Settings clusters: {row.get('settings_clusters')}",
                "",
            ]
        )
    return "\n".join(lines).strip() + "\n"


def write(payload: dict[str, Any]) -> Path | None:
    for directory, json_path, md_path in [
        (OUTPUT_JSON.parent, OUTPUT_JSON, OUTPUT_MD),
        (FALLBACK_DIR, FALLBACK_DIR / OUTPUT_JSON.name, FALLBACK_DIR / OUTPUT_MD.name),
    ]:
        try:
            directory.mkdir(parents=True, exist_ok=True)
            json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            md_path.write_text(render_markdown(payload), encoding="utf-8")
            action_path = directory / ACTION_JSON.name
            action_payload = {
                "schema_version": 1,
                "family_count": payload.get("family_count"),
                "actions": payload.get("actions") or [],
            }
            action_path.write_text(json.dumps(action_payload, ensure_ascii=False, indent=2), encoding="utf-8")
            return json_path
        except PermissionError:
            continue
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Build raw-family feedback and decision report.")
    parser.parse_args()
    payload = summarize()
    output = write(payload)
    print(f"Families: {payload['family_count']}")
    if output:
        try:
            print(f"JSON: {output.relative_to(ROOT_DIR)}")
        except ValueError:
            print(f"JSON: {output}")
    else:
        print("JSON: <write skipped: permission denied>")


if __name__ == "__main__":
    main()
