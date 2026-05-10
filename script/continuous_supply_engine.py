#!/usr/bin/env python
"""Continuous supply engine for large-scale alpha backtesting.

This is the project's lightweight version of the worldquant-miner supply idea:
- expand base templates into many expression variants
- keep field inventories large enough for each variant
- emit runnable jobs for the continuous orchestrator

It intentionally does not final-submit alphas. It only creates simulation supply.
"""

from __future__ import annotations

import argparse
import copy
from datetime import datetime, timezone
import json
import random
import re
import sys
from pathlib import Path

import yaml


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from script.template_similarity import template_hash, template_similarity  # noqa: E402
from script.template_validator import analyze_expression_compatibility, validate_template_payload  # noqa: E402
from brain_client import iter_all_result_payloads  # noqa: E402
from script.raw_alpha_rotation import build_raw_alpha_rotation_jobs  # noqa: E402
from script.high_grade_repair_engine import repair_jobs as build_high_grade_repair_jobs  # noqa: E402
from script.submit_template_loop import extract_field_slots  # noqa: E402


SUPPLY_DIR = ROOT_DIR / "result_store" / "supply"
SUPPLY_TEMPLATE_DIR = SUPPLY_DIR / "templates"
SUPPLY_JOBS_FILE = SUPPLY_DIR / "supply_jobs.json"
SUPPLY_GENERATION_STATE_FILE = SUPPLY_DIR / "generation_state.json"
SUPPLY_LINEAGE_WAREHOUSE_FILE = SUPPLY_DIR / "lineage_warehouse.json"
JOB_METADATA_ARCHIVE_FILE = ROOT_DIR / "result_store" / "analysis" / "job_metadata_archive.json"
RUNTIME_GUARDRAILS_FILE = ROOT_DIR / "result_store" / "analysis" / "runtime_guardrails.json"


_RESULT_PAYLOAD_CACHE: list[dict] | None = None
_HISTORICAL_JOB_SCORE_CACHE: dict[str, float] = {}
_HISTORICAL_SETTINGS_SCORE_CACHE: dict[str, float] = {}
_HISTORICAL_RESULT_HASHES_CACHE: dict[int, set[str]] = {}
_RECENT_CATEGORY_SCORES_CACHE: dict[int, dict[str, float]] = {}
_RECENT_PERFORMANCE_SNAPSHOT_CACHE: dict[int, dict[str, float | int | str]] = {}
_EXPLOIT_SOURCES_CACHE: dict[tuple[int, float], list[tuple[dict, dict]]] = {}
_HOPEFUL_SOURCES_CACHE: dict[int, list[tuple[dict, dict]]] = {}
_HYBRID_BREAKER_PEERS_CACHE: dict[tuple[int, str], list[dict]] = {}
_TEMPLATE_EXPRESSION_CACHE: dict[str, str] = {}
_TEMPLATE_SETTINGS_CACHE: dict[str, dict] = {}
_FAMILY_FEEDBACK_CACHE: dict[str, dict] | None = None
_RAW_FAMILY_ACTIONS_CACHE: dict[str, dict] | None = None


WINDOWS = [5, 10, 15, 20, 30, 40, 60, 90, 120, 180, 252]
WRAPPERS = [
    "identity",
    "reverse",
    "rank",
    "zscore",
    "winsorize_4",
    "ts_rank_20",
    "ts_rank_60",
    "ts_rank_120",
    "ts_mean_5",
    "ts_mean_20",
    "ts_mean_60",
    "trade_when_volume_rank",
    "trade_when_low_volatility",
    "trade_when_recent_volume_peak",
]
MUTATION_OPERATORS = ["ts_rank", "zscore", "ts_mean", "ts_delta", "winsorize", "ts_backfill", "reverse"]

TS_WRAPPER_PREFIXES = ("ts_rank_", "ts_mean_")
NON_CORE_SUBMIT_CHECKS = {"SELF_CORRELATION", "MATCHES_COMPETITION", "ALREADY_SUBMITTED"}
SEVERE_EXPLOIT_FAIL_CHECKS = {"CONCENTRATED_WEIGHT"}
SEVERE_EXPLOIT_ERROR_CHECKS = {"LOW_SUB_UNIVERSE_SHARPE", "LOW_FITNESS"}
FAMILY_FEEDBACK_REPORT_FILE = ROOT_DIR / "result_store" / "analysis" / "family_feedback_report.json"
RAW_FAMILY_ACTIONS_FILE = ROOT_DIR / "result_store" / "analysis" / "raw_family_actions.json"


def slugify(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9_]+", "_", value).strip("_").lower() or "supply"


def slugify_template_name(value: str) -> str:
    normalized = re.sub(r"\{\{FIELD(?:_[A-Z0-9]+)?_SLUG\}\}", "field", value)
    normalized = re.sub(r"\{\{FIELD(?:_[A-Z0-9]+)?\}\}", "field", normalized)
    return slugify(normalized)


def load_config(path: Path) -> dict:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Invalid config: {path}")
    return payload


def load_template(path: Path) -> dict:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Invalid template: {path}")
    return payload


def load_runtime_guardrails() -> dict:
    if not RUNTIME_GUARDRAILS_FILE.exists():
        return {}
    try:
        payload = json.loads(RUNTIME_GUARDRAILS_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def active_wrappers() -> list[str]:
    payload = load_runtime_guardrails()
    disabled = {
        str(item).strip()
        for item in (payload.get("disabled_wrappers") or [])
        if str(item).strip()
    }
    return [wrapper for wrapper in WRAPPERS if wrapper not in disabled]


def _safe_float(value, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def result_payloads() -> list[dict]:
    global _RESULT_PAYLOAD_CACHE
    if _RESULT_PAYLOAD_CACHE is None:
        _RESULT_PAYLOAD_CACHE = list(iter_all_result_payloads())
    return _RESULT_PAYLOAD_CACHE


def load_json_file(path: Path) -> dict:
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def load_generation_state() -> dict:
    if not SUPPLY_GENERATION_STATE_FILE.exists():
        return {
            "generation_id": 0,
            "history": [],
            "last_completed_at": None,
            "family_lifecycle": {},
            "retired_families": [],
        }
    try:
        payload = json.loads(SUPPLY_GENERATION_STATE_FILE.read_text(encoding="utf-8"))
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    payload.setdefault("generation_id", 0)
    payload.setdefault("history", [])
    payload.setdefault("last_completed_at", None)
    payload.setdefault("family_lifecycle", {})
    payload.setdefault("retired_families", [])
    return payload


def finalize_generation_state(state: dict, summary: dict) -> dict:
    generation_id = int(state.get("generation_id") or 0) + 1
    state["generation_id"] = generation_id
    history = state.setdefault("history", [])
    history.append(summary)
    if len(history) > 40:
        del history[:-40]
    state["last_completed_at"] = utc_now()
    return state


def generation_policy_config(supply_cfg: dict) -> dict:
    policy = copy.deepcopy(supply_cfg.get("generation_policy") or {})
    policy.setdefault("family_job_cap", 6)
    policy.setdefault("elite_family_bonus_cap", 2)
    policy.setdefault("retire_degrading_streak", 3)
    policy.setdefault("retire_max_top_reward", 1.2)
    policy.setdefault("reactivate_health_score", 0.72)
    policy.setdefault("reactivate_top_reward", 1.8)
    policy.setdefault("min_pair_jobs", 12)
    policy.setdefault("min_lateral_jobs", 6)
    policy.setdefault("min_mutation_jobs", 4)
    policy.setdefault("min_crossover_jobs", 4)
    policy.setdefault("lineage_soft_job_cap", 80)
    return policy


def research_novelty_policy_config(supply_cfg: dict) -> dict:
    policy = copy.deepcopy(supply_cfg.get("research_novelty_mode") or {})
    policy.setdefault("enabled", True)
    policy.setdefault("field_cluster_job_cap", 34)
    policy.setdefault("expression_signature_job_cap", 22)
    policy.setdefault("raw_family_job_cap", 44)
    policy.setdefault("mechanism_job_cap", 52)
    policy.setdefault("expression_family_job_cap", 38)
    policy.setdefault("source_lineage_job_cap", 14)
    policy.setdefault("max_exploit_share", 0.42)
    policy.setdefault("strict_fill_ratio", 0.82)
    policy.setdefault("penalty_per_extra_cluster", 0.08)
    policy.setdefault("penalty_per_extra_signature", 0.12)
    policy.setdefault("min_distinct_field_clusters", 6)
    policy.setdefault("cold_dataset_min_share", 0.12)
    policy.setdefault("underused_dataset_min_share", 0.22)
    policy.setdefault("family_feedback_enabled", True)
    policy.setdefault("family_feedback_promote_bonus", 0.75)
    policy.setdefault("family_feedback_refine_bonus", 0.18)
    policy.setdefault("family_feedback_observe_bonus", 0.08)
    policy.setdefault("family_feedback_retire_penalty", 1.15)
    return policy


def load_family_feedback_map() -> dict[str, dict]:
    global _FAMILY_FEEDBACK_CACHE
    if _FAMILY_FEEDBACK_CACHE is not None:
        return _FAMILY_FEEDBACK_CACHE
    if not FAMILY_FEEDBACK_REPORT_FILE.exists():
        _FAMILY_FEEDBACK_CACHE = {}
        return _FAMILY_FEEDBACK_CACHE
    try:
        payload = json.loads(FAMILY_FEEDBACK_REPORT_FILE.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        _FAMILY_FEEDBACK_CACHE = {}
        return _FAMILY_FEEDBACK_CACHE
    rows = payload.get("families") if isinstance(payload, dict) else []
    _FAMILY_FEEDBACK_CACHE = {
        str(row.get("family") or ""): row
        for row in (rows or [])
        if isinstance(row, dict) and row.get("family")
    }
    return _FAMILY_FEEDBACK_CACHE


def load_raw_family_actions_map() -> dict[str, dict]:
    global _RAW_FAMILY_ACTIONS_CACHE
    if _RAW_FAMILY_ACTIONS_CACHE is not None:
        return _RAW_FAMILY_ACTIONS_CACHE
    if not RAW_FAMILY_ACTIONS_FILE.exists():
        _RAW_FAMILY_ACTIONS_CACHE = {}
        return _RAW_FAMILY_ACTIONS_CACHE
    try:
        payload = json.loads(RAW_FAMILY_ACTIONS_FILE.read_text(encoding="utf-8", errors="ignore"))
    except Exception:
        _RAW_FAMILY_ACTIONS_CACHE = {}
        return _RAW_FAMILY_ACTIONS_CACHE
    rows = payload.get("actions") if isinstance(payload, dict) else []
    _RAW_FAMILY_ACTIONS_CACHE = {
        str(row.get("family") or ""): row
        for row in (rows or [])
        if isinstance(row, dict) and row.get("family")
    }
    return _RAW_FAMILY_ACTIONS_CACHE


def apply_family_feedback_priority(job: dict, policy: dict, feedback: dict[str, dict]) -> None:
    if not bool(policy.get("family_feedback_enabled", True)):
        return
    family = str(job.get("raw_alpha_family") or "")
    if not family:
        return
    row = feedback.get(family)
    if not row:
        return
    decision = str(row.get("decision") or "").lower()
    if decision == "promote":
        adjustment = float(policy.get("family_feedback_promote_bonus", 0.75))
    elif decision == "refine":
        adjustment = float(policy.get("family_feedback_refine_bonus", 0.18))
    elif decision == "observe":
        adjustment = float(policy.get("family_feedback_observe_bonus", 0.08))
    elif decision == "retire":
        adjustment = -float(policy.get("family_feedback_retire_penalty", 1.15))
    else:
        adjustment = 0.0
    if adjustment:
        job["family_feedback_decision"] = decision
        job["family_feedback_adjustment"] = round(adjustment, 6)
        job["supply_priority"] = _safe_float(job.get("supply_priority")) + adjustment


def apply_raw_family_action(job: dict, actions: dict[str, dict]) -> None:
    family = str(job.get("raw_alpha_family") or "")
    if not family:
        return
    row = actions.get(family)
    if not row:
        return
    decision = str(row.get("decision") or "").lower()
    priority_delta = _safe_float(row.get("priority_delta"))
    if priority_delta:
        job["raw_family_action_decision"] = decision
        job["raw_family_action_delta"] = round(priority_delta, 6)
        job["supply_priority"] = _safe_float(job.get("supply_priority")) + priority_delta
    if bool(row.get("rotation_paused")):
        job["raw_family_rotation_paused"] = True
    if row.get("resource_action"):
        job["raw_family_resource_action"] = str(row.get("resource_action"))
    if row.get("refine_focus"):
        job["raw_family_refine_focus"] = list(row.get("refine_focus") or [])


def window_variants(expr: str) -> list[tuple[str, str]]:
    variants = [("base", expr)]
    seen = {expr}
    for window in WINDOWS:
        replaced = re.sub(r"(ts_[a-z_]+\([^,]+,\s*)\d+(\s*\))", rf"\g<1>{window}\2", expr)
        if replaced not in seen:
            seen.add(replaced)
            variants.append((f"w{window}", replaced))
    return variants


def wrap_expr(expr: str, wrapper: str) -> str:
    if wrapper == "identity":
        return expr
    if wrapper == "reverse":
        return f"reverse({expr})"
    if wrapper == "rank":
        return expr if expr.strip().startswith(("rank(", "group_rank(")) else f"rank({expr})"
    if wrapper == "zscore":
        return f"zscore({expr})"
    if wrapper == "winsorize_4":
        return f"winsorize({expr}, std=4)"
    if wrapper == "ts_rank_20":
        return f"ts_rank({expr}, 20)"
    if wrapper == "ts_rank_60":
        return f"ts_rank({expr}, 60)"
    if wrapper == "ts_rank_120":
        return f"ts_rank({expr}, 120)"
    if wrapper == "ts_mean_5":
        return f"ts_mean({expr}, 5)"
    if wrapper == "ts_mean_20":
        return f"ts_mean({expr}, 20)"
    if wrapper == "ts_mean_60":
        return f"ts_mean({expr}, 60)"
    if wrapper == "vector_neut_volume":
        return f"vector_neut({expr}, volume)"
    if wrapper == "vector_neut_returns":
        return f"vector_neut({expr}, returns)"
    if wrapper == "regression_neut_volatility":
        return f"regression_neut({expr}, ts_std_dev(returns, 30))"
    if wrapper == "trade_when_volume_rank":
        return f"trade_when(ts_rank(volume, 20) > 0.55, {expr}, -1)"
    if wrapper == "trade_when_low_volatility":
        return f"trade_when(ts_rank(ts_std_dev(returns, 10), 252) < 0.9, {expr}, -1)"
    if wrapper == "trade_when_recent_volume_peak":
        return f"trade_when(ts_arg_max(volume, 5) == 0, {expr}, -1)"
    raise ValueError(f"Unknown wrapper: {wrapper}")


def wrapper_is_compatible(expr: str, wrapper: str) -> bool:
    profile = analyze_expression_compatibility(expr)
    if profile.parse_error:
        return False
    if wrapper.startswith(TS_WRAPPER_PREFIXES) and (
        profile.uses_cross_sectional_operator or profile.cross_sectional_result_inside_ts_operator
    ):
        return False
    if wrapper.startswith("vector_neut") and profile.uses_vector_operator:
        return False
    if wrapper.startswith("regression_neut") and "regression_neut(" in expr:
        return False
    if wrapper.startswith("trade_when") and "trade_when(" in expr:
        return False
    return True


def unwrap_field_expression(expr: str) -> str:
    # Convert concrete historical expressions back to a single-slot template.
    known_group_keys = {
        "industry",
        "sector",
        "subindustry",
        "market",
        "country",
        "exchange",
    }
    operators = {
        "abs",
        "add",
        "bucket",
        "densify",
        "divide",
        "group_rank",
        "group_zscore",
        "group_neutralize",
        "if_else",
        "log",
        "multiply",
        "rank",
        "reverse",
        "scale",
        "signed_power",
        "subtract",
        "ts_backfill",
        "ts_delta",
        "ts_mean",
        "ts_rank",
        "ts_std_dev",
        "ts_sum",
        "trade_when",
        "vector_neut",
        "regression_neut",
        "winsorize",
        "zscore",
    }
    tokens = re.findall(r"\b[A-Za-z_][A-Za-z0-9_]*\b", expr)
    field_candidates = [
        token
        for token in tokens
        if token not in operators
        and token not in known_group_keys
        and not token.isupper()
        and not token.startswith("P")
    ]
    if not field_candidates:
        return expr
    # Replace the longest concrete field first to avoid partial replacement.
    field = sorted(set(field_candidates), key=len, reverse=True)[0]
    return re.sub(rf"\b{re.escape(field)}\b", "{{FIELD}}", expr, count=1)


def infer_source_job(payload: dict, base_jobs: list[dict]) -> dict | None:
    category = str(payload.get("category") or "").lower()
    source_file = str(payload.get("source_file") or "").lower()
    batch_name = str(payload.get("batch_name") or "").lower()
    raw_rotation_active = any(job.get("raw_alpha_family") for job in base_jobs)
    for job in base_jobs:
        job_name = str(job.get("name") or "").lower()
        job_category = str(job.get("category") or "").lower()
        if job_name and (job_name in source_file or job_name in batch_name):
            return job
        raw_family = str(job.get("raw_alpha_family") or "").lower()
        if raw_family and (raw_family in source_file or raw_family in batch_name):
            return job
        if category and job_category and category == job_category:
            if not raw_rotation_active:
                return job
    if raw_rotation_active:
        return None
    return base_jobs[0] if base_jobs else None


def align_job_field_sources(job: dict, variant: dict) -> None:
    slot_specs = job.get("slot_inventories") or {}
    if not isinstance(slot_specs, dict) or not slot_specs:
        return
    slots = extract_field_slots(variant)
    if not slots:
        job.pop("slot_inventories", None)
        return
    if slots == ["FIELD"]:
        preferred_spec = None
        job_category = str(job.get("category") or "").lower()
        for spec in slot_specs.values():
            if not isinstance(spec, dict):
                continue
            if str(spec.get("category") or "").lower() == job_category:
                preferred_spec = spec
                break
        if preferred_spec is None:
            preferred_spec = next((spec for spec in slot_specs.values() if isinstance(spec, dict)), None)
        if preferred_spec:
            for key in [
                "refresh_inventory",
                "inventory_stale_after_minutes",
                "instrument_type",
                "region",
                "delay",
                "universe",
                "limit",
                "inventory_fetch_limit",
                "selection_limit",
                "discover_datasets",
                "dataset_categories",
                "max_datasets",
                "per_dataset_limit",
                "selection_query",
                "min_selection_score",
                "category",
            ]:
                if key in preferred_spec:
                    job[key] = copy.deepcopy(preferred_spec[key])
            base_inventory_name = (
                str(job.get("inventory_name") or "")
                or str(job.get("name") or "")
                or str(variant.get("name") or "")
            )
            job["inventory_name"] = slugify(f"{base_inventory_name}_field")
            job.pop("slot_inventories", None)
        return
    filtered = {
        slot: copy.deepcopy(spec)
        for slot, spec in slot_specs.items()
        if slot in slots and isinstance(spec, dict)
    }
    if filtered:
        job["slot_inventories"] = filtered
    else:
        job.pop("slot_inventories", None)


def result_score(payload: dict) -> float:
    details = payload.get("alpha_details") or {}
    is_block = details.get("is") or {}
    sharpe = float(is_block.get("sharpe") or 0)
    fitness = float(is_block.get("fitness") or 0)
    returns = float(is_block.get("returns") or 0)
    failed_checks = len(
        [
            check
            for check in (((details.get("submitPreview") or {}).get("is") or {}).get("checks") or [])
            if check.get("result") == "FAIL" and check.get("name") != "ALREADY_SUBMITTED"
        ]
    )
    return sharpe + fitness + returns * 5 - failed_checks * 0.5


def submit_checks(payload: dict) -> list[dict]:
    details = payload.get("alpha_details") or {}
    checks = (((details.get("submitPreview") or {}).get("is") or {}).get("checks") or [])
    if checks:
        return [check for check in checks if isinstance(check, dict)]
    checks = (details.get("is") or {}).get("checks") or []
    return [check for check in checks if isinstance(check, dict)]


def effective_pass_count(payload: dict) -> int:
    count = 0
    for check in submit_checks(payload):
        name = str(check.get("name") or "")
        result = str(check.get("result") or "")
        if name in NON_CORE_SUBMIT_CHECKS:
            continue
        if result == "PASS":
            count += 1
    return count


def exploit_quality_penalty(payload: dict) -> tuple[float, list[str]]:
    penalty = 0.0
    reasons: list[str] = []
    checks = submit_checks(payload)
    pass_count = effective_pass_count(payload)

    for check in checks:
        name = str(check.get("name") or "")
        result = str(check.get("result") or "")
        if result == "FAIL" and name in SEVERE_EXPLOIT_FAIL_CHECKS:
            penalty += 2.8
            reasons.append(name.lower())
        elif result == "ERROR" and name in SEVERE_EXPLOIT_ERROR_CHECKS:
            penalty += 0.8
            reasons.append(f"{name.lower()}_error")

    failed = set(failed_check_names(payload))
    if pass_count >= 6 and "CONCENTRATED_WEIGHT" not in failed:
        penalty -= 0.45
        reasons.append("near_submit")
    elif pass_count >= 5 and "CONCENTRATED_WEIGHT" not in failed:
        penalty -= 0.2
        reasons.append("promising")
    if "CONCENTRATED_WEIGHT" in failed and {"LOW_SHARPE", "LOW_FITNESS"} & failed:
        penalty += 0.9
        reasons.append("structural_concentration_combo")

    return penalty, reasons


def is_structural_concentration_breed_risk(payload: dict) -> bool:
    failed = set(failed_check_names(payload))
    if "CONCENTRATED_WEIGHT" not in failed:
        return False
    if {"LOW_SHARPE", "LOW_FITNESS"} & failed:
        return True
    if "HIGH_TURNOVER" in failed:
        return True
    return effective_pass_count(payload) < 7


def is_hopeful_payload(payload: dict) -> bool:
    details = payload.get("alpha_details") or {}
    is_block = details.get("is") or {}
    sharpe = float(is_block.get("sharpe") or 0)
    fitness = float(is_block.get("fitness") or 0)
    returns = float(is_block.get("returns") or 0)
    margin = float(is_block.get("margin") or 0)
    return any(
        [
            sharpe < -1.25,
            fitness < -1.0,
            returns < -0.2,
            margin < -0.002,
        ]
    )


def is_repair_derived_payload(payload: dict) -> bool:
    haystack = " ".join(
        [
            str(payload.get("source_file") or ""),
            str(payload.get("batch_name") or ""),
            str(payload.get("name") or ""),
            " ".join(str(tag) for tag in (payload.get("tags") or [])),
        ]
    ).lower()
    return "high_grade_repair" in haystack or "repair_" in haystack


def is_high_quality_payload(payload: dict) -> bool:
    details = payload.get("alpha_details") or {}
    grade = str(details.get("grade") or payload.get("grade") or "").upper()
    color = str(details.get("color") or payload.get("color") or "").upper()
    tags = {str(tag).upper() for tag in (payload.get("tags") or [])}
    return grade in {"EXCELLENT", "SPECTACULAR"} or color in {"BLUE", "PURPLE"} or bool(tags & {"1EXCELLENT", "1SPECTACULAR"})


def is_high_quality_repair_payload(payload: dict) -> bool:
    return is_repair_derived_payload(payload) and is_high_quality_payload(payload)


def is_high_grade_payload(payload: dict) -> bool:
    details = payload.get("alpha_details") or {}
    grade = str(details.get("grade") or payload.get("grade") or "").upper()
    return grade in {"EXCELLENT", "SPECTACULAR"}


def failed_check_names(payload: dict) -> list[str]:
    return [
        str(check.get("name") or "").upper()
        for check in submit_checks(payload)
        if str(check.get("result") or "").upper() == "FAIL" and str(check.get("name") or "")
    ]


def has_concentrated_weight_fail(payload: dict) -> bool:
    return "CONCENTRATED_WEIGHT" in set(failed_check_names(payload))


def hybrid_base_score(payload: dict) -> float:
    details = payload.get("alpha_details") or {}
    grade = str(details.get("grade") or payload.get("grade") or "").upper()
    failed = set(failed_check_names(payload))
    score = result_score(payload) + effective_pass_count(payload) * 0.18
    if grade == "SPECTACULAR":
        score += 2.4
    elif grade == "EXCELLENT":
        score += 1.7
    elif grade == "GOOD":
        score += 0.7
    elif grade == "AVERAGE":
        score += 0.35
    if is_repair_derived_payload(payload):
        score += 0.45
    if "SELF_CORRELATION" in failed or "PROD_CORRELATION" in failed:
        score += 0.65
    if "CONCENTRATED_WEIGHT" in failed:
        score -= 1.25
        if is_high_grade_payload(payload) or is_high_quality_repair_payload(payload):
            score += 0.35
    return score


def historical_job_score(job_name: str) -> float:
    if not job_name:
        return 0.0
    cached = _HISTORICAL_JOB_SCORE_CACHE.get(job_name)
    if cached is not None:
        return cached
    scores: list[float] = []
    probe = str(job_name).lower()
    for payload in result_payloads():
        source_file = str(payload.get("source_file") or "").lower()
        batch_name = str(payload.get("batch_name") or "").lower()
        if probe not in source_file and probe not in batch_name:
            continue
        scores.append(result_score(payload))
    if not scores:
        _HISTORICAL_JOB_SCORE_CACHE[job_name] = 0.0
        return 0.0
    scores.sort(reverse=True)
    top_slice = scores[: min(len(scores), 20)]
    score = sum(top_slice) / len(top_slice)
    _HISTORICAL_JOB_SCORE_CACHE[job_name] = score
    return score


def settings_signature(settings: dict) -> str:
    payload = settings or {}
    return json.dumps(
        {
            "delay": payload.get("delay"),
            "neutralization": payload.get("neutralization"),
            "decay": payload.get("decay"),
            "truncation": payload.get("truncation"),
        },
        sort_keys=True,
        ensure_ascii=False,
    )


def expression_signature_for_expression(expression: str) -> str:
    expression = str(expression or "").lower()
    if not expression:
        return "unknown"
    operators = re.findall(r"\b([a-z_][a-z0-9_]*)\s*\(", expression)
    operator_chain = "_".join(operators[:8]) or extract_main_operator(expression)
    normalized = re.sub(r"\b[a-z][a-z0-9_]*\b", "field", expression)
    normalized = re.sub(r"\d+", "n", normalized)
    normalized = re.sub(r"\s+", "", normalized)
    return template_hash(f"{operator_chain}:{normalized}")[:16]


def historical_settings_score(settings: dict) -> float:
    target = settings_signature(settings)
    cached = _HISTORICAL_SETTINGS_SCORE_CACHE.get(target)
    if cached is not None:
        return cached
    scores: list[float] = []
    for payload in result_payloads():
        existing = payload.get("settings") or {}
        if settings_signature(existing) != target:
            continue
        scores.append(result_score(payload))
    if not scores:
        _HISTORICAL_SETTINGS_SCORE_CACHE[target] = 0.0
        return 0.0
    scores.sort(reverse=True)
    top_slice = scores[: min(len(scores), 30)]
    score = sum(top_slice) / len(top_slice)
    _HISTORICAL_SETTINGS_SCORE_CACHE[target] = score
    return score


def reward_score(payload: dict) -> float:
    details = payload.get("alpha_details") or {}
    is_block = details.get("is") or {}
    sharpe = float(is_block.get("sharpe") or 0)
    fitness = float(is_block.get("fitness") or 0)
    returns = float(is_block.get("returns") or 0)
    margin = float(is_block.get("margin") or 0)
    turnover = abs(float(is_block.get("turnover") or 0))
    reward = sharpe * 0.45 + fitness * 0.35 + returns * 3.0 + margin * 150.0
    if turnover > 0.7:
        reward -= 0.3
    if sharpe > 2.0 and fitness > 2.0:
        reward += 0.4
    return reward


def family_key_for_payload(payload: dict) -> str:
    source_file = str(payload.get("source_file") or "").strip()
    batch_name = str(payload.get("batch_name") or "").strip()
    category = slugify(str(payload.get("category") or "unknown"))
    if source_file.startswith("<slot_miner:") and source_file.endswith(">"):
        core = source_file[1:-1]
        parts = core.split(":")
        if len(parts) >= 3:
            return slugify(f"{parts[1]}::{parts[2]}")
    if batch_name:
        return slugify(batch_name)
    if source_file:
        return slugify(source_file)
    expression = str(payload.get("expression") or "").strip()
    if expression:
        return slugify(f"{category}::{template_hash(expression)[:20]}")
    alpha_id = str(payload.get("alpha_id") or payload.get("name") or "payload")
    return slugify(f"{category}::{alpha_id}")


def compute_health_metrics(scores: list[float]) -> dict:
    if not scores:
        return {
            "count": 0,
            "avg_reward": 0.0,
            "recent_avg_reward": 0.0,
            "older_avg_reward": 0.0,
            "top_reward": 0.0,
            "health_score": 0.5,
            "is_degrading": False,
        }
    recent_window = min(10, len(scores))
    recent_scores = scores[-recent_window:]
    older_scores = scores[:-recent_window]
    avg_reward = sum(scores) / len(scores)
    recent_avg = sum(recent_scores) / len(recent_scores)
    older_avg = sum(older_scores) / len(older_scores) if older_scores else recent_avg
    top_reward = max(scores)
    stability = 1.0 / (1.0 + (max(scores) - min(scores)) / max(len(scores), 3))
    trend_bonus = 0.0
    if older_scores:
        trend_bonus = max(-0.35, min(0.35, (recent_avg - older_avg) * 0.12))
    health_score = max(0.0, min(1.0, 0.55 + trend_bonus + stability * 0.25 + min(0.2, max(0.0, avg_reward) * 0.05)))
    return {
        "count": len(scores),
        "avg_reward": avg_reward,
        "recent_avg_reward": recent_avg,
        "older_avg_reward": older_avg,
        "top_reward": top_reward,
        "health_score": health_score,
        "is_degrading": bool(older_scores and recent_avg < older_avg * 0.82),
    }


def build_population_archive(limit: int = 1200) -> dict:
    payloads = result_payloads()
    if not payloads:
        return {
            "family_count": 0,
            "elite_families": [],
            "degrading_families": [],
            "family_metrics": {},
            "lineage_summary": {},
        }

    families: dict[str, dict] = {}
    for payload in payloads[-limit:]:
        family_key = family_key_for_payload(payload)
        bucket = families.setdefault(
            family_key,
            {
                "family_key": family_key,
                "category": str(payload.get("category") or "").lower(),
                "scores": [],
                "top_reward": -999.0,
                "best_alpha_id": "",
                "sample_expression": str(payload.get("expression") or ""),
                "batch_names": set(),
            },
        )
        score = reward_score(payload)
        bucket["scores"].append(score)
        if score > bucket["top_reward"]:
            bucket["top_reward"] = score
            bucket["best_alpha_id"] = str(payload.get("alpha_id") or "")
            bucket["sample_expression"] = str(payload.get("expression") or "")
        batch_name = str(payload.get("batch_name") or "").strip()
        if batch_name:
            bucket["batch_names"].add(batch_name)

    family_rows: list[dict] = []
    for family_key, bucket in families.items():
        metrics = compute_health_metrics(bucket["scores"])
        row = {
            "family_key": family_key,
            "category": bucket["category"],
            "count": metrics["count"],
            "avg_reward": round(metrics["avg_reward"], 6),
            "recent_avg_reward": round(metrics["recent_avg_reward"], 6),
            "older_avg_reward": round(metrics["older_avg_reward"], 6),
            "top_reward": round(metrics["top_reward"], 6),
            "health_score": round(metrics["health_score"], 6),
            "is_degrading": metrics["is_degrading"],
            "best_alpha_id": bucket["best_alpha_id"],
            "sample_expression": bucket["sample_expression"],
            "batch_names": sorted(bucket["batch_names"])[:5],
        }
        family_rows.append(row)

    family_rows.sort(
        key=lambda item: (
            -float(item.get("health_score") or 0.0),
            -float(item.get("top_reward") or 0.0),
            -int(item.get("count") or 0),
        )
    )
    elite = family_rows[: min(len(family_rows), 20)]
    degrading = [row for row in family_rows if row.get("is_degrading")]
    family_metrics = {
        row["family_key"]: {
            "health_score": row["health_score"],
            "avg_reward": row["avg_reward"],
            "top_reward": row["top_reward"],
            "is_degrading": row["is_degrading"],
            "count": row["count"],
            "category": row["category"],
        }
        for row in family_rows
    }
    lineage_summary = {
        row["family_key"]: {
            "family_key": row["family_key"],
            "category": row["category"],
            "health_score": row["health_score"],
            "avg_reward": row["avg_reward"],
            "top_reward": row["top_reward"],
            "is_degrading": row["is_degrading"],
            "count": row["count"],
            "best_alpha_id": row["best_alpha_id"],
        }
        for row in family_rows
    }
    return {
        "family_count": len(family_rows),
        "elite_families": elite,
        "degrading_families": degrading[: min(len(degrading), 20)],
        "family_metrics": family_metrics,
        "lineage_summary": lineage_summary,
    }


def extract_main_operator(expression: str) -> str:
    expr = str(expression or "").strip()
    if not expr:
        return "unknown"
    match = re.match(r"([A-Za-z_][A-Za-z0-9_]*)\s*\(", expr)
    if match:
        return match.group(1).lower()
    return "raw"


def extract_strategy_tags(payload: dict) -> list[str]:
    raw_tags = payload.get("tags") or []
    tags = [slugify(str(tag)) for tag in raw_tags if str(tag).strip()]
    blocked_prefixes = ("supply_",)
    blocked_exact = {
        "night_search",
        "batch",
        "alpha",
        "template",
    }
    preferred = {"momentum", "quality", "valuation", "analyst", "sentiment", "fundamental", "mean_reversion"}
    strategies: list[str] = []
    for tag in tags:
        if not tag or tag in blocked_exact or tag.startswith(blocked_prefixes):
            continue
        if tag in preferred:
            strategies.append(tag)
            continue
        if re.fullmatch(r"[a-z]+[0-9_]*", tag):
            continue
        strategies.append(tag)
    deduped: list[str] = []
    seen: set[str] = set()
    for tag in strategies:
        if tag in seen:
            continue
        seen.add(tag)
        deduped.append(tag)
    return deduped[:4]


def build_hierarchical_bandit_summary(limit: int = 1200) -> dict:
    payloads = result_payloads()
    if not payloads:
        return {
            "category_bandit": {},
            "strategy_bandit": {},
            "operator_bandit": {},
            "quality_feedback": {},
        }

    category_scores: dict[str, list[float]] = {}
    strategy_scores: dict[str, list[float]] = {}
    operator_scores: dict[str, list[float]] = {}
    quality_penalties: list[float] = []
    quality_reason_counts: dict[str, int] = {}
    for payload in payloads[-limit:]:
        reward = reward_score(payload)
        category = slugify(str(payload.get("category") or "unknown"))
        category_scores.setdefault(category, []).append(reward)
        for strategy in extract_strategy_tags(payload):
            strategy_scores.setdefault(strategy, []).append(reward)
        operator = extract_main_operator(str(payload.get("expression") or ""))
        operator_scores.setdefault(operator, []).append(reward)
        penalty, reasons = exploit_quality_penalty(payload)
        quality_penalties.append(penalty)
        for reason in reasons:
            quality_reason_counts[reason] = quality_reason_counts.get(reason, 0) + 1

    def summarize(score_map: dict[str, list[float]], top_n: int = 20) -> dict[str, dict]:
        summary: dict[str, dict] = {}
        for key, scores in score_map.items():
            if not scores:
                continue
            top_scores = sorted(scores, reverse=True)[: min(len(scores), top_n)]
            avg_reward = sum(top_scores) / len(top_scores)
            success_rate = len([score for score in top_scores if score >= 1.0]) / len(top_scores)
            summary[key] = {
                "count": len(scores),
                "avg_reward": round(avg_reward, 6),
                "success_rate": round(success_rate, 6),
            }
        return summary

    return {
        "category_bandit": summarize(category_scores),
        "strategy_bandit": summarize(strategy_scores),
        "operator_bandit": summarize(operator_scores),
        "quality_feedback": {
            "avg_penalty": round(sum(quality_penalties) / len(quality_penalties), 6) if quality_penalties else 0.0,
            "top_reasons": sorted(quality_reason_counts.items(), key=lambda item: (-item[1], item[0]))[:12],
        },
    }


def build_lineage_summary(
    jobs: list[dict],
    lineage_summary: dict[str, dict] | None = None,
) -> dict:
    lineage_summary = lineage_summary or {}
    family_job_counts: dict[str, dict[str, int]] = {}
    for job in jobs:
        family_key = str(job.get("source_family_key") or "").strip()
        if not family_key:
            continue
        bucket = family_job_counts.setdefault(
            family_key,
            {
                "family_key": family_key,
                "exploit_jobs": 0,
                "pair_jobs": 0,
                "mutation_jobs": 0,
                "crossover_jobs": 0,
                "lateral_jobs": 0,
                "hopeful_jobs": 0,
                "total_jobs": 0,
                "best_priority": 0.0,
            },
        )
        tags = [str(tag) for tag in (job.get("tags") or [])]
        bucket["total_jobs"] += 1
        if str(job.get("supply_mode") or "") == "exploit":
            bucket["exploit_jobs"] += 1
        if "supply_pair" in tags or int(job.get("field_slot_count") or 0) > 1:
            bucket["pair_jobs"] += 1
        if "supply_mutation" in tags:
            bucket["mutation_jobs"] += 1
        if "supply_crossover" in tags:
            bucket["crossover_jobs"] += 1
        if "supply_lateral" in tags:
            bucket["lateral_jobs"] += 1
        if "supply_hopeful_negation" in tags:
            bucket["hopeful_jobs"] += 1
        bucket["best_priority"] = max(bucket["best_priority"], _safe_float(job.get("supply_priority")))

    rows: list[dict] = []
    for family_key, counts in family_job_counts.items():
        lineage = lineage_summary.get(family_key) or {}
        rows.append(
            {
                **counts,
                "health_score": _safe_float(lineage.get("health_score"), 0.5),
                "avg_reward": _safe_float(lineage.get("avg_reward")),
                "top_reward": _safe_float(lineage.get("top_reward")),
                "is_degrading": bool(lineage.get("is_degrading")),
                "category": str(lineage.get("category") or ""),
                "best_alpha_id": str(lineage.get("best_alpha_id") or ""),
            }
        )
    rows.sort(
        key=lambda item: (
            -_safe_float(item.get("health_score")),
            -_safe_float(item.get("best_priority")),
            -int(item.get("total_jobs") or 0),
        )
    )
    return {
        "family_job_count": len(rows),
        "families": rows[: min(len(rows), 40)],
    }


def build_generation_summary(
    generation_id: int,
    jobs: list[dict],
    exploit_source_count: int,
    hopeful_source_count: int,
    population_archive: dict,
    lineage_rollup: dict,
    generation_state: dict | None = None,
) -> dict:
    generation_state = generation_state or {}
    exploit_jobs = [job for job in jobs if str(job.get("supply_mode") or "") == "exploit"]
    explore_jobs = [job for job in jobs if str(job.get("supply_mode") or "") == "explore"]
    pair_jobs = [job for job in jobs if int(job.get("field_slot_count") or 0) > 1]
    mutation_jobs = [job for job in jobs if "supply_mutation" in [str(tag) for tag in (job.get("tags") or [])]]
    crossover_jobs = [job for job in jobs if "supply_crossover" in [str(tag) for tag in (job.get("tags") or [])]]
    lateral_jobs = [job for job in jobs if "supply_lateral" in [str(tag) for tag in (job.get("tags") or [])]]
    hopeful_jobs = [job for job in jobs if "supply_hopeful_negation" in [str(tag) for tag in (job.get("tags") or [])]]
    elite_families = population_archive.get("elite_families") or []
    return {
        "generation_id": generation_id,
        "generated_at": utc_now(),
        "explore_jobs": len(explore_jobs),
        "exploit_jobs": len(exploit_jobs),
        "pair_jobs": len(pair_jobs),
        "mutation_jobs": len(mutation_jobs),
        "crossover_jobs": len(crossover_jobs),
        "lateral_jobs": len(lateral_jobs),
        "hopeful_jobs": len(hopeful_jobs),
        "exploit_source_count": exploit_source_count,
        "hopeful_source_count": hopeful_source_count,
        "elite_family_count": len(elite_families),
        "lineage_family_count": int(lineage_rollup.get("family_job_count") or 0),
        "retired_family_count": len(generation_state.get("retired_families") or []),
        "top_elite_families": [
            {
                "family_key": row.get("family_key"),
                "health_score": row.get("health_score"),
                "top_reward": row.get("top_reward"),
                "category": row.get("category"),
            }
            for row in elite_families[:8]
        ],
    }


def is_pair_job(job: dict) -> bool:
    tags = {str(tag) for tag in (job.get("tags") or [])}
    return "supply_pair" in tags or int(job.get("field_slot_count") or 0) > 1


def has_job_tag(job: dict, tag: str) -> bool:
    return tag in {str(item) for item in (job.get("tags") or [])}


def update_family_lifecycle_state(
    generation_state: dict,
    population_archive: dict,
    generation_id: int,
    policy: dict,
) -> dict:
    lifecycle = copy.deepcopy(generation_state.get("family_lifecycle") or {})
    family_metrics = population_archive.get("family_metrics") or {}
    retired_families: set[str] = set(generation_state.get("retired_families") or [])
    retire_degrading_streak = int(policy.get("retire_degrading_streak", 3))
    retire_max_top_reward = _safe_float(policy.get("retire_max_top_reward"), 1.2)
    reactivate_health_score = _safe_float(policy.get("reactivate_health_score"), 0.72)
    reactivate_top_reward = _safe_float(policy.get("reactivate_top_reward"), 1.8)

    for family_key, metrics in family_metrics.items():
        entry = lifecycle.setdefault(
            family_key,
            {
                "degrading_streak": 0,
                "healthy_streak": 0,
                "retired": False,
                "last_generation_id": generation_id,
            },
        )
        is_degrading = bool(metrics.get("is_degrading"))
        health_score = _safe_float(metrics.get("health_score"), 0.5)
        top_reward = _safe_float(metrics.get("top_reward"))
        avg_reward = _safe_float(metrics.get("avg_reward"))
        entry["last_generation_id"] = generation_id
        entry["last_health_score"] = round(health_score, 6)
        entry["last_top_reward"] = round(top_reward, 6)
        entry["last_avg_reward"] = round(avg_reward, 6)
        entry["category"] = str(metrics.get("category") or "")

        if is_degrading:
            entry["degrading_streak"] = int(entry.get("degrading_streak") or 0) + 1
            entry["healthy_streak"] = 0
        else:
            entry["degrading_streak"] = 0
            entry["healthy_streak"] = int(entry.get("healthy_streak") or 0) + 1

        should_retire = (
            int(entry.get("degrading_streak") or 0) >= retire_degrading_streak
            and top_reward <= retire_max_top_reward
        )
        can_reactivate = (
            health_score >= reactivate_health_score
            or top_reward >= reactivate_top_reward
        )
        if should_retire:
            entry["retired"] = True
            entry["retired_at_generation"] = generation_id
            retired_families.add(family_key)
        elif bool(entry.get("retired")) and can_reactivate:
            entry["retired"] = False
            entry["reactivated_at_generation"] = generation_id
            retired_families.discard(family_key)

    generation_state["family_lifecycle"] = lifecycle
    generation_state["retired_families"] = sorted(retired_families)
    return generation_state


def load_lineage_warehouse() -> dict:
    if not SUPPLY_LINEAGE_WAREHOUSE_FILE.exists():
        return {
            "schema_version": 1,
            "updated_at": None,
            "lineages": {},
        }
    try:
        payload = json.loads(SUPPLY_LINEAGE_WAREHOUSE_FILE.read_text(encoding="utf-8"))
    except Exception:
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    payload.setdefault("schema_version", 1)
    payload.setdefault("updated_at", None)
    payload.setdefault("lineages", {})
    if not isinstance(payload.get("lineages"), dict):
        payload["lineages"] = {}
    return payload


def save_lineage_warehouse(payload: dict) -> None:
    payload["updated_at"] = utc_now()
    SUPPLY_DIR.mkdir(parents=True, exist_ok=True)
    SUPPLY_LINEAGE_WAREHOUSE_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def variant_family_kind(job: dict) -> str:
    tags = {str(tag) for tag in (job.get("tags") or [])}
    if "supply_mutation" in tags:
        return "mutation"
    if "supply_crossover" in tags:
        return "crossover"
    if "supply_lateral" in tags:
        return "lateral"
    if "supply_hopeful_negation" in tags:
        return "hopeful"
    if "supply_pair" in tags or int(job.get("field_slot_count") or 0) > 1:
        return "pair"
    return str(job.get("supply_mode") or "explore")


def expression_hash_for_job(job: dict) -> str:
    template_path = ROOT_DIR / str(job.get("template") or "")
    try:
        template = load_template(template_path)
    except Exception:
        return ""
    return template_hash(str(template.get("expression") or ""))


FIELD_CLUSTER_KEYWORDS: dict[str, tuple[str, ...]] = {
    "analyst_revision": ("anl", "analyst", "estimate", "revision", "eps", "recommend", "target"),
    "guidance": ("guidance", "forecast", "consensus", "surprise"),
    "sentiment_news": ("sentiment", "news", "social", "buzz", "media", "article"),
    "fundamental_quality": ("roe", "roa", "margin", "profit", "earnings", "income", "ebit", "ebitda"),
    "balance_sheet": ("asset", "liabil", "debt", "equity", "book", "cash", "inventory"),
    "cashflow": ("cashflow", "cash_flow", "fcf", "capex", "operating_cash"),
    "sales_growth": ("sales", "revenue", "turnover", "growth"),
    "price_volume": ("close", "open", "high", "low", "volume", "vwap", "returns", "adv"),
    "risk_volatility": ("risk", "beta", "volatility", "vol", "drawdown", "var"),
    "ownership_flow": ("holder", "holding", "insider", "institution", "ownership", "short_interest"),
    "macro_rates": ("macro", "rate", "yield", "inflation", "currency", "fx"),
    "model_factor": ("factor", "model", "score"),
}


def slot_dataset_clusters(job: dict) -> list[str]:
    clusters: list[str] = []
    for spec in job.get("slot_dataset_plan") or []:
        if not isinstance(spec, dict):
            continue
        seed_field = str(spec.get("seed_field") or "").lower()
        dataset_id = str(spec.get("dataset_id") or "").lower()
        category = str(spec.get("category") or "").lower()
        if "credit_risk_premium" in seed_field or "crp" in seed_field:
            clusters.append("credit_premium")
        elif "distress" in seed_field or seed_field.endswith("_dm"):
            clusters.append("distress_model")
        elif "repayment" in seed_field:
            clusters.append("debt_repayment")
        elif "interest" in seed_field:
            clusters.append("interest_burden")
        elif "issuance" in seed_field or "proceeds" in seed_field or "refinanc" in seed_field:
            clusters.append("debt_refinancing")
        elif "debt" in seed_field:
            clusters.append("debt_balance")
        elif dataset_id:
            clusters.append(f"dataset_{dataset_id}")
        elif category:
            clusters.append(f"category_{category}")
    return list(dict.fromkeys(clusters))


def template_expression_for_job(job: dict) -> str:
    template_ref = str(job.get("template") or "").strip()
    if not template_ref:
        return ""
    cached = _TEMPLATE_EXPRESSION_CACHE.get(template_ref)
    if cached is not None:
        return cached
    try:
        template = load_template(ROOT_DIR / template_ref)
        expression = str(template.get("expression") or "")
    except Exception:
        expression = ""
    _TEMPLATE_EXPRESSION_CACHE[template_ref] = expression
    return expression


def template_settings_for_job(job: dict) -> dict:
    template_ref = str(job.get("template") or "").strip()
    if not template_ref:
        return {}
    cached = _TEMPLATE_SETTINGS_CACHE.get(template_ref)
    if cached is not None:
        return cached
    try:
        template = load_template(ROOT_DIR / template_ref)
        settings = dict(template.get("settings") or {})
    except Exception:
        settings = {}
    _TEMPLATE_SETTINGS_CACHE[template_ref] = settings
    return settings


def text_for_novelty(job: dict) -> str:
    parts = [
        str(job.get("name") or ""),
        str(job.get("inventory_name") or ""),
        str(job.get("raw_alpha_family") or ""),
        str(job.get("mechanism_id") or job.get("research_mechanism_id") or ""),
        str(job.get("data_family") or ""),
        str(job.get("expression_family") or ""),
        str(job.get("anti_correlation_target") or ""),
        str(job.get("raw_alpha_domain") or ""),
        str(job.get("category") or ""),
        " ".join(str(tag) for tag in (job.get("tags") or [])),
        template_expression_for_job(job),
    ]
    field_selection = job.get("field_selection") or {}
    if isinstance(field_selection, dict):
        parts.extend(
            [
                " ".join(str(item) for item in (field_selection.get("blocked_terms") or [])),
                " ".join(str(item) for item in (field_selection.get("preferred_terms") or [])),
                " ".join(str(item) for item in (field_selection.get("required_terms") or [])),
            ]
        )
    return " ".join(part for part in parts if part).lower()


def field_clusters_for_job(job: dict) -> list[str]:
    planned_clusters = slot_dataset_clusters(job)
    if planned_clusters:
        clusters = planned_clusters[:3]
        if len(clusters) > 1:
            pivot_key = str(job.get("name") or job.get("template") or "")
            pivot = int(template_hash(pivot_key)[:8], 16) % len(clusters)
            clusters = clusters[pivot:] + clusters[:pivot]
        return clusters

    text = text_for_novelty(job)
    scored_clusters = []
    for cluster, keywords in FIELD_CLUSTER_KEYWORDS.items():
        score = sum(text.count(keyword) for keyword in keywords)
        if score > 0:
            scored_clusters.append((score, cluster))
    scored_clusters.sort(key=lambda item: (-item[0], item[1]))
    clusters = [cluster for _, cluster in scored_clusters[:3]]
    if not clusters:
        clusters = ["misc_research"]
    return list(dict.fromkeys(clusters))


def expression_signature_for_job(job: dict) -> str:
    return expression_signature_for_expression(template_expression_for_job(job))


def annotate_novelty_features(job: dict) -> dict:
    clusters = field_clusters_for_job(job)
    signature = expression_signature_for_job(job)
    job["field_clusters"] = clusters
    job["primary_field_cluster"] = clusters[0]
    job["expression_signature"] = signature
    dataset_tiers: list[str] = []
    dataset_scores: list[float] = []
    semantic_tags: list[str] = []
    semantic_overlap_tags: list[str] = []
    for spec in job.get("slot_dataset_plan") or []:
        if not isinstance(spec, dict):
            continue
        score = _safe_float(spec.get("dataset_exploration_score"))
        if score:
            dataset_scores.append(score)
        if score >= 4.0:
            dataset_tiers.append("cold_high_value")
        elif score >= 2.4:
            dataset_tiers.append("underused")
        elif score > 0:
            dataset_tiers.append("normal")
        semantic_tags.extend(str(tag) for tag in (spec.get("field_semantic_tags") or []) if str(tag))
        semantic_overlap_tags.extend(str(tag) for tag in (spec.get("semantic_tag_overlap") or []) if str(tag))
    if dataset_tiers:
        job["dataset_novelty_tiers"] = list(dict.fromkeys(dataset_tiers))
        job["dataset_exploration_score_avg"] = round(sum(dataset_scores) / len(dataset_scores), 6) if dataset_scores else 0.0
    if semantic_tags:
        job["field_semantic_tags"] = list(dict.fromkeys(semantic_tags))
    if semantic_overlap_tags:
        job["semantic_tag_overlap"] = list(dict.fromkeys(semantic_overlap_tags))
        job["semantic_fit_score"] = round(len(set(semantic_overlap_tags)) / max(1, len(set(semantic_tags))), 6)
    elif semantic_tags:
        job["semantic_fit_score"] = 0.0
    return job


def grounding_fit_bonus(job: dict) -> float:
    annotate_novelty_features(job)
    bonus = 0.0
    semantic_score = _safe_float(job.get("semantic_fit_score"))
    if semantic_score > 0:
        bonus += min(0.22, semantic_score * 0.28)
    elif job.get("field_semantic_tags"):
        bonus -= 0.12
    dataset_score = _safe_float(job.get("dataset_exploration_score_avg"))
    if dataset_score:
        bonus += min(0.14, dataset_score * 0.022)
    tiers = {str(item) for item in (job.get("dataset_novelty_tiers") or [])}
    if "cold_high_value" in tiers:
        bonus += 0.1
    elif "underused" in tiers:
        bonus += 0.06
    scoped_slots = 0
    total_slots = 0
    for spec in job.get("slot_dataset_plan") or []:
        if not isinstance(spec, dict):
            continue
        total_slots += 1
        if spec.get("dataset_scope_delay") is not None and spec.get("dataset_scope_universe"):
            scoped_slots += 1
    if total_slots:
        bonus += min(0.08, 0.08 * scoped_slots / total_slots)
    return round(bonus, 6)


def novelty_bucket_counts(jobs: list[dict], key: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    for job in jobs:
        value = job.get(key)
        if isinstance(value, list):
            values = [str(item) for item in value]
        else:
            values = [str(value or "")]
        for item in values:
            if not item:
                continue
            counts[item] = counts.get(item, 0) + 1
    return dict(sorted(counts.items(), key=lambda item: (-item[1], item[0])))


def settings_cluster_for_job(job: dict) -> str:
    settings = template_settings_for_job(job)
    payload = {
        "universe": settings.get("universe"),
        "delay": settings.get("delay"),
        "neutralization": settings.get("neutralization"),
        "decay": settings.get("decay"),
        "truncation": settings.get("truncation"),
        "pasteurization": settings.get("pasteurization"),
        "nanHandling": settings.get("nanHandling"),
    }
    return "|".join(f"{key}={payload.get(key)}" for key in sorted(payload))


def annotate_raw_family_features(job: dict) -> dict:
    annotate_novelty_features(job)
    job["settings_cluster"] = settings_cluster_for_job(job)
    return job


def job_metadata_record(job: dict, generation_id: int) -> dict:
    slot_plan = [spec for spec in (job.get("slot_dataset_plan") or []) if isinstance(spec, dict)]
    dataset_ids = list(dict.fromkeys(str(spec.get("dataset_id") or "") for spec in slot_plan if spec.get("dataset_id")))
    return {
        "job_name": str(job.get("name") or ""),
        "inventory_name": str(job.get("inventory_name") or ""),
        "template": str(job.get("template") or ""),
        "generation_id": generation_id,
        "archived_at": datetime.now(timezone.utc).isoformat(),
        "supply_mode": job.get("supply_mode"),
        "raw_alpha_family": job.get("raw_alpha_family"),
        "mechanism_id": job.get("mechanism_id"),
        "mechanism_theme": job.get("mechanism_theme"),
        "source_family_key": job.get("source_family_key"),
        "settings_cluster": job.get("settings_cluster"),
        "field_clusters": job.get("field_clusters"),
        "primary_field_cluster": job.get("primary_field_cluster"),
        "field_semantic_tags": job.get("field_semantic_tags") or [],
        "mechanism_required_semantic_tags": job.get("mechanism_required_semantic_tags") or [],
        "semantic_tag_overlap": job.get("semantic_tag_overlap") or [],
        "semantic_fit_score": job.get("semantic_fit_score"),
        "grounding_fit_bonus": job.get("grounding_fit_bonus"),
        "dataset_ids": dataset_ids,
        "slot_dataset_plan": slot_plan,
        "tags": job.get("tags") or [],
    }


def save_job_metadata_archive(jobs: list[dict], generation_id: int) -> dict:
    archive = load_json_file(JOB_METADATA_ARCHIVE_FILE)
    if not isinstance(archive, dict):
        archive = {}
    jobs_by_name = archive.get("jobs_by_name")
    if not isinstance(jobs_by_name, dict):
        jobs_by_name = {}
    templates_by_path = archive.get("templates_by_path")
    if not isinstance(templates_by_path, dict):
        templates_by_path = {}

    for job in jobs:
        record = job_metadata_record(job, generation_id)
        job_name = str(record.get("job_name") or "")
        template = str(record.get("template") or "")
        if job_name:
            jobs_by_name[job_name] = record
        if template and job_name:
            templates_by_path[template] = job_name

    payload = {
        "schema_version": 1,
        "updated_at": datetime.now(timezone.utc).isoformat(),
        "latest_generation_id": generation_id,
        "job_count": len(jobs_by_name),
        "template_count": len(templates_by_path),
        "jobs_by_name": jobs_by_name,
        "templates_by_path": templates_by_path,
    }
    JOB_METADATA_ARCHIVE_FILE.parent.mkdir(parents=True, exist_ok=True)
    JOB_METADATA_ARCHIVE_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "job_count": len(jobs_by_name),
        "template_count": len(templates_by_path),
        "latest_generation_id": generation_id,
    }


def ratio(numerator: int, denominator: int) -> float:
    if denominator <= 0:
        return 0.0
    return round(numerator / denominator, 6)


def raw_family_diversity_requirements(policy: dict) -> dict:
    requirements = dict(policy.get("raw_family_requirements") or {})
    requirements.setdefault("enabled", True)
    requirements.setdefault("min_jobs_for_gate", 12)
    requirements.setdefault("min_expression_signatures", 4)
    requirements.setdefault("max_top_expression_signature_share", 0.35)
    requirements.setdefault("min_field_clusters", 3)
    requirements.setdefault("max_top_field_cluster_share", 0.55)
    requirements.setdefault("min_settings_clusters", 3)
    requirements.setdefault("max_top_settings_cluster_share", 0.55)
    return requirements


def raw_family_diversity_report(jobs: list[dict], policy: dict) -> dict:
    requirements = raw_family_diversity_requirements(policy)
    groups: dict[str, list[dict]] = {}
    for job in jobs:
        family = str(job.get("raw_alpha_family") or "").strip()
        if not family:
            continue
        annotate_raw_family_features(job)
        groups.setdefault(family, []).append(job)

    reports: dict[str, dict] = {}
    for family, family_jobs in sorted(groups.items()):
        total = len(family_jobs)
        expression_counts = novelty_bucket_counts(family_jobs, "expression_signature")
        field_counts = novelty_bucket_counts(family_jobs, "field_clusters")
        primary_field_counts = novelty_bucket_counts(family_jobs, "primary_field_cluster")
        settings_counts = novelty_bucket_counts(family_jobs, "settings_cluster")
        top_expression = max(expression_counts.values()) if expression_counts else 0
        top_field = max(primary_field_counts.values()) if primary_field_counts else 0
        top_settings = max(settings_counts.values()) if settings_counts else 0
        top_field_share = ratio(top_field, total)
        checks = {
            "enough_jobs_for_gate": total >= int(requirements["min_jobs_for_gate"]),
            "expression_signatures": len(expression_counts) >= int(requirements["min_expression_signatures"]),
            "top_expression_signature_share": ratio(top_expression, total)
            <= float(requirements["max_top_expression_signature_share"]),
            "field_clusters": len(field_counts) >= int(requirements["min_field_clusters"]),
            "top_field_cluster_share": top_field_share
            <= float(requirements["max_top_field_cluster_share"]),
            "settings_clusters": len(settings_counts) >= int(requirements["min_settings_clusters"]),
            "top_settings_cluster_share": ratio(top_settings, total)
            <= float(requirements["max_top_settings_cluster_share"]),
        }
        gated = bool(requirements.get("enabled", True)) and checks["enough_jobs_for_gate"]
        passed = all(value for key, value in checks.items() if key != "enough_jobs_for_gate") if gated else None
        reports[family] = {
            "job_count": total,
            "gate_applied": gated,
            "passed": passed,
            "checks": checks,
            "requirements": requirements,
            "expression_signature_count": len(expression_counts),
            "top_expression_signature_share": ratio(top_expression, total),
            "field_cluster_count": len(field_counts),
            "top_primary_field_cluster_share": ratio(top_field, total),
            "settings_cluster_count": len(settings_counts),
            "top_settings_cluster_share": ratio(top_settings, total),
            "expression_signatures": expression_counts,
            "field_clusters": field_counts,
            "primary_field_clusters": primary_field_counts,
            "settings_clusters": settings_counts,
        }
    return {
        "enabled": bool(requirements.get("enabled", True)),
        "requirements": requirements,
        "families": reports,
    }


def enforce_raw_family_diversity_gate(
    selected: list[dict],
    candidate_pool: list[dict],
    max_jobs: int,
    policy: dict,
) -> tuple[list[dict], dict]:
    requirements = raw_family_diversity_requirements(policy)
    if not bool(requirements.get("enabled", True)):
        report = raw_family_diversity_report(selected, policy)
        report["gate_enforced"] = False
        return selected[:max_jobs], report

    selected_names = {str(job.get("name") or "") for job in selected}
    ordered_pool = [
        annotate_raw_family_features(job)
        for job in sorted(candidate_pool, key=lambda item: _safe_float(item.get("supply_priority")), reverse=True)
        if str(job.get("name") or "") not in selected_names
    ]
    output = [annotate_raw_family_features(job) for job in selected[:max_jobs]]

    def family_jobs(family: str) -> list[dict]:
        return [job for job in output if str(job.get("raw_alpha_family") or "") == family]

    families = sorted({str(job.get("raw_alpha_family") or "") for job in output if job.get("raw_alpha_family")})
    for family in families:
        jobs_for_family = family_jobs(family)
        if len(jobs_for_family) < int(requirements["min_jobs_for_gate"]):
            continue
        for key, min_count in [
            ("expression_signature", int(requirements["min_expression_signatures"])),
            ("primary_field_cluster", int(requirements["min_field_clusters"])),
            ("settings_cluster", int(requirements["min_settings_clusters"])),
        ]:
            current_values = set(str(job.get(key) or "") for job in jobs_for_family if job.get(key))
            while len(current_values) < min_count:
                replacement = next(
                    (
                        job
                        for job in ordered_pool
                        if str(job.get("raw_alpha_family") or "") == family
                        and str(job.get(key) or "") not in current_values
                    ),
                    None,
                )
                if replacement is None:
                    break
                drop_index = next(
                    (
                        index
                        for index, job in enumerate(output)
                        if str(job.get("raw_alpha_family") or "") == family
                        and str(job.get(key) or "") in current_values
                    ),
                    None,
                )
                if drop_index is None:
                    break
                dropped = output.pop(drop_index)
                selected_names.discard(str(dropped.get("name") or ""))
                output.append(replacement)
                selected_names.add(str(replacement.get("name") or ""))
                ordered_pool = [job for job in ordered_pool if job is not replacement]
                jobs_for_family = family_jobs(family)
                current_values = set(str(job.get(key) or "") for job in jobs_for_family if job.get(key))

    output = sorted(output, key=lambda item: _safe_float(item.get("supply_priority")), reverse=True)[:max_jobs]
    report = raw_family_diversity_report(output, policy)
    report["gate_enforced"] = True
    return output, report


def apply_research_novelty_policy(
    jobs: list[dict],
    candidate_pool: list[dict],
    max_jobs_per_cycle: int,
    policy: dict,
) -> tuple[list[dict], dict]:
    if not bool(policy.get("enabled", True)) or max_jobs_per_cycle <= 0:
        return jobs[:max_jobs_per_cycle], {"enabled": False}

    field_cap = int(policy.get("field_cluster_job_cap", 34))
    signature_cap = int(policy.get("expression_signature_job_cap", 22))
    raw_family_cap = int(policy.get("raw_family_job_cap", 44))
    mechanism_cap = int(policy.get("mechanism_job_cap", 52))
    expression_family_cap = int(policy.get("expression_family_job_cap", 38))
    lineage_cap = int(policy.get("source_lineage_job_cap", 14))
    max_exploit_share = max(0.0, min(1.0, float(policy.get("max_exploit_share", 0.42))))
    exploit_cap = max(1, int(round(max_jobs_per_cycle * max_exploit_share)))
    strict_target = max(1, int(round(max_jobs_per_cycle * float(policy.get("strict_fill_ratio", 0.82)))))
    strict_target = min(strict_target, max_jobs_per_cycle, len(candidate_pool) or len(jobs))

    selected: list[dict] = []
    selected_names: set[str] = set()
    cluster_counts: dict[str, int] = {}
    signature_counts: dict[str, int] = {}
    raw_family_counts: dict[str, int] = {}
    mechanism_counts: dict[str, int] = {}
    expression_family_counts: dict[str, int] = {}
    lineage_counts: dict[str, int] = {}
    mode_counts: dict[str, int] = {}
    dataset_tier_counts: dict[str, int] = {}
    skipped: dict[str, int] = {}

    def bump(reason: str) -> None:
        skipped[reason] = skipped.get(reason, 0) + 1

    def can_accept(job: dict, relaxed: bool = False) -> tuple[bool, str]:
        name = str(job.get("name") or "")
        if name in selected_names:
            return False, "duplicate_name"
        mode = str(job.get("supply_mode") or "explore")
        if mode == "exploit" and mode_counts.get("exploit", 0) >= exploit_cap and not relaxed:
            return False, "exploit_share"
        signature = str(job.get("expression_signature") or "unknown")
        if signature_cap > 0 and signature_counts.get(signature, 0) >= signature_cap and not relaxed:
            return False, "expression_signature"
        raw_family = str(job.get("raw_alpha_family") or "")
        if raw_family and raw_family_cap > 0 and raw_family_counts.get(raw_family, 0) >= raw_family_cap and not relaxed:
            return False, "raw_family"
        mechanism_id = str(job.get("mechanism_id") or job.get("research_mechanism_id") or "")
        if mechanism_id and mechanism_cap > 0 and mechanism_counts.get(mechanism_id, 0) >= mechanism_cap and not relaxed:
            return False, "mechanism"
        expression_family = str(job.get("expression_family") or "")
        if expression_family and expression_family_cap > 0 and expression_family_counts.get(expression_family, 0) >= expression_family_cap and not relaxed:
            return False, "expression_family"
        lineage = str(job.get("source_family_key") or "")
        if lineage and lineage_cap > 0 and lineage_counts.get(lineage, 0) >= lineage_cap and not relaxed:
            return False, "source_lineage"
        primary_cluster = str(job.get("primary_field_cluster") or "misc_research")
        if field_cap > 0 and cluster_counts.get(primary_cluster, 0) >= field_cap and not relaxed:
            return False, "field_cluster"
        return True, ""

    def accept(job: dict, relaxed: bool = False) -> None:
        name = str(job.get("name") or "")
        selected.append(job)
        selected_names.add(name)
        mode = str(job.get("supply_mode") or "explore")
        mode_counts[mode] = mode_counts.get(mode, 0) + 1
        signature = str(job.get("expression_signature") or "unknown")
        signature_counts[signature] = signature_counts.get(signature, 0) + 1
        raw_family = str(job.get("raw_alpha_family") or "")
        if raw_family:
            raw_family_counts[raw_family] = raw_family_counts.get(raw_family, 0) + 1
        mechanism_id = str(job.get("mechanism_id") or job.get("research_mechanism_id") or "")
        if mechanism_id:
            mechanism_counts[mechanism_id] = mechanism_counts.get(mechanism_id, 0) + 1
        expression_family = str(job.get("expression_family") or "")
        if expression_family:
            expression_family_counts[expression_family] = expression_family_counts.get(expression_family, 0) + 1
        lineage = str(job.get("source_family_key") or "")
        if lineage:
            lineage_counts[lineage] = lineage_counts.get(lineage, 0) + 1
        primary_cluster = str(job.get("primary_field_cluster") or "misc_research")
        cluster_counts[primary_cluster] = cluster_counts.get(primary_cluster, 0) + 1
        for tier in job.get("dataset_novelty_tiers") or []:
            tier_key = str(tier)
            dataset_tier_counts[tier_key] = dataset_tier_counts.get(tier_key, 0) + 1
        if relaxed:
            job["novelty_relaxed"] = True
        cluster_over = sum(max(0, cluster_counts.get(cluster, 0) - field_cap) for cluster in job.get("field_clusters", []))
        signature_over = max(0, signature_counts.get(signature, 0) - signature_cap)
        novelty_penalty = (
            cluster_over * float(policy.get("penalty_per_extra_cluster", 0.08))
            + signature_over * float(policy.get("penalty_per_extra_signature", 0.12))
        )
        if novelty_penalty:
            job["novelty_penalty"] = round(novelty_penalty, 6)
            job["supply_priority"] = _safe_float(job.get("supply_priority")) - novelty_penalty

    ordered_candidates: list[dict] = []
    for job in [*jobs, *candidate_pool]:
        annotate_novelty_features(job)
        ordered_candidates.append(job)
    ordered_candidates.sort(key=lambda item: _safe_float(item.get("supply_priority")), reverse=True)

    for job in ordered_candidates:
        if len(selected) >= max_jobs_per_cycle:
            break
        ok, reason = can_accept(job, relaxed=False)
        if ok:
            accept(job, relaxed=False)
        else:
            bump(reason)

    if len(selected) < max_jobs_per_cycle:
        for job in ordered_candidates:
            if len(selected) >= max_jobs_per_cycle:
                break
            ok, reason = can_accept(job, relaxed=True)
            if ok:
                accept(job, relaxed=True)
            else:
                bump(f"relaxed_{reason}")

    def backfill_dataset_tier(tier: str, min_share: float) -> None:
        if min_share <= 0 or max_jobs_per_cycle <= 0:
            return
        min_count = max(0, int(round(max_jobs_per_cycle * min_share)))
        while dataset_tier_counts.get(tier, 0) < min_count and len(selected) >= max_jobs_per_cycle:
            replacement = next(
                (
                    job
                    for job in ordered_candidates
                    if str(job.get("name") or "") not in selected_names
                    and tier in [str(item) for item in (job.get("dataset_novelty_tiers") or [])]
                ),
                None,
            )
            if replacement is None:
                break
            drop_index = next(
                (
                    index
                    for index, job in enumerate(reversed(selected))
                    if tier not in [str(item) for item in (job.get("dataset_novelty_tiers") or [])]
                ),
                None,
            )
            if drop_index is None:
                break
            actual_index = len(selected) - 1 - drop_index
            dropped = selected.pop(actual_index)
            selected_names.discard(str(dropped.get("name") or ""))
            for dropped_tier in dropped.get("dataset_novelty_tiers") or []:
                dropped_key = str(dropped_tier)
                dataset_tier_counts[dropped_key] = max(0, dataset_tier_counts.get(dropped_key, 0) - 1)
            accept(replacement, relaxed=True)

    selected.sort(key=lambda item: _safe_float(item.get("supply_priority")), reverse=True)
    backfill_dataset_tier("cold_high_value", float(policy.get("cold_dataset_min_share", 0.0)))
    backfill_dataset_tier("underused", float(policy.get("underused_dataset_min_share", 0.0)))
    selected.sort(key=lambda item: _safe_float(item.get("supply_priority")), reverse=True)
    summary = {
        "enabled": True,
        "strict_target": strict_target,
        "selected_count": len(selected),
        "skipped": skipped,
        "caps": {
            "field_cluster_job_cap": field_cap,
            "expression_signature_job_cap": signature_cap,
            "raw_family_job_cap": raw_family_cap,
            "mechanism_job_cap": mechanism_cap,
            "expression_family_job_cap": expression_family_cap,
            "source_lineage_job_cap": lineage_cap,
            "max_exploit_share": max_exploit_share,
            "exploit_cap": exploit_cap,
        },
        "field_clusters": novelty_bucket_counts(selected, "field_clusters"),
        "expression_signature_count": len(novelty_bucket_counts(selected, "expression_signature")),
        "raw_families": novelty_bucket_counts(selected, "raw_alpha_family"),
        "mechanisms": novelty_bucket_counts(selected, "mechanism_id"),
        "expression_families": novelty_bucket_counts(selected, "expression_family"),
        "dataset_novelty_tiers": novelty_bucket_counts(selected, "dataset_novelty_tiers"),
        "modes": novelty_bucket_counts(selected, "supply_mode"),
        "relaxed_count": len([job for job in selected if job.get("novelty_relaxed")]),
    }
    return selected[:max_jobs_per_cycle], summary


def lineage_child_record_for_job(job: dict) -> dict:
    return {
        "generation_id": int(job.get("generation_id") or 0),
        "job_name": str(job.get("name") or ""),
        "template": str(job.get("template") or ""),
        "variant_kind": variant_family_kind(job),
        "expression_hash": expression_hash_for_job(job),
        "parent_alpha_id": str(job.get("source_alpha_id") or ""),
        "parent_expression_hash": str(job.get("source_expression_hash") or ""),
        "peer_alpha_id": str(job.get("peer_alpha_id") or ""),
        "peer_expression_hash": str(job.get("peer_expression_hash") or ""),
        "priority": round(_safe_float(job.get("supply_priority")), 6),
    }


def update_lineage_warehouse(
    warehouse: dict,
    jobs: list[dict],
    population_archive: dict,
    generation_id: int,
) -> dict:
    lineages = warehouse.setdefault("lineages", {})
    lineage_metrics = population_archive.get("lineage_summary") or {}
    for job in jobs:
        family_key = str(job.get("source_family_key") or "").strip()
        if not family_key:
            continue
        metrics = lineage_metrics.get(family_key) or {}
        entry = lineages.setdefault(
            family_key,
            {
                "family_key": family_key,
                "created_generation": generation_id,
                "last_generation": generation_id,
                "total_jobs_generated": 0,
                "variant_counts": {},
                "expression_hashes": [],
                "parent_alpha_ids": [],
                "offspring": [],
                "recent_job_names": [],
            },
        )
        entry["last_generation"] = generation_id
        entry["category"] = str(metrics.get("category") or job.get("category") or "")
        entry["health_score"] = _safe_float(metrics.get("health_score"), 0.5)
        entry["avg_reward"] = _safe_float(metrics.get("avg_reward"))
        entry["top_reward"] = _safe_float(metrics.get("top_reward"))
        entry["is_degrading"] = bool(metrics.get("is_degrading"))
        entry["best_alpha_id"] = str(metrics.get("best_alpha_id") or "")
        entry["total_jobs_generated"] = int(entry.get("total_jobs_generated") or 0) + 1

        parent_alpha_id = str(job.get("source_alpha_id") or "")
        parent_alpha_ids = list(entry.get("parent_alpha_ids") or [])
        if parent_alpha_id and parent_alpha_id not in parent_alpha_ids:
            parent_alpha_ids.append(parent_alpha_id)
        if len(parent_alpha_ids) > 40:
            parent_alpha_ids = parent_alpha_ids[-40:]
        entry["parent_alpha_ids"] = parent_alpha_ids

        kind = variant_family_kind(job)
        variant_counts = entry.setdefault("variant_counts", {})
        variant_counts[kind] = int(variant_counts.get(kind) or 0) + 1

        expr_hash = expression_hash_for_job(job)
        expression_hashes = list(entry.get("expression_hashes") or [])
        if expr_hash and expr_hash not in expression_hashes:
            expression_hashes.append(expr_hash)
        if len(expression_hashes) > 80:
            expression_hashes = expression_hashes[-80:]
        entry["expression_hashes"] = expression_hashes

        offspring = list(entry.get("offspring") or [])
        child_record = lineage_child_record_for_job(job)
        child_key = (
            child_record["generation_id"],
            child_record["job_name"],
            child_record["expression_hash"],
        )
        existing_keys = {
            (
                int(child.get("generation_id") or 0),
                str(child.get("job_name") or ""),
                str(child.get("expression_hash") or ""),
            )
            for child in offspring
        }
        if child_key not in existing_keys:
            offspring.append(child_record)
        if len(offspring) > 120:
            offspring = offspring[-120:]
        entry["offspring"] = offspring

        recent_job_names = list(entry.get("recent_job_names") or [])
        job_name = str(job.get("name") or "")
        if job_name:
            recent_job_names.append(job_name)
        if len(recent_job_names) > 30:
            recent_job_names = recent_job_names[-30:]
        entry["recent_job_names"] = recent_job_names

    warehouse["lineage_count"] = len(lineages)
    warehouse["top_lineages"] = sorted(
        [
            {
                "family_key": key,
                "health_score": _safe_float(value.get("health_score"), 0.5),
                "top_reward": _safe_float(value.get("top_reward")),
                "total_jobs_generated": int(value.get("total_jobs_generated") or 0),
                "is_degrading": bool(value.get("is_degrading")),
                "variant_counts": value.get("variant_counts") or {},
                "parent_count": len(value.get("parent_alpha_ids") or []),
                "offspring_count": len(value.get("offspring") or []),
            }
            for key, value in lineages.items()
        ],
        key=lambda item: (
            -_safe_float(item.get("health_score"), 0.5),
            -_safe_float(item.get("top_reward")),
            int(item.get("total_jobs_generated") or 0),
        ),
    )[:30]
    return warehouse


def lineage_warehouse_penalty(job: dict, warehouse: dict, policy: dict) -> float:
    family_key = str(job.get("source_family_key") or "").strip()
    if not family_key:
        return 0.0
    lineages = warehouse.get("lineages") or {}
    entry = lineages.get(family_key) or {}
    if not entry:
        return 0.0
    penalty = 0.0
    max_generated = int(policy.get("lineage_soft_job_cap", 80))
    total_generated = int(entry.get("total_jobs_generated") or 0)
    if total_generated > max_generated:
        penalty += min(1.2, (total_generated - max_generated) / max(max_generated, 1) * 0.6)
    if bool(entry.get("is_degrading")):
        penalty += 0.25
    expr_hash = expression_hash_for_job(job)
    if expr_hash and expr_hash in set(entry.get("expression_hashes") or []):
        penalty += 0.75
    return penalty


def enforce_family_job_caps(
    jobs: list[dict],
    family_cap: int,
    elite_bonus_cap: int,
    elite_families: set[str],
) -> list[dict]:
    if family_cap <= 0:
        return list(jobs)
    counts: dict[str, int] = {}
    filtered: list[dict] = []
    for job in jobs:
        family_key = str(job.get("source_family_key") or "").strip()
        if not family_key:
            filtered.append(job)
            continue
        limit = family_cap + (elite_bonus_cap if family_key in elite_families else 0)
        counts.setdefault(family_key, 0)
        if counts[family_key] >= limit:
            continue
        counts[family_key] += 1
        filtered.append(job)
    return filtered


def backfill_generation_quota(
    selected: list[dict],
    candidate_pool: list[dict],
    predicate,
    min_count: int,
) -> list[dict]:
    if min_count <= 0:
        return selected
    current = [job for job in selected if predicate(job)]
    if len(current) >= min_count:
        return selected

    selected_keys = {str(job.get("name") or "") for job in selected}
    replacements = [
        job
        for job in candidate_pool
        if predicate(job) and str(job.get("name") or "") not in selected_keys
    ]
    replacements.sort(key=lambda item: _safe_float(item.get("supply_priority")), reverse=True)
    if not replacements:
        return selected

    kept = sorted(selected, key=lambda item: _safe_float(item.get("supply_priority")))
    protected_names = {str(job.get("name") or "") for job in current}
    while len(current) < min_count and replacements:
        replacement = replacements.pop(0)
        drop_index = next(
            (
                index
                for index, job in enumerate(kept)
                if str(job.get("name") or "") not in protected_names and not predicate(job)
            ),
            None,
        )
        if drop_index is None:
            break
        kept.pop(drop_index)
        kept.append(replacement)
        current.append(replacement)
        protected_names.add(str(replacement.get("name") or ""))

    kept.sort(key=lambda item: _safe_float(item.get("supply_priority")), reverse=True)
    return kept


def frontload_generation_quota(
    selected: list[dict],
    candidate_pool: list[dict],
    predicate,
    target_count: int,
    max_jobs: int,
) -> list[dict]:
    if target_count <= 0 or max_jobs <= 0:
        return selected[:max_jobs]
    protected: list[dict] = []
    seen: set[str] = set()
    for job in sorted(candidate_pool, key=lambda item: _safe_float(item.get("supply_priority")), reverse=True):
        name = str(job.get("name") or "")
        if not name or name in seen or not predicate(job):
            continue
        protected.append(job)
        seen.add(name)
        if len(protected) >= target_count:
            break
    for job in selected:
        name = str(job.get("name") or "")
        if name and name not in seen:
            protected.append(job)
            seen.add(name)
        if len(protected) >= max_jobs:
            break
    protected.sort(
        key=lambda item: (
            0 if predicate(item) else 1,
            -_safe_float(item.get("supply_priority")),
        )
    )
    return protected[:max_jobs]


def recent_performance_snapshot(limit: int = 120) -> dict:
    cached = _RECENT_PERFORMANCE_SNAPSHOT_CACHE.get(limit)
    if cached is not None:
        return cached
    payloads = result_payloads()
    if not payloads:
        return {
            "count": 0,
            "avg_reward": 0.0,
            "success_rate": 0.0,
            "mode": "breadth_first",
            "explore_share": 0.7,
            "exploit_share": 0.3,
        }

    sample = payloads[-limit:]
    rewards = [reward_score(payload) for payload in sample]
    success_count = len([score for score in rewards if score >= 1.0])
    avg_reward = sum(rewards) / max(len(rewards), 1)
    success_rate = success_count / max(len(rewards), 1)

    if avg_reward >= 1.8 or success_rate >= 0.24:
        mode = "depth_first"
        explore_share = 0.4
        exploit_share = 0.6
    elif avg_reward >= 1.2 or success_rate >= 0.16:
        mode = "hybrid"
        explore_share = 0.5
        exploit_share = 0.5
    else:
        mode = "breadth_first"
        explore_share = 0.7
        exploit_share = 0.3
    snapshot = {
        "count": len(sample),
        "avg_reward": avg_reward,
        "success_rate": success_rate,
        "mode": mode,
        "explore_share": explore_share,
        "exploit_share": exploit_share,
    }
    _RECENT_PERFORMANCE_SNAPSHOT_CACHE[limit] = snapshot
    return snapshot


def recent_category_scores(limit: int = 180) -> dict[str, float]:
    cached = _RECENT_CATEGORY_SCORES_CACHE.get(limit)
    if cached is not None:
        return cached
    payloads = result_payloads()
    if not payloads:
        return {}
    scores_by_category: dict[str, list[float]] = {}
    for payload in payloads[-limit:]:
        category = str(payload.get("category") or "").lower().strip()
        if not category:
            continue
        scores_by_category.setdefault(category, []).append(reward_score(payload))
    output: dict[str, float] = {}
    for category, scores in scores_by_category.items():
        if not scores:
            continue
        scores.sort(reverse=True)
        top_slice = scores[: min(len(scores), 20)]
        output[category] = sum(top_slice) / len(top_slice)
    _RECENT_CATEGORY_SCORES_CACHE[limit] = output
    return output


def historical_result_hashes(limit: int = 800) -> set[str]:
    cached = _HISTORICAL_RESULT_HASHES_CACHE.get(limit)
    if cached is not None:
        return cached
    hashes: set[str] = set()
    payloads = result_payloads()
    for payload in payloads[-limit:]:
        expression = str(payload.get("expression") or "").strip()
        if expression:
            hashes.add(template_hash(expression))
    _HISTORICAL_RESULT_HASHES_CACHE[limit] = hashes
    return hashes


def tournament_select_candidates(
    candidates: list[tuple[float, dict, dict]],
    limit: int,
    tournament_size: int = 3,
    elite_ratio: float = 0.35,
) -> list[tuple[dict, dict]]:
    if limit <= 0 or not candidates:
        return []
    ordered = sorted(candidates, key=lambda item: item[0], reverse=True)
    elite_count = min(len(ordered), max(1, int(round(limit * elite_ratio))))
    selected = ordered[:elite_count]
    remaining = ordered[elite_count:]
    while len(selected) < min(limit, len(ordered)) and remaining:
        tournament = random.sample(remaining, min(tournament_size, len(remaining)))
        winner = max(tournament, key=lambda item: item[0])
        selected.append(winner)
        remaining.remove(winner)
    return [(payload, job) for _, payload, job in selected[:limit]]


def interleave_jobs(explore_jobs: list[dict], exploit_jobs: list[dict], max_jobs_per_cycle: int) -> list[dict]:
    ordered: list[dict] = []
    while (explore_jobs or exploit_jobs) and len(ordered) < max_jobs_per_cycle:
        if explore_jobs:
            ordered.append(explore_jobs.pop(0))
            if len(ordered) >= max_jobs_per_cycle:
                break
        if exploit_jobs:
            ordered.append(exploit_jobs.pop(0))
    return ordered[:max_jobs_per_cycle]


def choose_jobs_by_search_strategy(
    explore_jobs: list[dict],
    exploit_jobs: list[dict],
    max_jobs_per_cycle: int,
    snapshot: dict,
) -> list[dict]:
    if max_jobs_per_cycle <= 0:
        return []
    mode = str(snapshot.get("mode") or "breadth_first")
    if mode == "depth_first":
        exploit_quota = min(len(exploit_jobs), max(1, int(round(max_jobs_per_cycle * float(snapshot.get("exploit_share") or 0.6)))))
        explore_quota = min(len(explore_jobs), max_jobs_per_cycle - exploit_quota)
        selected = exploit_jobs[:exploit_quota] + explore_jobs[:explore_quota]
        if len(selected) < max_jobs_per_cycle:
            remainder = exploit_jobs[exploit_quota:] + explore_jobs[explore_quota:]
            selected.extend(remainder[: max_jobs_per_cycle - len(selected)])
        return selected[:max_jobs_per_cycle]
    if mode == "hybrid":
        explore_quota = min(len(explore_jobs), max(1, int(round(max_jobs_per_cycle * float(snapshot.get("explore_share") or 0.5)))))
        exploit_quota = min(len(exploit_jobs), max_jobs_per_cycle - explore_quota)
        explore_copy = list(explore_jobs[:explore_quota])
        exploit_copy = list(exploit_jobs[:exploit_quota])
        selected = interleave_jobs(explore_copy, exploit_copy, max_jobs_per_cycle)
        if len(selected) < max_jobs_per_cycle:
            remainder = explore_jobs[explore_quota:] + exploit_jobs[exploit_quota:]
            selected.extend(remainder[: max_jobs_per_cycle - len(selected)])
        return selected[:max_jobs_per_cycle]
    explore_quota = min(len(explore_jobs), max(1, int(round(max_jobs_per_cycle * float(snapshot.get("explore_share") or 0.7)))))
    exploit_quota = min(len(exploit_jobs), max_jobs_per_cycle - explore_quota)
    selected = explore_jobs[:explore_quota] + exploit_jobs[:exploit_quota]
    if len(selected) < max_jobs_per_cycle:
        remainder = explore_jobs[explore_quota:] + exploit_jobs[exploit_quota:]
        selected.extend(remainder[: max_jobs_per_cycle - len(selected)])
    return selected[:max_jobs_per_cycle]


def filter_duplicate_jobs(
    jobs: list[dict],
    similarity_threshold: float = 0.985,
    historical_hashes: set[str] | None = None,
) -> list[dict]:
    filtered: list[dict] = []
    seen_hashes: set[str] = set()
    seen_templates: list[tuple[str, dict]] = []
    historical_hashes = historical_hashes or set()
    for job in jobs:
        if str(job.get("supply_mode") or "explore") != "exploit":
            filtered.append(job)
            continue
        template_path = ROOT_DIR / str(job.get("template") or "")
        try:
            template = load_template(template_path)
        except Exception:
            filtered.append(job)
            continue
        expression = str(template.get("expression") or "").strip()
        expr_hash = template_hash(expression)
        if str(job.get("repair_engine") or "") == "high_grade_failed_repair":
            signature_hash = template_hash(
                json.dumps(
                    {"expression": expression, "settings": template.get("settings") or {}},
                    sort_keys=True,
                    ensure_ascii=False,
                )
            )
            if signature_hash in seen_hashes:
                continue
            filtered.append(job)
            seen_hashes.add(signature_hash)
            seen_templates.append((expression, job))
            continue
        if expr_hash in historical_hashes:
            continue
        if expr_hash in seen_hashes:
            continue
        duplicate = False
        for existing_expression, existing_job in seen_templates:
            similarity = template_similarity(expression, existing_expression)
            if similarity >= similarity_threshold:
                existing_priority = float(existing_job.get("supply_priority") or 0)
                current_priority = float(job.get("supply_priority") or 0)
                if current_priority > existing_priority:
                    filtered = [row for row in filtered if row is not existing_job]
                    seen_templates = [(expr, row) for expr, row in seen_templates if row is not existing_job]
                    duplicate = False
                    break
                duplicate = True
                break
        if duplicate:
            continue
        filtered.append(job)
        seen_hashes.add(expr_hash)
        seen_templates.append((expression, job))
    return filtered


def load_exploit_sources(
    base_jobs: list[dict],
    limit: int,
    min_score: float,
    family_metrics: dict[str, dict] | None = None,
) -> list[tuple[dict, dict]]:
    cache_key = (limit, round(min_score, 6))
    cached = _EXPLOIT_SOURCES_CACHE.get(cache_key)
    if cached is not None:
        return cached
    candidates: list[tuple[float, dict, dict]] = []
    family_metrics = family_metrics or {}
    for payload in result_payloads():
        expression = str(payload.get("expression") or "").strip()
        if not expression:
            continue
        if "{{" in expression or "}}" in expression:
            continue
        failed = set(failed_check_names(payload))
        if "CONCENTRATED_WEIGHT" in failed and not is_high_quality_repair_payload(payload):
            continue
        if is_structural_concentration_breed_risk(payload) and not is_high_quality_repair_payload(payload):
            continue
        score = result_score(payload)
        quality_penalty, quality_reasons = exploit_quality_penalty(payload)
        adjusted_base_score = score - quality_penalty
        if score < min_score:
            continue
        job = infer_source_job(payload, base_jobs)
        if not job:
            continue
        family_key = family_key_for_payload(payload)
        family = family_metrics.get(family_key) or {}
        health_score = _safe_float(family.get("health_score"), 0.5)
        degradation_penalty = 0.45 if family.get("is_degrading") else 0.0
        family_bonus = health_score * 0.55 + min(0.35, _safe_float(family.get("top_reward")) * 0.08)
        repair_continuation_bonus = 0.0
        if is_high_quality_repair_payload(payload):
            repair_continuation_bonus = 1.25
        adjusted_score = adjusted_base_score + family_bonus + repair_continuation_bonus - degradation_penalty
        payload = dict(payload)
        payload["family_key"] = family_key
        payload["family_health_score"] = health_score
        payload["family_is_degrading"] = bool(family.get("is_degrading"))
        payload["family_top_reward"] = _safe_float(family.get("top_reward"))
        payload["high_quality_repair_continuation"] = bool(repair_continuation_bonus)
        payload["exploit_quality_penalty"] = round(quality_penalty, 6)
        payload["exploit_quality_reasons"] = quality_reasons
        payload["effective_pass_count"] = effective_pass_count(payload)
        candidates.append((adjusted_score, payload, job))
    selected = tournament_select_candidates(candidates, limit=limit)
    _EXPLOIT_SOURCES_CACHE[cache_key] = selected
    return selected


def load_hopeful_sources(base_jobs: list[dict], limit: int) -> list[tuple[dict, dict]]:
    cached = _HOPEFUL_SOURCES_CACHE.get(limit)
    if cached is not None:
        return cached
    candidates: list[tuple[float, dict, dict]] = []
    for payload in result_payloads():
        expression = str(payload.get("expression") or "").strip()
        if not expression:
            continue
        if "{{" in expression or "}}" in expression:
            continue
        if not is_hopeful_payload(payload):
            continue
        job = infer_source_job(payload, base_jobs)
        if not job:
            continue
        candidates.append((abs(result_score(payload)), payload, job))
    candidates.sort(key=lambda item: item[0], reverse=True)
    selected = [(payload, job) for _, payload, job in candidates[:limit]]
    _HOPEFUL_SOURCES_CACHE[limit] = selected
    return selected


def load_high_quality_repair_sources(base_jobs: list[dict], limit: int) -> list[tuple[dict, dict]]:
    if limit <= 0:
        return []
    cache_key = ("high_quality_repair", limit)
    cached = _EXPLOIT_SOURCES_CACHE.get(cache_key)
    if cached is not None:
        return cached
    candidates: list[tuple[float, dict, dict]] = []
    for payload in result_payloads():
        expression = str(payload.get("expression") or "").strip()
        if not expression or "{{" in expression or "}}" in expression:
            continue
        if not is_high_quality_repair_payload(payload):
            continue
        job = infer_source_job(payload, base_jobs)
        if not job:
            continue
        candidates.append((result_score(payload) + 1.25 + effective_pass_count(payload) * 0.12, payload, job))
    candidates.sort(key=lambda item: item[0], reverse=True)
    selected = [(dict(payload, high_quality_repair_continuation=True), job) for _, payload, job in candidates[:limit]]
    _EXPLOIT_SOURCES_CACHE[cache_key] = selected
    return selected


def payload_dataset_hint(payload: dict) -> str:
    for key in ("dataset_id", "dataset", "data_family", "raw_alpha_family", "category"):
        value = str(payload.get(key) or "").strip().lower()
        if value:
            return value
    source = str(payload.get("source_file") or payload.get("batch_name") or "").lower()
    for token in re.split(r"[^a-z0-9]+", source):
        if token in {"fundamental", "analyst", "model", "sentiment", "news", "option", "risk"}:
            return token
    return ""


def payload_operator_signature(payload: dict) -> str:
    expression = str(payload.get("expression") or "").lower()
    operators = re.findall(r"\b([a-z_][a-z0-9_]*)\s*\(", expression)
    return "_".join(operators[:4]) or extract_main_operator(expression)


def hybrid_peer_distance(left: dict, right: dict) -> float:
    if left is right:
        return -99.0
    left_expr = str(left.get("expression") or "")
    right_expr = str(right.get("expression") or "")
    if not left_expr or not right_expr:
        return -99.0
    distance = 0.0
    if family_key_for_payload(left) != family_key_for_payload(right):
        distance += 1.0
    if payload_dataset_hint(left) != payload_dataset_hint(right):
        distance += 0.7
    if payload_operator_signature(left) != payload_operator_signature(right):
        distance += 0.6
    distance += min(0.8, 1.0 - template_similarity(left_expr, right_expr))
    distance += max(-0.4, min(0.4, result_score(right) * 0.08))
    return distance


def load_hybrid_breaker_peers(limit: int, exclude_family: str = "") -> list[dict]:
    if limit <= 0:
        return []
    cache_key = (limit, exclude_family)
    cached = _HYBRID_BREAKER_PEERS_CACHE.get(cache_key)
    if cached is not None:
        return cached
    candidates: list[tuple[float, dict]] = []
    for payload in result_payloads():
        expression = str(payload.get("expression") or "").strip()
        if not expression or "{{" in expression or "}}" in expression:
            continue
        if exclude_family and family_key_for_payload(payload) == exclude_family:
            continue
        score = result_score(payload) + effective_pass_count(payload) * 0.08
        if is_high_grade_payload(payload):
            score += 0.35
        candidates.append((score, payload))
    candidates.sort(key=lambda item: item[0], reverse=True)
    selected = [payload for _, payload in candidates[: max(limit * 5, limit)]]
    _HYBRID_BREAKER_PEERS_CACHE[cache_key] = selected
    return selected


def select_hybrid_peers(payload: dict, pool: list[dict], limit: int) -> list[dict]:
    ranked: list[tuple[float, dict]] = []
    source_family = family_key_for_payload(payload)
    source_expression = str(payload.get("expression") or "")
    for peer in pool:
        if str(peer.get("alpha_id") or "") == str(payload.get("alpha_id") or ""):
            continue
        if family_key_for_payload(peer) == source_family:
            continue
        peer_expression = str(peer.get("expression") or "")
        if not peer_expression:
            continue
        if template_similarity(source_expression, peer_expression) >= 0.72:
            continue
        distance = hybrid_peer_distance(payload, peer)
        if distance <= 0:
            continue
        ranked.append((distance, peer))
    ranked.sort(key=lambda item: item[0], reverse=True)
    return [peer for _, peer in ranked[:limit]]


def build_exploit_base_template(payload: dict) -> dict:
    details = payload.get("alpha_details") or {}
    expression = unwrap_field_expression(str(payload.get("expression") or ""))
    settings = payload.get("settings") or {}
    return {
        "name": f"exploit_{payload.get('alpha_id') or slugify(payload.get('name') or 'alpha')}_{{{{FIELD_SLUG}}}}",
        "type": "REGULAR",
        "expression": expression,
        "settings": settings,
        "category": payload.get("category"),
        "tags": ["supply_auto", "supply_exploit", str(payload.get("alpha_id") or "")],
        "description": f"Auto exploit variant from alpha {payload.get('alpha_id')} grade={details.get('grade')}",
        "field_selection": {
            "allowed_types": ["MATRIX"],
            "blocked_types": ["EVENT", "GROUP"],
            "blocked_terms": ["item", "person", "currency"],
            "prefer_low_usage": True,
        },
        "min_coverage": 0.58,
    }


def build_exploit_variants(payload: dict, max_variants: int) -> list[dict]:
    base = build_exploit_base_template(payload)
    variants = build_template_variants(base, max_variants=max_variants)
    if len(variants) < max_variants:
        negated = copy.deepcopy(base)
        negated["name"] = f"{slugify(str(base.get('name')))}_neg_{{{{FIELD_SLUG}}}}"
        negated["expression"] = f"reverse({base['expression']})"
        tags = list(negated.get("tags") or [])
        if "supply_negation" not in tags:
            tags.append("supply_negation")
        negated["tags"] = tags
        validation = validate_template_payload(Path("<exploit_negation>"), negated)
        if validation.valid:
            variants.append(negated)
    return variants[:max_variants]


def build_lateral_variants(payload: dict, max_variants: int) -> list[dict]:
    base = build_exploit_base_template(payload)
    base_expr = str(base.get("expression") or "").strip()
    variants: list[dict] = []
    seen_hashes: set[str] = set()

    candidate_expressions = [base_expr]
    window_swaps = [("252", "126"), ("126", "60"), ("60", "30"), ("60", "90"), ("20", "10"), ("20", "40")]
    for old, new in window_swaps:
        if old in base_expr:
            candidate_expressions.append(base_expr.replace(old, new))
    operator_swaps = [
        ("ts_rank(", "zscore("),
        ("zscore(", "rank("),
        ("ts_mean(", "ts_rank("),
        ("group_rank(", "group_zscore("),
    ]
    for old, new in operator_swaps:
        if old in base_expr:
            candidate_expressions.append(base_expr.replace(old, new, 1))
    if "winsorize(" not in base_expr:
        candidate_expressions.append(f"winsorize({base_expr}, std=4)")
    if "ts_backfill(" not in base_expr:
        candidate_expressions.append(f"ts_backfill({base_expr}, 60)")
    if "reverse(" not in base_expr:
        candidate_expressions.append(f"reverse({base_expr})")
    if "trade_when(" not in base_expr and not has_concentrated_weight_fail(payload):
        candidate_expressions.append(f"trade_when(ts_rank(volume, 20) > 0.55, {base_expr}, -1)")
        candidate_expressions.append(f"trade_when(ts_arg_max(volume, 5) == 0, {base_expr}, -1)")

    for index, expr in enumerate(candidate_expressions, start=1):
        dedupe_hash = template_hash(expr)
        if dedupe_hash in seen_hashes:
            continue
        seen_hashes.add(dedupe_hash)
        candidate = copy.deepcopy(base)
        candidate["expression"] = expr
        candidate["name"] = f"lateral_{payload.get('alpha_id') or slugify(payload.get('name') or 'alpha')}_{index}_{{{{FIELD_SLUG}}}}"
        tags = list(candidate.get("tags") or [])
        for tag in ["supply_auto", "supply_lateral"]:
            if tag not in tags:
                tags.append(tag)
        candidate["tags"] = tags
        validation = validate_template_payload(Path("<lateral_variant>"), candidate)
        if not validation.valid:
            continue
        variants.append(candidate)
        if len(variants) >= max_variants:
            break
    return variants[:max_variants]


def self_improvement_expressions(expression: str, failed: set[str], max_variants: int) -> list[tuple[str, str]]:
    variants: list[tuple[str, str]] = []
    lower = expression.lower()

    def add(label: str, expr: str) -> None:
        if len(variants) >= max_variants:
            return
        expr = str(expr or "").strip()
        if not expr or expr == expression:
            return
        if any(existing == expr for _, existing in variants):
            return
        variants.append((label, expr))

    if not failed:
        failed = {"GENERAL_SELF_IMPROVEMENT"}

    if "CONCENTRATED_WEIGHT" in failed:
        if "winsorize(" not in lower:
            add("self_concentration_winsorize3", f"winsorize({expression}, std=3)")
            add("self_concentration_winsorize2", f"winsorize({expression}, std=2)")
        if "rank(" not in lower:
            add("self_concentration_rank", f"rank({expression})")
        if "group_rank(" not in lower:
            add("self_concentration_group_rank_subindustry", f"group_rank({expression}, subindustry)")
            add("self_concentration_group_rank_industry", f"group_rank({expression}, industry)")

    if "LOW_SUB_UNIVERSE_SHARPE" in failed:
        if "group_rank(" not in lower:
            add("self_subuni_group_rank_subindustry", f"group_rank({expression}, subindustry)")
            add("self_subuni_group_rank_industry", f"group_rank({expression}, industry)")
        if "group_zscore(" not in lower:
            add("self_subuni_group_zscore_subindustry", f"group_zscore({expression}, subindustry)")
            add("self_subuni_group_zscore_industry", f"group_zscore({expression}, industry)")
        if "rank(" not in lower:
            add("self_subuni_rank", f"rank({expression})")

    if "HIGH_TURNOVER" in failed:
        if "ts_mean(" not in lower:
            add("self_turnover_ts_mean5", f"ts_mean({expression}, 5)")
            add("self_turnover_ts_mean10", f"ts_mean({expression}, 10)")
        if "hump(" not in lower:
            add("self_turnover_hump", f"hump({expression}, 0.01)")
        if "trade_when(" not in lower:
            add("self_turnover_liquid_gate", f"trade_when(ts_rank(volume, 20) > 0.35, {expression}, -1)")

    if "LOW_TURNOVER" in failed:
        if "ts_delta(" not in lower:
            add("self_lowturn_delta5", f"ts_delta({expression}, 5)")
        if "ts_rank(" not in lower:
            add("self_lowturn_ts_rank10", f"ts_rank({expression}, 10)")

    if "LOW_FITNESS" in failed or "LOW_SHARPE" in failed:
        if "rank(" not in lower:
            add("self_signal_rank", f"rank({expression})")
        if "zscore(" not in lower:
            add("self_signal_zscore", f"zscore({expression})")
        if "winsorize(" not in lower:
            add("self_signal_winsorize4", f"winsorize({expression}, std=4)")
        if "ts_rank(" not in lower:
            add("self_signal_ts_rank20", f"ts_rank({expression}, 20)")

    if "GENERAL_SELF_IMPROVEMENT" in failed:
        window_swaps = [("252", "126"), ("126", "60"), ("60", "30"), ("20", "10"), ("20", "40"), ("5", "10")]
        for old, new in window_swaps:
            if old in expression:
                add(f"self_window_{old}_to_{new}", expression.replace(old, new, 1))
        operator_swaps = [
            ("ts_rank(", "zscore("),
            ("zscore(", "rank("),
            ("group_rank(", "group_zscore("),
        ]
        for old, new in operator_swaps:
            if old in expression:
                add(f"self_operator_{old.strip('(')}_to_{new.strip('(')}", expression.replace(old, new, 1))
        if "winsorize(" not in lower:
            add("self_general_winsorize4", f"winsorize({expression}, std=4)")

    return variants[:max_variants]


def build_self_improvement_variants(payload: dict, max_variants: int) -> list[dict]:
    base = build_exploit_base_template(payload)
    base_expr = str(base.get("expression") or "").strip()
    if not base_expr or max_variants <= 0:
        return []
    variants: list[dict] = []
    seen_hashes: set[str] = set()
    failed = set(failed_check_names(payload))
    source_id = str(payload.get("alpha_id") or slugify(payload.get("name") or "alpha"))
    for label, expr in self_improvement_expressions(base_expr, failed, max_variants=max_variants * 2):
        if len(variants) >= max_variants:
            break
        dedupe_hash = template_hash(expr)
        if dedupe_hash in seen_hashes:
            continue
        seen_hashes.add(dedupe_hash)
        candidate = copy.deepcopy(base)
        candidate["expression"] = expr
        candidate["name"] = f"self_{source_id}_{label}_{{{{FIELD_SLUG}}}}"
        candidate["self_improvement_label"] = label
        tags = list(candidate.get("tags") or [])
        for tag in ["supply_auto", "supply_self_improvement", label]:
            if tag not in tags:
                tags.append(tag)
        candidate["tags"] = tags
        validation = validate_template_payload(Path("<self_improvement_variant>"), candidate)
        if not validation.valid:
            continue
        variants.append(candidate)
    return variants[:max_variants]


def build_hybrid_breaker_variants(
    payload: dict,
    peer_payloads: list[dict],
    max_variants: int,
) -> list[dict]:
    base = build_exploit_base_template(payload)
    base_expr = str(base.get("expression") or "").strip()
    if not base_expr or max_variants <= 0:
        return []
    variants: list[dict] = []
    seen_hashes: set[str] = set()
    source_id = str(payload.get("alpha_id") or slugify(payload.get("name") or "alpha"))

    for peer_index, peer in enumerate(peer_payloads, start=1):
        if len(variants) >= max_variants:
            break
        peer_expr = unwrap_field_expression(str(peer.get("expression") or "").strip())
        if not peer_expr:
            continue
        guard_specs = [
            (
                "gate_top",
                f"trade_when(rank({peer_expr}) > 0.6, {base_expr}, -1)",
                "trade_when",
            ),
            (
                "gate_bottom",
                f"trade_when(rank({peer_expr}) < 0.4, {base_expr}, -1)",
                "trade_when",
            ),
            (
                "gate_extreme",
                f"trade_when(abs(zscore({peer_expr})) > 0.5, {base_expr}, -1)",
                "trade_when",
            ),
            (
                "gate_liquid_peer",
                f"trade_when(ts_rank({peer_expr}, 20) > 0.55, {base_expr}, -1)",
                "trade_when",
            ),
            (
                "gate_combo",
                f"trade_when((rank({peer_expr}) > 0.55) && (ts_rank(volume, 20) > 0.35), {base_expr}, -1)",
                "trade_when",
            ),
        ]
        for label, expr, hybrid_style in guard_specs:
            if len(variants) >= max_variants:
                break
            dedupe_hash = template_hash(expr)
            if dedupe_hash in seen_hashes:
                continue
            seen_hashes.add(dedupe_hash)
            candidate = copy.deepcopy(base)
            candidate["expression"] = expr
            candidate["name"] = f"hybrid_{source_id}_{peer_index}_{label}_{{{{FIELD_SLUG}}}}"
            candidate["peer_alpha_id"] = str(peer.get("alpha_id") or "")
            candidate["peer_expression_hash"] = template_hash(str(peer.get("expression") or ""))
            candidate["hybrid_peer_family_key"] = family_key_for_payload(peer)
            tags = list(candidate.get("tags") or [])
            for tag in ["supply_auto", "supply_hybrid_breaker", f"hybrid_{label}"]:
                if tag not in tags:
                    tags.append(tag)
            candidate["tags"] = tags
            validation = validate_template_payload(Path("<hybrid_breaker_variant>"), candidate)
            if not validation.valid:
                continue
            candidate["hybrid_style"] = hybrid_style
            variants.append(candidate)
    return variants[:max_variants]


def mutate_expression(expression: str) -> list[str]:
    variants: list[str] = []
    if not expression:
        return variants

    for operator in MUTATION_OPERATORS:
        if operator == "reverse" and "reverse(" not in expression:
            variants.append(f"reverse({expression})")
        elif operator == "winsorize" and "winsorize(" not in expression:
            variants.append(f"winsorize({expression}, std=4)")
        elif operator == "ts_backfill" and "ts_backfill(" not in expression:
            variants.append(f"ts_backfill({expression}, 60)")
        elif operator == "ts_mean" and "ts_mean(" not in expression:
            variants.append(f"ts_mean({expression}, 20)")
        elif operator == "ts_delta" and "ts_delta(" not in expression:
            variants.append(f"ts_delta({expression}, 5)")
        elif operator == "ts_rank" and "ts_rank(" not in expression:
            variants.append(f"ts_rank({expression}, 20)")
        elif operator == "zscore" and "zscore(" not in expression:
            variants.append(f"zscore({expression})")

    if "trade_when(" not in expression:
        variants.append(f"trade_when(ts_rank(volume, 20) > 0.55, {expression}, -1)")
        variants.append(f"trade_when(ts_arg_max(volume, 5) == 0, {expression}, -1)")

    parameter_swaps = [("252", "126"), ("126", "60"), ("60", "30"), ("20", "10"), ("5", "20")]
    for old, new in parameter_swaps:
        if old in expression:
            variants.append(expression.replace(old, new))

    operator_swaps = [
        ("ts_rank(", "zscore("),
        ("zscore(", "rank("),
        ("ts_mean(", "ts_rank("),
        ("group_rank(", "group_zscore("),
        ("reverse(", "rank("),
    ]
    for old, new in operator_swaps:
        if old in expression:
            variants.append(expression.replace(old, new, 1))

    deduped: list[str] = []
    seen: set[str] = set()
    for candidate in variants:
        normalized = template_hash(candidate)
        if normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(candidate)
    return deduped


def build_mutation_variants(payload: dict, max_variants: int) -> list[dict]:
    base = build_exploit_base_template(payload)
    base_expr = str(base.get("expression") or "").strip()
    variants: list[dict] = []
    seen_hashes: set[str] = set()
    for index, expr in enumerate(mutate_expression(base_expr), start=1):
        if len(variants) >= max_variants:
            break
        dedupe_hash = template_hash(expr)
        if dedupe_hash in seen_hashes:
            continue
        seen_hashes.add(dedupe_hash)
        candidate = copy.deepcopy(base)
        candidate["expression"] = expr
        candidate["name"] = f"mutant_{payload.get('alpha_id') or slugify(payload.get('name') or 'alpha')}_{index}_{{{{FIELD_SLUG}}}}"
        tags = list(candidate.get("tags") or [])
        for tag in ["supply_auto", "supply_mutation"]:
            if tag not in tags:
                tags.append(tag)
        candidate["tags"] = tags
        validation = validate_template_payload(Path("<mutation_variant>"), candidate)
        if not validation.valid:
            continue
        variants.append(candidate)
    return variants[:max_variants]


def crossover_expressions(left_expr: str, right_expr: str) -> list[str]:
    variants: list[str] = []
    if not left_expr or not right_expr:
        return variants
    left_parts = left_expr.split("(", 1)
    right_parts = right_expr.split("(", 1)
    if len(left_parts) > 1 and len(right_parts) > 1:
        variants.append(left_parts[0] + "(" + right_parts[1])
        variants.append(right_parts[0] + "(" + left_parts[1])
    if "{{FIELD}}" in left_expr and "{{FIELD}}" in right_expr:
        variants.append(f"({left_expr}) - ({right_expr})")
        variants.append(f"rank({left_expr}) - rank({right_expr})")
    deduped: list[str] = []
    seen: set[str] = set()
    for candidate in variants:
        normalized = template_hash(candidate)
        if normalized in seen:
            continue
        seen.add(normalized)
        deduped.append(candidate)
    return deduped


def build_crossover_variants(
    payload: dict,
    peer_payloads: list[dict],
    max_variants: int,
) -> list[dict]:
    base = build_exploit_base_template(payload)
    base_expr = str(base.get("expression") or "").strip()
    variants: list[dict] = []
    seen_hashes: set[str] = set()
    peer_index = 0
    for peer in peer_payloads:
        if len(variants) >= max_variants:
            break
        peer_expr = unwrap_field_expression(str(peer.get("expression") or "").strip())
        for expr in crossover_expressions(base_expr, peer_expr):
            if len(variants) >= max_variants:
                break
            dedupe_hash = template_hash(expr)
            if dedupe_hash in seen_hashes:
                continue
            seen_hashes.add(dedupe_hash)
            peer_index += 1
            candidate = copy.deepcopy(base)
            candidate["expression"] = expr
            candidate["name"] = f"cross_{payload.get('alpha_id') or slugify(payload.get('name') or 'alpha')}_{peer_index}_{{{{FIELD_SLUG}}}}"
            candidate["peer_alpha_id"] = str(peer.get("alpha_id") or "")
            candidate["peer_expression_hash"] = template_hash(str(peer.get("expression") or ""))
            tags = list(candidate.get("tags") or [])
            for tag in ["supply_auto", "supply_crossover"]:
                if tag not in tags:
                    tags.append(tag)
            candidate["tags"] = tags
            validation = validate_template_payload(Path("<crossover_variant>"), candidate)
            if not validation.valid:
                continue
            variants.append(candidate)
    return variants[:max_variants]


def build_hopeful_negation_variants(payload: dict, max_variants: int) -> list[dict]:
    base = build_exploit_base_template(payload)
    negated = copy.deepcopy(base)
    negated["name"] = f"{slugify(str(base.get('name')))}_hopeful_neg_{{{{FIELD_SLUG}}}}"
    negated["expression"] = f"reverse({base['expression']})"
    tags = list(negated.get("tags") or [])
    for tag in ["supply_auto", "supply_hopeful_negation"]:
        if tag not in tags:
            tags.append(tag)
    negated["tags"] = tags
    validation = validate_template_payload(Path("<hopeful_negation>"), negated)
    return [negated] if validation.valid else []


def build_template_variants(base_template: dict, max_variants: int) -> list[dict]:
    base_name = slugify(str(base_template.get("name") or "template"))
    base_expr = str(base_template.get("expression") or "").strip()
    variants: list[dict] = []
    seen_hashes: set[str] = set()
    wrappers = active_wrappers()

    for window_label, window_expr in window_variants(base_expr):
        for wrapper in wrappers:
            if len(variants) >= max_variants:
                return variants
            if not wrapper_is_compatible(window_expr, wrapper):
                continue
            expr = wrap_expr(window_expr, wrapper)
            expr_hash = template_hash(expr)
            if expr_hash in seen_hashes:
                continue
            candidate = copy.deepcopy(base_template)
            suffix = slugify(f"{window_label}_{wrapper}")
            candidate["name"] = f"{base_name}_{suffix}_{{{{FIELD_SLUG}}}}"
            candidate["expression"] = expr
            tags = list(candidate.get("tags") or [])
            for tag in ["supply_auto", f"supply_{suffix}"]:
                if tag not in tags:
                    tags.append(tag)
            candidate["tags"] = tags
            validation = validate_template_payload(Path("<supply_variant>"), candidate)
            if not validation.valid:
                continue
            seen_hashes.add(expr_hash)
            variants.append(candidate)
    return variants


def build_pair_template_variants(base_template: dict, max_variants: int) -> list[dict]:
    if max_variants <= 0:
        return []
    expression = str(base_template.get("expression") or "").strip()
    if "{{FIELD}}" not in expression:
        return []
    compatibility_profile = analyze_expression_compatibility(expression)
    field_inside_ts_operator = compatibility_profile.placeholder_inside_ts_operator

    replacement_specs = [
        (
            "pair_ratio",
            "({{FIELD_A}} / (abs({{FIELD_B}}) + 0.001))",
            "ratio style pair replacement",
        ),
        (
            "pair_diff",
            "({{FIELD_A}} - {{FIELD_B}})",
            "difference style pair replacement",
        ),
        (
            "pair_rankdiff",
            "(rank({{FIELD_A}}) - rank({{FIELD_B}}))",
            "rank spread pair replacement",
        ),
        (
            "pair_zdiff",
            "(zscore({{FIELD_A}}) - zscore({{FIELD_B}}))",
            "zscore spread pair replacement",
        ),
        (
            "pair_ranksum",
            "(rank({{FIELD_A}}) + rank({{FIELD_B}}))",
            "rank sum pair replacement",
        ),
        (
            "pair_meanratio",
            "(ts_mean({{FIELD_A}}, 20) / (abs(ts_mean({{FIELD_B}}, 20)) + 0.001))",
            "smoothed ratio pair replacement",
        ),
    ]
    variants: list[dict] = []
    seen_hashes: set[str] = set()
    base_name = slugify(str(base_template.get("name") or "template"))

    for suffix, replacement, _ in replacement_specs:
        if len(variants) >= max_variants:
            break
        if field_inside_ts_operator and suffix in {"pair_rankdiff", "pair_zdiff", "pair_ranksum"}:
            continue
        candidate = copy.deepcopy(base_template)
        candidate["expression"] = expression.replace("{{FIELD}}", replacement)
        candidate["name"] = (
            f"{base_name}_{suffix}_{{{{FIELD_A_SLUG}}}}_{{{{FIELD_B_SLUG}}}}"
        )
        tags = [
            tag
            for tag in list(candidate.get("tags") or [])
            if "{{FIELD" not in str(tag)
        ]
        for tag in ["supply_auto", "supply_pair", f"supply_{suffix}"]:
            if tag not in tags:
                tags.append(tag)
        candidate["tags"] = tags
        dedupe_hash = template_hash(candidate["expression"])
        if dedupe_hash in seen_hashes:
            continue
        validation = validate_template_payload(Path("<pair_variant>"), candidate)
        if not validation.valid:
            continue
        seen_hashes.add(dedupe_hash)
        variants.append(candidate)
    return variants[:max_variants]


def build_setting_variants(base_template: dict, source_job: dict) -> list[tuple[str, dict]]:
    variants: list[tuple[str, dict]] = []
    base_settings = copy.deepcopy(base_template.get("settings") or {})
    neutralizations = list(source_job.get("supply_neutralization_variants") or [])
    delays = list(source_job.get("supply_delay_variants") or [])
    decays = list(source_job.get("supply_decay_variants") or [])
    truncations = list(source_job.get("supply_truncation_variants") or [])
    universes = list(source_job.get("supply_universe_variants") or [])

    variants.append(("basecfg", copy.deepcopy(base_settings)))

    def variant_key(settings: dict) -> tuple:
        return (
            str(settings.get("neutralization") or ""),
            int(settings.get("delay") or 0),
            int(settings.get("decay") or 0),
            float(settings.get("truncation") or 0),
            str(settings.get("universe") or ""),
        )

    def add_variant(label: str, settings: dict, seen_keys: set[tuple]) -> None:
        key = variant_key(settings)
        if key in seen_keys:
            return
        seen_keys.add(key)
        variants.append((label, settings))

    seen_keys = {variant_key(base_settings)}

    for neutralization in neutralizations:
        candidate = copy.deepcopy(base_settings)
        candidate["neutralization"] = neutralization
        add_variant(f"neu_{slugify(str(neutralization))}", candidate, seen_keys)

    for delay in delays:
        candidate = copy.deepcopy(base_settings)
        candidate["delay"] = int(delay)
        add_variant(f"d{int(delay)}", candidate, seen_keys)

    for decay in decays:
        candidate = copy.deepcopy(base_settings)
        candidate["decay"] = int(decay)
        add_variant(f"decay_{int(decay)}", candidate, seen_keys)

    for truncation in truncations:
        candidate = copy.deepcopy(base_settings)
        candidate["truncation"] = float(truncation)
        add_variant(f"trunc_{str(truncation).replace('.', 'p')}", candidate, seen_keys)

    for universe in universes:
        candidate = copy.deepcopy(base_settings)
        candidate["universe"] = str(universe)
        add_variant(f"uni_{slugify(str(universe))}", candidate, seen_keys)

    if neutralizations and delays:
        for neutralization in neutralizations:
            for delay in delays:
                candidate = copy.deepcopy(base_settings)
                candidate["neutralization"] = neutralization
                candidate["delay"] = int(delay)
                add_variant(f"neu_{slugify(str(neutralization))}_d{int(delay)}", candidate, seen_keys)

    if neutralizations and decays:
        for neutralization in neutralizations:
            for decay in decays:
                candidate = copy.deepcopy(base_settings)
                candidate["neutralization"] = neutralization
                candidate["decay"] = int(decay)
                add_variant(f"neu_{slugify(str(neutralization))}_decay_{int(decay)}", candidate, seen_keys)

    if delays and decays:
        for delay in delays:
            for decay in decays:
                candidate = copy.deepcopy(base_settings)
                candidate["delay"] = int(delay)
                candidate["decay"] = int(decay)
                add_variant(f"d{int(delay)}_decay_{int(decay)}", candidate, seen_keys)

    return variants


def apply_setting_variants(base_variants: list[dict], source_job: dict, max_variants: int) -> list[dict]:
    expanded: list[dict] = []
    seen_hashes: set[str] = set()
    setting_variants = build_setting_variants(base_template=base_variants[0] if base_variants else {}, source_job=source_job)

    ordered_pairs: list[tuple[dict, str, dict]] = []
    if base_variants and setting_variants:
        for offset in range(len(setting_variants)):
            for index, template_variant in enumerate(base_variants):
                setting_label, settings = setting_variants[(index + offset) % len(setting_variants)]
                ordered_pairs.append((template_variant, setting_label, settings))

    for template_variant, setting_label, settings in ordered_pairs:
        if len(expanded) >= max_variants:
            return expanded
        candidate = copy.deepcopy(template_variant)
        candidate["settings"] = copy.deepcopy(settings)
        if setting_label != "basecfg":
            candidate["name"] = f"{str(candidate.get('name') or 'template')}_{setting_label}"
            tags = list(candidate.get("tags") or [])
            setting_tag = f"supply_{setting_label}"
            if setting_tag not in tags:
                tags.append(setting_tag)
            candidate["tags"] = tags
        dedupe_hash = json.dumps(
            {
                "expression": candidate.get("expression"),
                "settings": candidate.get("settings"),
            },
            sort_keys=True,
            ensure_ascii=False,
        )
        if dedupe_hash in seen_hashes:
            continue
        validation = validate_template_payload(Path("<setting_variant>"), candidate)
        if not validation.valid:
            continue
        seen_hashes.add(dedupe_hash)
        expanded.append(candidate)
    return expanded[:max_variants]


def hierarchical_bonus_for_template(
    template_payload: dict,
    bandit_summary: dict,
) -> tuple[float, dict]:
    category = slugify(str(template_payload.get("category") or "unknown"))
    tags = extract_strategy_tags(template_payload)
    operator = extract_main_operator(str(template_payload.get("expression") or ""))
    category_bandit = (bandit_summary.get("category_bandit") or {}).get(category) or {}
    strategy_bandits = bandit_summary.get("strategy_bandit") or {}
    operator_bandit = (bandit_summary.get("operator_bandit") or {}).get(operator) or {}

    strategy_scores = [
        _safe_float((strategy_bandits.get(tag) or {}).get("avg_reward"))
        for tag in tags
        if strategy_bandits.get(tag)
    ]
    strategy_success = [
        _safe_float((strategy_bandits.get(tag) or {}).get("success_rate"))
        for tag in tags
        if strategy_bandits.get(tag)
    ]
    category_bonus = _safe_float(category_bandit.get("avg_reward")) * 0.12 + _safe_float(category_bandit.get("success_rate")) * 0.25
    strategy_bonus = (sum(strategy_scores) / len(strategy_scores) if strategy_scores else 0.0) * 0.12
    strategy_bonus += (sum(strategy_success) / len(strategy_success) if strategy_success else 0.0) * 0.18
    operator_bonus = _safe_float(operator_bandit.get("avg_reward")) * 0.10 + _safe_float(operator_bandit.get("success_rate")) * 0.16
    total_bonus = category_bonus + strategy_bonus + operator_bonus
    return (
        total_bonus,
        {
            "category": category,
            "strategies": tags,
            "operator": operator,
            "category_avg_reward": _safe_float(category_bandit.get("avg_reward")),
            "operator_avg_reward": _safe_float(operator_bandit.get("avg_reward")),
        },
    )


def write_template(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(yaml.safe_dump(payload, sort_keys=False, allow_unicode=True), encoding="utf-8")


def prune_stale_supply_templates(active_relative_templates: list[str]) -> dict:
    active_paths = {
        (ROOT_DIR / relative_path).resolve()
        for relative_path in active_relative_templates
        if relative_path
    }
    removed_files = 0
    removed_dirs = 0
    if SUPPLY_TEMPLATE_DIR.exists():
        for path in sorted(SUPPLY_TEMPLATE_DIR.rglob("*.yaml"), reverse=True):
            resolved = path.resolve()
            if resolved in active_paths:
                continue
            path.unlink(missing_ok=True)
            removed_files += 1
        for directory in sorted(
            [path for path in SUPPLY_TEMPLATE_DIR.rglob("*") if path.is_dir()],
            reverse=True,
        ):
            try:
                directory.rmdir()
                removed_dirs += 1
            except OSError:
                continue
    return {
        "removed_files": removed_files,
        "removed_dirs": removed_dirs,
    }


def build_supply_jobs(config: dict) -> dict:
    supply_cfg = config.get("supply") or {}
    generation_state = load_generation_state()
    next_generation_id = int(generation_state.get("generation_id") or 0) + 1
    generation_policy = generation_policy_config(supply_cfg)
    novelty_policy = research_novelty_policy_config(supply_cfg)
    family_feedback = load_family_feedback_map()
    raw_family_actions = load_raw_family_actions_map()
    max_variants_per_job = int(supply_cfg.get("max_variants_per_job", 12))
    max_pair_variants_per_job = int(supply_cfg.get("max_pair_variants_per_job", 4))
    max_jobs_per_cycle = int(supply_cfg.get("max_jobs_per_cycle", 24))
    exploit_enabled = bool(supply_cfg.get("exploit_enabled", True))
    max_exploit_sources = int(supply_cfg.get("max_exploit_sources", 8))
    max_exploit_variants = int(supply_cfg.get("max_exploit_variants_per_source", 4))
    max_self_improvement_variants = int(supply_cfg.get("max_self_improvement_variants_per_source", 3))
    max_lateral_variants = int(supply_cfg.get("max_lateral_variants_per_source", 4))
    max_mutation_variants = int(supply_cfg.get("max_mutation_variants_per_source", 4))
    max_crossover_variants = int(supply_cfg.get("max_crossover_variants_per_source", 3))
    hybrid_breaker_enabled = bool(supply_cfg.get("hybrid_breaker_enabled", True))
    max_hybrid_sources = int(supply_cfg.get("max_hybrid_sources", 8))
    max_hybrid_variants = int(supply_cfg.get("max_hybrid_variants_per_source", 2))
    max_hybrid_peers = int(supply_cfg.get("max_hybrid_peers_per_source", 3))
    min_exploit_score = float(supply_cfg.get("min_exploit_score", 0.4))
    hopeful_enabled = bool(supply_cfg.get("hopeful_enabled", True))
    max_hopeful_sources = int(supply_cfg.get("max_hopeful_sources", 8))
    high_grade_repair_cfg = supply_cfg.get("high_grade_repair") or {}
    high_grade_repair_enabled = bool(high_grade_repair_cfg.get("enabled", True))
    high_grade_repair_templates: list[str] = []
    high_grade_repair_summary: dict[str, Any] = {}
    high_quality_repair_sources: list[tuple[dict, dict]] = []
    self_improvement_job_count = 0
    self_improvement_source_ids: set[str] = set()
    hybrid_breaker_job_count = 0
    hybrid_breaker_source_ids: set[str] = set()
    dedupe_similarity_threshold = float(supply_cfg.get("dedupe_similarity_threshold", 0.92))
    population_archive = build_population_archive(limit=int(supply_cfg.get("population_history_limit", 1200)))
    generation_state = update_family_lifecycle_state(
        generation_state=generation_state,
        population_archive=population_archive,
        generation_id=next_generation_id,
        policy=generation_policy,
    )
    hierarchical_bandit = build_hierarchical_bandit_summary(limit=int(supply_cfg.get("bandit_history_limit", 1200)))
    lineage_warehouse = load_lineage_warehouse()
    family_metrics = population_archive.get("family_metrics") or {}
    elite_families = {str(row.get("family_key") or "") for row in (population_archive.get("elite_families") or [])}
    retired_families = {str(item) for item in (generation_state.get("retired_families") or [])}
    explore_jobs: list[dict] = []
    exploit_jobs: list[dict] = []
    templates_written: list[str] = []
    static_base_jobs = [copy.deepcopy(job) for job in (config.get("jobs") or [])]
    raw_rotation_cfg = config.get("raw_alpha_rotation") or {}
    rotating_raw_jobs, rotating_templates, raw_alpha_rotation_summary = build_raw_alpha_rotation_jobs(config)
    templates_written.extend(rotating_templates)
    replace_static_jobs = bool(raw_rotation_cfg.get("replace_static_jobs", True))
    raw_rotation_active = bool(raw_rotation_cfg.get("enabled")) and bool(rotating_raw_jobs)
    if raw_rotation_active and replace_static_jobs:
        base_jobs = [*rotating_raw_jobs]
    else:
        base_jobs = [*static_base_jobs, *rotating_raw_jobs]
    if raw_rotation_active and bool(raw_rotation_cfg.get("disable_ordinary_exploit", True)):
        exploit_enabled = False
    category_scores = recent_category_scores()

    for base_job in base_jobs:
        template_path = ROOT_DIR / str(base_job["template"])
        base_template = load_template(template_path)
        expression_variants = build_template_variants(base_template, max_variants=max_variants_per_job * 2)
        pair_variants = build_pair_template_variants(base_template, max_variants=max_pair_variants_per_job)
        single_variants = apply_setting_variants(expression_variants, base_job, max_variants=max_variants_per_job)
        pair_variants = apply_setting_variants(pair_variants, base_job, max_variants=max_pair_variants_per_job)
        variants = single_variants + pair_variants
        base_name = str(base_job.get("name") or template_path.stem)
        job_priority = historical_job_score(base_name)
        settings_score = historical_settings_score(base_template.get("settings") or {})
        category_score = float(category_scores.get(str(base_job.get("category") or "").lower()) or 0.0)
        raw_alpha_bonus = 1.35 if base_job.get("raw_alpha_family") else 0.0
        for idx, variant in enumerate(variants, start=1):
            field_slot_count = len(extract_field_slots(variant))
            hierarchy_bonus, hierarchy_meta = hierarchical_bonus_for_template(variant, hierarchical_bandit)
            variant_name = slugify_template_name(str(variant.get("name") or f"{base_name}_{idx}"))
            relative_template = Path("result_store") / "supply" / "templates" / base_name / f"{variant_name}.yaml"
            template_output = ROOT_DIR / relative_template
            write_template(template_output, variant)
            templates_written.append(str(relative_template))

            job = copy.deepcopy(base_job)
            job["name"] = f"{base_name}_{idx:03d}"
            job["inventory_name"] = str(base_job.get("inventory_name") or base_name)
            job["template"] = str(relative_template).replace("\\", "/")
            job["refresh_inventory"] = True
            job["selection_limit"] = int(base_job.get("supply_selection_limit") or base_job.get("selection_limit") or 500)
            job["inventory_fetch_limit"] = int(base_job.get("supply_fetch_limit") or base_job.get("inventory_fetch_limit") or base_job.get("limit") or 1200)
            job["supply_mode"] = "explore"
            job["tags"] = list(variant.get("tags") or [])
            job["field_slot_count"] = field_slot_count
            job["supply_priority"] = (
                job_priority
                + raw_alpha_bonus
                + category_score * 0.22
                + settings_score * 0.35
                + hierarchy_bonus
                + max(0, field_slot_count - 1) * 0.45
            )
            job["bandit_category"] = hierarchy_meta.get("category")
            job["bandit_strategies"] = hierarchy_meta.get("strategies")
            job["bandit_operator"] = hierarchy_meta.get("operator")
            job["generation_id"] = next_generation_id
            apply_family_feedback_priority(job, novelty_policy, family_feedback)
            apply_raw_family_action(job, raw_family_actions)
            grounding_bonus = grounding_fit_bonus(job)
            if grounding_bonus:
                job["grounding_fit_bonus"] = grounding_bonus
                job["supply_priority"] = _safe_float(job.get("supply_priority")) + grounding_bonus
            explore_jobs.append(job)

    if exploit_enabled:
        exploit_sources = load_exploit_sources(
            base_jobs=base_jobs,
            limit=max_exploit_sources,
            min_score=min_exploit_score,
            family_metrics=family_metrics,
        )
        hybrid_sources = exploit_sources[:max_hybrid_sources] if hybrid_breaker_enabled else []
        exploit_counter = 0
        for payload, source_job in exploit_sources:
            expression_variants = build_exploit_variants(payload, max_variants=max_exploit_variants * 2)
            variants = apply_setting_variants(expression_variants, source_job, max_variants=max_exploit_variants)
            self_improvement_variants = build_self_improvement_variants(
                payload,
                max_variants=max_self_improvement_variants * 2,
            )
            self_improvement_variants = apply_setting_variants(
                self_improvement_variants,
                source_job,
                max_variants=max_self_improvement_variants,
            )
            lateral_variants = build_lateral_variants(payload, max_variants=max_lateral_variants * 2)
            lateral_variants = apply_setting_variants(lateral_variants, source_job, max_variants=max_lateral_variants)
            mutation_variants = build_mutation_variants(payload, max_variants=max_mutation_variants * 2)
            mutation_variants = apply_setting_variants(mutation_variants, source_job, max_variants=max_mutation_variants)
            peer_payloads = [item[0] for item in exploit_sources if item[0] is not payload][:4]
            crossover_variants = build_crossover_variants(payload, peer_payloads=peer_payloads, max_variants=max_crossover_variants * 2)
            crossover_variants = apply_setting_variants(crossover_variants, source_job, max_variants=max_crossover_variants)
            hybrid_variants: list[dict] = []
            if hybrid_breaker_enabled and any(source is payload for source, _ in hybrid_sources):
                hybrid_peer_pool = load_hybrid_breaker_peers(
                    limit=max(12, max_hybrid_peers * 4),
                    exclude_family=family_key_for_payload(payload),
                )
                selected_peers = select_hybrid_peers(payload, hybrid_peer_pool, limit=max_hybrid_peers)
                raw_hybrid_variants = build_hybrid_breaker_variants(
                    payload,
                    peer_payloads=selected_peers,
                    max_variants=max_hybrid_variants * 2,
                )
                hybrid_variants = apply_setting_variants(
                    raw_hybrid_variants,
                    source_job,
                    max_variants=max_hybrid_variants,
                )
            variants.extend(self_improvement_variants)
            variants.extend(lateral_variants)
            variants.extend(mutation_variants)
            variants.extend(crossover_variants)
            variants.extend(hybrid_variants)
            for variant in variants:
                exploit_counter += 1
                field_slot_count = len(extract_field_slots(variant))
                hierarchy_bonus, hierarchy_meta = hierarchical_bonus_for_template(variant, hierarchical_bandit)
                source_name = str(source_job.get("name") or "exploit")
                variant_name = slugify_template_name(str(variant.get("name") or f"exploit_{exploit_counter}"))
                relative_template = Path("result_store") / "supply" / "templates" / "exploit" / f"{variant_name}.yaml"
                template_output = ROOT_DIR / relative_template
                write_template(template_output, variant)
                templates_written.append(str(relative_template))

                job = copy.deepcopy(source_job)
                job["name"] = f"exploit_{exploit_counter:03d}_{source_name}"
                job["inventory_name"] = str(source_job.get("inventory_name") or source_name)
                job["template"] = str(relative_template).replace("\\", "/")
                align_job_field_sources(job, variant)
                job["refresh_inventory"] = True
                job["selection_limit"] = int(source_job.get("supply_selection_limit") or source_job.get("selection_limit") or 500)
                job["inventory_fetch_limit"] = int(source_job.get("supply_fetch_limit") or source_job.get("inventory_fetch_limit") or source_job.get("limit") or 1200)
                job["supply_mode"] = "exploit"
                job["tags"] = list(variant.get("tags") or [])
                job["field_slot_count"] = field_slot_count
                variant_settings_score = historical_settings_score(variant.get("settings") or {})
                lateral_bonus = 0.2 if "supply_lateral" in (variant.get("tags") or []) else 0.0
                self_improvement_bonus = 0.52 if "supply_self_improvement" in (variant.get("tags") or []) else 0.0
                mutation_bonus = 0.16 if "supply_mutation" in (variant.get("tags") or []) else 0.0
                crossover_bonus = 0.18 if "supply_crossover" in (variant.get("tags") or []) else 0.0
                hybrid_bonus = 0.18 if "supply_hybrid_breaker" in (variant.get("tags") or []) else 0.0
                category_score = float(category_scores.get(str(source_job.get("category") or "").lower()) or 0.0)
                job["supply_priority"] = (
                    result_score(payload)
                    + category_score * 0.22
                    + variant_settings_score * 0.35
                    + self_improvement_bonus
                    + lateral_bonus
                    + mutation_bonus
                    + crossover_bonus
                    + hybrid_bonus
                    + hierarchy_bonus
                    + _safe_float(payload.get("family_health_score"), 0.5) * 0.45
                    - (0.4 if payload.get("family_is_degrading") else 0.0)
                    - _safe_float(payload.get("exploit_quality_penalty"))
                    + max(0, field_slot_count - 1) * 0.45
                )
                job["source_family_key"] = payload.get("family_key")
                job["source_alpha_id"] = str(payload.get("alpha_id") or "")
                job["source_expression_hash"] = template_hash(str(payload.get("expression") or ""))
                job["source_effective_pass_count"] = payload.get("effective_pass_count")
                job["source_quality_penalty"] = payload.get("exploit_quality_penalty")
                job["source_quality_reasons"] = payload.get("exploit_quality_reasons")
                job["peer_alpha_id"] = str(variant.get("peer_alpha_id") or "")
                job["peer_expression_hash"] = str(variant.get("peer_expression_hash") or "")
                job["source_family_health_score"] = payload.get("family_health_score")
                job["source_family_is_degrading"] = payload.get("family_is_degrading")
                job["self_improvement_label"] = str(variant.get("self_improvement_label") or "")
                job["hybrid_style"] = str(variant.get("hybrid_style") or "")
                job["hybrid_peer_family_key"] = str(variant.get("hybrid_peer_family_key") or "")
                job["bandit_category"] = hierarchy_meta.get("category")
                job["bandit_strategies"] = hierarchy_meta.get("strategies")
                job["bandit_operator"] = hierarchy_meta.get("operator")
                job["generation_id"] = next_generation_id
                apply_family_feedback_priority(job, novelty_policy, family_feedback)
                apply_raw_family_action(job, raw_family_actions)
                grounding_bonus = grounding_fit_bonus(job)
                if grounding_bonus:
                    job["grounding_fit_bonus"] = grounding_bonus
                    job["supply_priority"] = _safe_float(job.get("supply_priority")) + grounding_bonus
                if "supply_self_improvement" in (variant.get("tags") or []):
                    self_improvement_job_count += 1
                    self_improvement_source_ids.add(str(payload.get("alpha_id") or ""))
                if "supply_hybrid_breaker" in (variant.get("tags") or []):
                    hybrid_breaker_job_count += 1
                    hybrid_breaker_source_ids.add(str(payload.get("alpha_id") or ""))
                exploit_jobs.append(job)

    if hopeful_enabled:
        hopeful_sources = load_hopeful_sources(
            base_jobs=base_jobs,
            limit=max_hopeful_sources,
        )
        hopeful_counter = 0
        for payload, source_job in hopeful_sources:
            expression_variants = build_hopeful_negation_variants(payload, max_variants=1)
            variants = apply_setting_variants(expression_variants, source_job, max_variants=1)
            for variant in variants:
                hopeful_counter += 1
                field_slot_count = len(extract_field_slots(variant))
                hierarchy_bonus, hierarchy_meta = hierarchical_bonus_for_template(variant, hierarchical_bandit)
                source_name = str(source_job.get("name") or "hopeful")
                variant_name = slugify_template_name(str(variant.get("name") or f"hopeful_{hopeful_counter}"))
                relative_template = Path("result_store") / "supply" / "templates" / "hopeful" / f"{variant_name}.yaml"
                template_output = ROOT_DIR / relative_template
                write_template(template_output, variant)
                templates_written.append(str(relative_template))

                job = copy.deepcopy(source_job)
                job["name"] = f"hopeful_{hopeful_counter:03d}_{source_name}"
                job["inventory_name"] = str(source_job.get("inventory_name") or source_name)
                job["template"] = str(relative_template).replace("\\", "/")
                align_job_field_sources(job, variant)
                job["refresh_inventory"] = True
                job["selection_limit"] = int(source_job.get("supply_selection_limit") or source_job.get("selection_limit") or 500)
                job["inventory_fetch_limit"] = int(source_job.get("supply_fetch_limit") or source_job.get("inventory_fetch_limit") or source_job.get("limit") or 1200)
                job["supply_mode"] = "exploit"
                job["tags"] = list(variant.get("tags") or [])
                job["field_slot_count"] = field_slot_count
                job["supply_priority"] = abs(result_score(payload)) + 0.25 + hierarchy_bonus + max(0, field_slot_count - 1) * 0.45
                job["bandit_category"] = hierarchy_meta.get("category")
                job["bandit_strategies"] = hierarchy_meta.get("strategies")
                job["bandit_operator"] = hierarchy_meta.get("operator")
                job["source_alpha_id"] = str(payload.get("alpha_id") or "")
                job["source_expression_hash"] = template_hash(str(payload.get("expression") or ""))
                job["generation_id"] = next_generation_id
                apply_family_feedback_priority(job, novelty_policy, family_feedback)
                apply_raw_family_action(job, raw_family_actions)
                grounding_bonus = grounding_fit_bonus(job)
                if grounding_bonus:
                    job["grounding_fit_bonus"] = grounding_bonus
                    job["supply_priority"] = _safe_float(job.get("supply_priority")) + grounding_bonus
                exploit_jobs.append(job)

    if high_grade_repair_enabled and bool(high_grade_repair_cfg.get("prioritize_successful_repairs", True)):
        high_quality_repair_sources = load_high_quality_repair_sources(
            base_jobs=base_jobs,
            limit=int(high_grade_repair_cfg.get("max_continuation_sources", 6)),
        )
        continuation_counter = 0
        for payload, source_job in high_quality_repair_sources:
            self_variants = build_self_improvement_variants(
                payload,
                max_variants=int(high_grade_repair_cfg.get("max_continuation_variants_per_source", 2)) * 2,
            )
            expression_variants = build_lateral_variants(
                payload,
                max_variants=int(high_grade_repair_cfg.get("max_continuation_variants_per_source", 2)),
            )
            variants = apply_setting_variants(
                self_variants + expression_variants,
                source_job,
                max_variants=int(high_grade_repair_cfg.get("max_continuation_variants_per_source", 2)) * 2,
            )
            for variant in variants:
                continuation_counter += 1
                field_slot_count = len(extract_field_slots(variant))
                hierarchy_bonus, hierarchy_meta = hierarchical_bonus_for_template(variant, hierarchical_bandit)
                source_name = str(source_job.get("name") or "repair_continuation")
                variant_name = slugify_template_name(str(variant.get("name") or f"repair_continuation_{continuation_counter}"))
                relative_template = Path("result_store") / "supply" / "templates" / "high_grade_repair_continuation" / f"{variant_name}.yaml"
                template_output = ROOT_DIR / relative_template
                write_template(template_output, variant)
                templates_written.append(str(relative_template))

                job = copy.deepcopy(source_job)
                job["name"] = f"repair_continuation_{continuation_counter:03d}_{source_name}"
                job["inventory_name"] = str(source_job.get("inventory_name") or source_name)
                job["template"] = str(relative_template).replace("\\", "/")
                align_job_field_sources(job, variant)
                job["refresh_inventory"] = True
                job["selection_limit"] = int(source_job.get("supply_selection_limit") or source_job.get("selection_limit") or 500)
                job["inventory_fetch_limit"] = int(source_job.get("supply_fetch_limit") or source_job.get("inventory_fetch_limit") or source_job.get("limit") or 1200)
                job["supply_mode"] = "exploit"
                tags = list(variant.get("tags") or [])
                for tag in ["high_grade_repair_continuation", "supply_lateral"]:
                    if tag not in tags:
                        tags.append(tag)
                job["tags"] = tags
                job["field_slot_count"] = field_slot_count
                job["supply_priority"] = (
                    result_score(payload)
                    + 1.6
                    + effective_pass_count(payload) * 0.12
                    + hierarchy_bonus
                    + max(0, field_slot_count - 1) * 0.45
                )
                job["source_alpha_id"] = str(payload.get("alpha_id") or "")
                job["source_expression_hash"] = template_hash(str(payload.get("expression") or ""))
                job["source_effective_pass_count"] = effective_pass_count(payload)
                job["repair_continuation_source"] = True
                job["generation_id"] = next_generation_id
                job["bandit_category"] = hierarchy_meta.get("category")
                job["bandit_strategies"] = hierarchy_meta.get("strategies")
                job["bandit_operator"] = hierarchy_meta.get("operator")
                apply_family_feedback_priority(job, novelty_policy, family_feedback)
                apply_raw_family_action(job, raw_family_actions)
                if "supply_self_improvement" in (variant.get("tags") or []):
                    self_improvement_job_count += 1
                    self_improvement_source_ids.add(str(payload.get("alpha_id") or ""))
                exploit_jobs.append(job)

    if high_grade_repair_enabled and bool(high_grade_repair_cfg.get("hybrid_breaker_enabled", True)):
        repair_hybrid_sources = load_exploit_sources(
            base_jobs=base_jobs,
            limit=int(high_grade_repair_cfg.get("max_hybrid_sources", 6)),
            min_score=float(high_grade_repair_cfg.get("min_hybrid_source_score", 0.0)),
            family_metrics=family_metrics,
        )
        repair_hybrid_sources = [
            (payload, source_job)
            for payload, source_job in sorted(
                repair_hybrid_sources,
                key=lambda item: hybrid_base_score(item[0]),
                reverse=True,
            )
            if is_high_grade_payload(payload) or is_high_quality_repair_payload(payload)
        ][: int(high_grade_repair_cfg.get("max_hybrid_sources", 6))]
        repair_hybrid_counter = 0
        for payload, source_job in repair_hybrid_sources:
            hybrid_peer_pool = load_hybrid_breaker_peers(
                limit=max(12, int(high_grade_repair_cfg.get("max_hybrid_peers_per_source", 3)) * 4),
                exclude_family=family_key_for_payload(payload),
            )
            selected_peers = select_hybrid_peers(
                payload,
                hybrid_peer_pool,
                limit=int(high_grade_repair_cfg.get("max_hybrid_peers_per_source", 3)),
            )
            expression_variants = build_hybrid_breaker_variants(
                payload,
                peer_payloads=selected_peers,
                max_variants=int(high_grade_repair_cfg.get("max_hybrid_variants_per_source", 2)) * 2,
            )
            variants = apply_setting_variants(
                expression_variants,
                source_job,
                max_variants=int(high_grade_repair_cfg.get("max_hybrid_variants_per_source", 2)),
            )
            for variant in variants:
                repair_hybrid_counter += 1
                field_slot_count = len(extract_field_slots(variant))
                hierarchy_bonus, hierarchy_meta = hierarchical_bonus_for_template(variant, hierarchical_bandit)
                source_name = str(source_job.get("name") or "repair_hybrid")
                variant_name = slugify_template_name(str(variant.get("name") or f"repair_hybrid_{repair_hybrid_counter}"))
                relative_template = Path("result_store") / "supply" / "templates" / "high_grade_repair_hybrid" / f"{variant_name}.yaml"
                template_output = ROOT_DIR / relative_template
                write_template(template_output, variant)
                templates_written.append(str(relative_template))

                job = copy.deepcopy(source_job)
                job["name"] = f"repair_hybrid_{repair_hybrid_counter:03d}_{source_name}"
                job["inventory_name"] = str(source_job.get("inventory_name") or source_name)
                job["template"] = str(relative_template).replace("\\", "/")
                align_job_field_sources(job, variant)
                job["refresh_inventory"] = True
                job["selection_limit"] = int(source_job.get("supply_selection_limit") or source_job.get("selection_limit") or 500)
                job["inventory_fetch_limit"] = int(source_job.get("supply_fetch_limit") or source_job.get("inventory_fetch_limit") or source_job.get("limit") or 1200)
                job["supply_mode"] = "exploit"
                tags = list(variant.get("tags") or [])
                for tag in ["high_grade_repair_hybrid", "supply_hybrid_breaker"]:
                    if tag not in tags:
                        tags.append(tag)
                job["tags"] = tags
                job["field_slot_count"] = field_slot_count
                job["supply_priority"] = (
                    hybrid_base_score(payload)
                    + 1.2
                    + hierarchy_bonus
                    + max(0, field_slot_count - 1) * 0.45
                )
                job["source_alpha_id"] = str(payload.get("alpha_id") or "")
                job["source_expression_hash"] = template_hash(str(payload.get("expression") or ""))
                job["source_effective_pass_count"] = effective_pass_count(payload)
                job["repair_hybrid_source"] = True
                job["repair_family_tag"] = ""
                job["hybrid_style"] = str(variant.get("hybrid_style") or "")
                job["hybrid_peer_family_key"] = str(variant.get("hybrid_peer_family_key") or "")
                job["peer_alpha_id"] = str(variant.get("peer_alpha_id") or "")
                job["peer_expression_hash"] = str(variant.get("peer_expression_hash") or "")
                job["generation_id"] = next_generation_id
                job["bandit_category"] = hierarchy_meta.get("category")
                job["bandit_strategies"] = hierarchy_meta.get("strategies")
                job["bandit_operator"] = hierarchy_meta.get("operator")
                apply_family_feedback_priority(job, novelty_policy, family_feedback)
                apply_raw_family_action(job, raw_family_actions)
                hybrid_breaker_job_count += 1
                hybrid_breaker_source_ids.add(str(payload.get("alpha_id") or ""))
                exploit_jobs.append(job)

    if high_grade_repair_enabled:
        repair_job_rows, repair_templates, high_grade_repair_summary = build_high_grade_repair_jobs(
            max_sources=int(high_grade_repair_cfg.get("max_sources", 10)),
            max_variants_per_source=int(high_grade_repair_cfg.get("max_variants_per_source", 6)),
            max_jobs=int(high_grade_repair_cfg.get("max_jobs", 24)),
        )
        for job in repair_job_rows:
            job["generation_id"] = next_generation_id
            job["supply_mode"] = "exploit"
            job["repair_resource_share_cap"] = float(high_grade_repair_cfg.get("max_share", 0.2))
            exploit_jobs.append(job)
        templates_written.extend(repair_templates)
        high_grade_repair_templates.extend(repair_templates)

    explore_jobs.sort(key=lambda item: float(item.get("supply_priority") or 0), reverse=True)
    exploit_jobs.sort(key=lambda item: float(item.get("supply_priority") or 0), reverse=True)
    if retired_families:
        exploit_jobs = [
            job
            for job in exploit_jobs
            if str(job.get("source_family_key") or "") not in retired_families
        ]
    if lineage_warehouse.get("lineages"):
        for job in exploit_jobs:
            penalty = lineage_warehouse_penalty(job, lineage_warehouse, generation_policy)
            if penalty:
                job["lineage_warehouse_penalty"] = round(penalty, 6)
                job["supply_priority"] = _safe_float(job.get("supply_priority")) - penalty
        exploit_jobs.sort(key=lambda item: float(item.get("supply_priority") or 0), reverse=True)
    exploit_jobs = enforce_family_job_caps(
        jobs=exploit_jobs,
        family_cap=int(generation_policy.get("family_job_cap", 6)),
        elite_bonus_cap=int(generation_policy.get("elite_family_bonus_cap", 2)),
        elite_families=elite_families,
    )
    snapshot = recent_performance_snapshot()
    jobs = choose_jobs_by_search_strategy(explore_jobs, exploit_jobs, max_jobs_per_cycle, snapshot)
    jobs = filter_duplicate_jobs(
        jobs,
        similarity_threshold=dedupe_similarity_threshold,
        historical_hashes=historical_result_hashes(),
    )
    candidate_pool = sorted(
        filter_duplicate_jobs(
            explore_jobs + exploit_jobs,
            similarity_threshold=dedupe_similarity_threshold,
            historical_hashes=historical_result_hashes(),
        ),
        key=lambda item: _safe_float(item.get("supply_priority")),
        reverse=True,
    )
    jobs = enforce_family_job_caps(
        jobs=jobs,
        family_cap=int(generation_policy.get("family_job_cap", 6)),
        elite_bonus_cap=int(generation_policy.get("elite_family_bonus_cap", 2)),
        elite_families=elite_families,
    )
    jobs = backfill_generation_quota(
        selected=jobs,
        candidate_pool=candidate_pool,
        predicate=is_pair_job,
        min_count=int(generation_policy.get("min_pair_jobs", 12)),
    )
    jobs = backfill_generation_quota(
        selected=jobs,
        candidate_pool=candidate_pool,
        predicate=lambda job: has_job_tag(job, "supply_lateral"),
        min_count=int(generation_policy.get("min_lateral_jobs", 6)),
    )
    jobs = backfill_generation_quota(
        selected=jobs,
        candidate_pool=candidate_pool,
        predicate=lambda job: has_job_tag(job, "supply_mutation"),
        min_count=int(generation_policy.get("min_mutation_jobs", 4)),
    )
    jobs = backfill_generation_quota(
        selected=jobs,
        candidate_pool=candidate_pool,
        predicate=lambda job: has_job_tag(job, "supply_crossover"),
        min_count=int(generation_policy.get("min_crossover_jobs", 4)),
    )
    if high_grade_repair_enabled:
        repair_min_jobs = min(
            int(high_grade_repair_cfg.get("min_jobs", 4)),
            int(max_jobs_per_cycle * float(high_grade_repair_cfg.get("max_share", 0.2))),
        )
        jobs = backfill_generation_quota(
            selected=jobs,
            candidate_pool=candidate_pool,
            predicate=lambda job: str(job.get("repair_engine") or "") == "high_grade_failed_repair",
            min_count=repair_min_jobs,
        )
        continuation_min_jobs = min(
            int(high_grade_repair_cfg.get("min_continuation_jobs", 2)),
            int(max_jobs_per_cycle * float(high_grade_repair_cfg.get("max_continuation_share", 0.08))),
        )
        jobs = backfill_generation_quota(
            selected=jobs,
            candidate_pool=candidate_pool,
            predicate=lambda job: bool(job.get("repair_continuation_source")) or has_job_tag(job, "high_grade_repair_continuation"),
            min_count=continuation_min_jobs,
        )
    jobs, novelty_summary = apply_research_novelty_policy(
        jobs=jobs,
        candidate_pool=candidate_pool,
        max_jobs_per_cycle=max_jobs_per_cycle,
        policy=novelty_policy,
    )
    jobs = backfill_generation_quota(
        selected=jobs,
        candidate_pool=candidate_pool,
        predicate=is_pair_job,
        min_count=int(generation_policy.get("min_pair_jobs", 12)),
    )
    jobs = backfill_generation_quota(
        selected=jobs,
        candidate_pool=candidate_pool,
        predicate=lambda job: has_job_tag(job, "supply_lateral"),
        min_count=int(generation_policy.get("min_lateral_jobs", 6)),
    )
    jobs = backfill_generation_quota(
        selected=jobs,
        candidate_pool=candidate_pool,
        predicate=lambda job: has_job_tag(job, "supply_mutation"),
        min_count=int(generation_policy.get("min_mutation_jobs", 4)),
    )
    jobs = backfill_generation_quota(
        selected=jobs,
        candidate_pool=candidate_pool,
        predicate=lambda job: has_job_tag(job, "supply_crossover"),
        min_count=int(generation_policy.get("min_crossover_jobs", 4)),
    )
    if high_grade_repair_enabled:
        continuation_min_jobs = min(
            int(high_grade_repair_cfg.get("min_continuation_jobs", 2)),
            int(max_jobs_per_cycle * float(high_grade_repair_cfg.get("max_continuation_share", 0.08))),
        )
        jobs = backfill_generation_quota(
            selected=jobs,
            candidate_pool=candidate_pool,
            predicate=lambda job: bool(job.get("repair_continuation_source")) or has_job_tag(job, "high_grade_repair_continuation"),
            min_count=continuation_min_jobs,
        )
    jobs, raw_family_diversity = enforce_raw_family_diversity_gate(
        selected=jobs,
        candidate_pool=candidate_pool,
        max_jobs=max_jobs_per_cycle,
        policy=novelty_policy,
    )
    if high_grade_repair_enabled and bool(high_grade_repair_cfg.get("frontload_enabled", True)):
        repair_front_jobs = min(
            int(high_grade_repair_cfg.get("frontload_jobs", high_grade_repair_cfg.get("min_jobs", 4))),
            int(max_jobs_per_cycle * float(high_grade_repair_cfg.get("frontload_max_share", high_grade_repair_cfg.get("max_share", 0.2)))),
        )
        jobs = frontload_generation_quota(
            selected=jobs,
            candidate_pool=candidate_pool,
            predicate=lambda job: str(job.get("repair_engine") or "") == "high_grade_failed_repair",
            target_count=repair_front_jobs,
            max_jobs=max_jobs_per_cycle,
        )
        continuation_front_jobs = min(
            int(high_grade_repair_cfg.get("frontload_continuation_jobs", high_grade_repair_cfg.get("min_continuation_jobs", 2))),
            int(max_jobs_per_cycle * float(high_grade_repair_cfg.get("max_continuation_share", 0.08))),
        )
        jobs = frontload_generation_quota(
            selected=jobs,
            candidate_pool=candidate_pool,
            predicate=lambda job: bool(job.get("repair_continuation_source")) or has_job_tag(job, "high_grade_repair_continuation"),
            target_count=continuation_front_jobs,
            max_jobs=max_jobs_per_cycle,
        )
    for job in jobs:
        annotate_raw_family_features(job)
    novelty_summary["final_field_clusters"] = novelty_bucket_counts(jobs, "field_clusters")
    novelty_summary["final_modes"] = novelty_bucket_counts(jobs, "supply_mode")
    novelty_summary["final_mechanisms"] = novelty_bucket_counts(jobs, "mechanism_id")
    novelty_summary["final_expression_families"] = novelty_bucket_counts(jobs, "expression_family")
    novelty_summary["raw_family_diversity"] = raw_family_diversity
    if high_grade_repair_enabled and bool(high_grade_repair_cfg.get("frontload_enabled", True)):
        jobs = sorted(
            jobs,
            key=lambda item: (
                0 if str(item.get("repair_engine") or "") == "high_grade_failed_repair" else 1,
                0 if bool(item.get("repair_continuation_source")) or has_job_tag(item, "high_grade_repair_continuation") else 1,
                -_safe_float(item.get("supply_priority")),
            ),
        )[:max_jobs_per_cycle]
    else:
        jobs = sorted(jobs, key=lambda item: _safe_float(item.get("supply_priority")), reverse=True)[:max_jobs_per_cycle]
    lineage_rollup = build_lineage_summary(
        jobs,
        lineage_summary=population_archive.get("lineage_summary") or {},
    )
    lineage_warehouse = update_lineage_warehouse(
        warehouse=lineage_warehouse,
        jobs=jobs,
        population_archive=population_archive,
        generation_id=next_generation_id,
    )
    save_lineage_warehouse(lineage_warehouse)
    generation_summary = build_generation_summary(
        generation_id=next_generation_id,
        jobs=jobs,
        exploit_source_count=len(exploit_sources) if exploit_enabled else 0,
        hopeful_source_count=len(hopeful_sources) if hopeful_enabled else 0,
        population_archive=population_archive,
        lineage_rollup=lineage_rollup,
        generation_state=generation_state,
    )
    generation_state = finalize_generation_state(generation_state, generation_summary)
    SUPPLY_DIR.mkdir(parents=True, exist_ok=True)
    SUPPLY_GENERATION_STATE_FILE.write_text(json.dumps(generation_state, ensure_ascii=False, indent=2), encoding="utf-8")
    template_cleanup = prune_stale_supply_templates(templates_written)
    raw_alpha_capacity_summary: list[dict] = []
    if raw_alpha_rotation_summary:
        for family in raw_alpha_rotation_summary.get("selected_families") or []:
            family_jobs = [job for job in jobs if str(job.get("raw_alpha_family") or "") == family]
            if not family_jobs:
                continue
            estimated_field_map_cap = max(int(job.get("estimated_field_map_cap") or 0) for job in family_jobs)
            raw_alpha_capacity_summary.append(
                {
                    "family": family,
                    "job_count": len(family_jobs),
                    "estimated_field_map_cap": estimated_field_map_cap,
                    "rough_backtest_capacity": len(family_jobs) * estimated_field_map_cap,
                }
            )
        raw_alpha_rotation_summary["capacity"] = raw_alpha_capacity_summary
    job_metadata_archive = save_job_metadata_archive(jobs, next_generation_id)

    return {
        "schema_version": 1,
        "generation_id": next_generation_id,
        "job_count": len(jobs),
        "template_count": len(templates_written),
        "templates": templates_written,
        "search_strategy": snapshot,
        "category_scores": category_scores,
        "hierarchical_bandit": hierarchical_bandit,
        "generation_summary": generation_summary,
        "generation_policy": generation_policy,
        "research_novelty_mode": novelty_summary,
        "family_feedback": {
            "enabled": bool(novelty_policy.get("family_feedback_enabled", True)),
            "family_count": len(family_feedback),
            "decisions": novelty_bucket_counts(jobs, "family_feedback_decision"),
        },
        "raw_family_actions": {
            "action_count": len(raw_family_actions),
            "decisions": novelty_bucket_counts(jobs, "raw_family_action_decision"),
        },
        "grounding_fit": {
            "with_bonus_count": len([job for job in jobs if job.get("grounding_fit_bonus")]),
            "avg_bonus": round(
                sum(_safe_float(job.get("grounding_fit_bonus")) for job in jobs) / max(1, len(jobs)),
                6,
            ),
            "semantic_fit_scores": novelty_bucket_counts(jobs, "semantic_fit_score"),
            "semantic_tags": novelty_bucket_counts(jobs, "field_semantic_tags"),
            "semantic_overlap": novelty_bucket_counts(jobs, "semantic_tag_overlap"),
        },
        "raw_family_diversity": raw_family_diversity,
        "high_grade_repair": {
            **(high_grade_repair_summary or {}),
            "enabled": high_grade_repair_enabled,
            "template_count": len(high_grade_repair_templates),
            "selected_jobs": len(
                [
                    job
                    for job in jobs
                    if str(job.get("repair_engine") or "") == "high_grade_failed_repair"
                ]
            ),
            "continuation_source_count": len(high_quality_repair_sources),
            "continuation_selected_jobs": len(
                [
                    job
                    for job in jobs
                    if bool(job.get("repair_continuation_source")) or has_job_tag(job, "high_grade_repair_continuation")
                ]
            ),
            "resource_share": ratio(
                len(
                    [
                        job
                        for job in jobs
                        if str(job.get("repair_engine") or "") == "high_grade_failed_repair"
                    ]
                ),
                len(jobs),
            ),
        },
        "hybrid_breaker": {
            "enabled": hybrid_breaker_enabled,
            "job_count": hybrid_breaker_job_count,
            "source_count": len(hybrid_breaker_source_ids),
            "selected_jobs": len([job for job in jobs if has_job_tag(job, "supply_hybrid_breaker")]),
            "repair_selected_jobs": len([job for job in jobs if has_job_tag(job, "high_grade_repair_hybrid")]),
            "continuation_selected_jobs": len([job for job in jobs if bool(job.get("repair_hybrid_source"))]),
            "styles": novelty_bucket_counts(jobs, "hybrid_style"),
        },
        "self_improvement": {
            "job_count": self_improvement_job_count,
            "source_count": len(self_improvement_source_ids),
            "selected_jobs": len([job for job in jobs if has_job_tag(job, "supply_self_improvement")]),
            "labels": novelty_bucket_counts(jobs, "self_improvement_label"),
        },
        "population_summary": {
            "family_count": population_archive.get("family_count"),
            "elite_family_count": len(population_archive.get("elite_families") or []),
            "degrading_family_count": len(population_archive.get("degrading_families") or []),
            "retired_family_count": len(generation_state.get("retired_families") or []),
            "elite_families": population_archive.get("elite_families") or [],
            "degrading_families": population_archive.get("degrading_families") or [],
            "retired_families": generation_state.get("retired_families") or [],
        },
        "lineage_summary": lineage_rollup,
        "lineage_warehouse_summary": {
            "lineage_count": lineage_warehouse.get("lineage_count", 0),
            "top_lineages": lineage_warehouse.get("top_lineages") or [],
            "offspring_count": sum(
                len((lineage or {}).get("offspring") or [])
                for lineage in (lineage_warehouse.get("lineages") or {}).values()
                if isinstance(lineage, dict)
            ),
        },
        "raw_alpha_rotation": raw_alpha_rotation_summary,
        "raw_rotation_active": raw_rotation_active,
        "raw_rotation_replace_static_jobs": replace_static_jobs,
        "job_metadata_archive": job_metadata_archive,
        "template_cleanup": template_cleanup,
        "explore_job_count": len([job for job in jobs if job.get("supply_mode") == "explore"]),
        "exploit_job_count": len([job for job in jobs if job.get("supply_mode") == "exploit"]),
        "jobs": jobs,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Build continuous supply jobs from base orchestrator config.")
    parser.add_argument("config", help="Continuous orchestrator YAML config")
    args = parser.parse_args()

    config_path = Path(args.config).resolve()
    config = load_config(config_path)
    payload = build_supply_jobs(config)
    SUPPLY_DIR.mkdir(parents=True, exist_ok=True)
    SUPPLY_JOBS_FILE.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    print("=" * 72)
    print("Continuous supply build complete")
    print("Supply jobs:", payload["job_count"])
    print("Supply templates:", payload["template_count"])
    print("Explore jobs:", payload.get("explore_job_count"))
    print("Exploit jobs:", payload.get("exploit_job_count"))
    print("Generation:", payload.get("generation_id"))
    bandit = payload.get("hierarchical_bandit") or {}
    print("Bandit categories:", len(bandit.get("category_bandit") or {}))
    print("Bandit strategies:", len(bandit.get("strategy_bandit") or {}))
    print("Bandit operators:", len(bandit.get("operator_bandit") or {}))
    quality_feedback = bandit.get("quality_feedback") or {}
    if quality_feedback:
        print("Quality feedback:", quality_feedback)
    family_feedback = payload.get("family_feedback") or {}
    if family_feedback:
        print("Family feedback:", family_feedback)
    population_summary = payload.get("population_summary") or {}
    print("Population families:", population_summary.get("family_count"))
    print("Degrading families:", population_summary.get("degrading_family_count"))
    print("Retired families:", population_summary.get("retired_family_count"))
    generation_summary = payload.get("generation_summary") or {}
    print("Generation exploit sources:", generation_summary.get("exploit_source_count"))
    print("Generation lineages:", generation_summary.get("lineage_family_count"))
    warehouse_summary = payload.get("lineage_warehouse_summary") or {}
    print("Warehouse lineages:", int(warehouse_summary.get("lineage_count") or 0))
    print("Warehouse offspring:", int(warehouse_summary.get("offspring_count") or 0))
    raw_alpha_rotation = payload.get("raw_alpha_rotation") or {}
    if raw_alpha_rotation:
        print("Raw alpha rotation window:", raw_alpha_rotation.get("window_index"))
        print("Raw alpha families:", ", ".join(raw_alpha_rotation.get("selected_families") or []))
        for row in raw_alpha_rotation.get("capacity") or []:
            print(
                "  -",
                row.get("family"),
                f"jobs={row.get('job_count')}",
                f"rough_capacity={row.get('rough_backtest_capacity')}",
            )
    raw_family_diversity = payload.get("raw_family_diversity") or {}
    for family, report in (raw_family_diversity.get("families") or {}).items():
        print(
            "Raw family diversity:",
            family,
            f"passed={report.get('passed')}",
            f"expr={report.get('expression_signature_count')}",
            f"field_clusters={report.get('field_cluster_count')}",
            f"settings={report.get('settings_cluster_count')}",
            f"top_expr_share={report.get('top_expression_signature_share')}",
        )
    print("Output:", SUPPLY_JOBS_FILE)
    print("=" * 72)


if __name__ == "__main__":
    main()
