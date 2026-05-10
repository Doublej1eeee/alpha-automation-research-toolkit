#!/usr/bin/env python
"""Conservative repair variants for high-grade but failed alphas.

This module targets EXCELLENT/SPECTACULAR alphas that still fail submission
checks. It never changes the color/tag decision for the source alpha; every
repair variant must be simulated again and will be re-colored by the normal
submit-check rules.
"""

from __future__ import annotations

import argparse
import copy
from collections import Counter
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
from typing import Any

import yaml


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from brain_client import iter_all_result_payloads  # noqa: E402
from brain_client import build_alpha_fingerprint  # noqa: E402
from script.batch_runtime import load_tested_fingerprints  # noqa: E402
from script.template_similarity import template_hash  # noqa: E402
from script.template_validator import validate_template_payload  # noqa: E402


REPAIR_TEMPLATE_DIR = ROOT_DIR / "result_store" / "supply" / "templates" / "high_grade_repair"
REPORT_PATH = ROOT_DIR / "result_store" / "analysis" / "high_grade_repair_report.json"
LIFECYCLE_PATH = ROOT_DIR / "result_store" / "analysis" / "high_grade_repair_lifecycle.json"
REPAIR_FAMILY_ACTIONS_PATH = ROOT_DIR / "result_store" / "analysis" / "repair_family_actions.json"
FALLBACK_REPORT_PATH = ROOT_DIR / "temp" / "high_grade_repair" / "high_grade_repair_report.json"
FALLBACK_LIFECYCLE_PATH = ROOT_DIR / "temp" / "high_grade_repair" / "high_grade_repair_lifecycle.json"
REPAIRABLE_CHECKS = {
    "CONCENTRATED_WEIGHT",
    "LOW_SUB_UNIVERSE_SHARPE",
    "HIGH_TURNOVER",
    "LOW_TURNOVER",
    "LOW_FITNESS",
}
REPAIR_TERMINAL_CHECKS = {
    "SELF_CORRELATION",
    "PROD_CORRELATION",
    "MATCHES_COMPETITION",
}
TARGET_GRADES = {"EXCELLENT", "SPECTACULAR"}
REPAIR_TAG = "1REPAIR"
REPAIR_FAMILY_TAG_PREFIX = "FAM"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def slugify(value: str, limit: int = 96) -> str:
    return re.sub(r"[^A-Za-z0-9_]+", "_", str(value or "")).strip("_").lower()[:limit] or "repair"


def safe_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except Exception:
        return default


def repair_family_tag(payload: dict[str, Any]) -> str:
    for tag in list(payload.get("tags") or []) + list((payload.get("display") or {}).get("tags") or []):
        tag_text = str(tag or "").strip().upper()
        if tag_text.startswith(f"{REPAIR_FAMILY_TAG_PREFIX}_"):
            return tag_text
    expression = str(payload.get("expression") or "")
    operators = "_".join(re.findall(r"\b([a-z_][a-z0-9_]*)\s*\(", expression.lower())[:8])
    normalized = re.sub(r"\b[a-z][a-z0-9_]*\b", "field", expression.lower())
    normalized = re.sub(r"\d+", "n", normalized)
    normalized = re.sub(r"\s+", "", normalized)
    return f"{REPAIR_FAMILY_TAG_PREFIX}_{template_hash(f'{operators}:{normalized}')[:12].upper()}"


def effective_checks(payload: dict[str, Any]) -> list[dict[str, Any]]:
    details = payload.get("alpha_details") or {}
    preview_checks = ((details.get("submitPreview") or {}).get("is") or {}).get("checks")
    if isinstance(preview_checks, list) and preview_checks:
        return [row for row in preview_checks if isinstance(row, dict)]
    submit_checks = ((details.get("is") or {}).get("submitChecks")) or []
    if isinstance(submit_checks, list) and submit_checks:
        return [row for row in submit_checks if isinstance(row, dict)]
    checks = ((details.get("is") or {}).get("checks")) or []
    return [row for row in checks if isinstance(row, dict)]


def failed_check_names(payload: dict[str, Any]) -> list[str]:
    names: list[str] = []
    for check in effective_checks(payload):
        if str(check.get("result") or "").upper() == "FAIL":
            name = str(check.get("name") or "").upper()
            if name:
                names.append(name)
    return names


def is_repair_result(payload: dict[str, Any]) -> bool:
    return (
        "high_grade_repair" in str(payload.get("source_file") or "").lower()
        or "repair_" in str(payload.get("batch_name") or "").lower()
    )


def alpha_id_of(payload: dict[str, Any]) -> str:
    return str(payload.get("alpha_id") or ((payload.get("alpha_details") or {}).get("id")) or "")


def repair_source_slug(payload: dict[str, Any]) -> str:
    alpha_id = alpha_id_of(payload)
    return slugify(alpha_id, limit=32)


def repair_result_source_slug(payload: dict[str, Any]) -> str:
    text = " ".join([str(payload.get("batch_name") or ""), str(payload.get("source_file") or "")]).lower()
    match = re.search(r"\brepair_([a-z0-9]+)_", text)
    return match.group(1) if match else ""


def completed_check_names(payload: dict[str, Any], result: str) -> list[str]:
    target = str(result or "").upper()
    return [
        str(check.get("name") or "").upper()
        for check in effective_checks(payload)
        if str(check.get("result") or "").upper() == target and str(check.get("name") or "")
    ]


def pass_count(payload: dict[str, Any]) -> int:
    return sum(1 for check in effective_checks(payload) if str(check.get("result") or "").upper() == "PASS")


def is_high_grade_failed(payload: dict[str, Any]) -> bool:
    details = payload.get("alpha_details") or {}
    grade = str(details.get("grade") or payload.get("grade") or "").upper()
    if grade not in TARGET_GRADES:
        return False
    failed = failed_check_names(payload)
    if not failed:
        return False
    if any(name in REPAIR_TERMINAL_CHECKS for name in failed):
        return False
    if not any(name in REPAIRABLE_CHECKS for name in failed):
        return False
    expression = str(payload.get("expression") or "").strip()
    if not expression or "{{" in expression or "}}" in expression:
        return False
    settings = payload.get("settings") or {}
    return isinstance(settings, dict) and bool(settings)


def is_submit_eligible(payload: dict[str, Any]) -> bool:
    details = payload.get("alpha_details") or {}
    checks = effective_checks(payload)
    if not checks:
        return False
    failed = [check for check in checks if str(check.get("result") or "").upper() == "FAIL"]
    pending = [check for check in checks if str(check.get("result") or "").upper() == "PENDING"]
    return not failed and not pending and pass_count(payload) >= 8 and str(details.get("grade") or payload.get("grade") or "").upper() in TARGET_GRADES


def is_temp_special_concentration_source(payload: dict[str, Any]) -> bool:
    """Temporary focus: high-grade alphas that are very close and only fail concentrated weight."""
    details = payload.get("alpha_details") or {}
    grade = str(details.get("grade") or payload.get("grade") or "").upper()
    return (
        grade == "SPECTACULAR"
        and pass_count(payload) >= 6
        and set(failed_check_names(payload)) == {"CONCENTRATED_WEIGHT"}
    )


def repair_priority(payload: dict[str, Any]) -> float:
    details = payload.get("alpha_details") or {}
    is_block = details.get("is") or {}
    grade = str(details.get("grade") or payload.get("grade") or "").upper()
    failed = set(failed_check_names(payload))
    score = 0.0
    score += 3.0 if grade == "SPECTACULAR" else 2.2
    score += safe_float(is_block.get("sharpe")) * 0.35
    score += safe_float(is_block.get("fitness")) * 0.18
    score += pass_count(payload) * 0.18
    if failed <= {"CONCENTRATED_WEIGHT", "SELF_CORRELATION"}:
        score += 0.8
    if is_temp_special_concentration_source(payload):
        score += 8.0
    if "LOW_SUB_UNIVERSE_SHARPE" in failed:
        score += 0.35
    if len(failed) > 2:
        score -= 0.5 * (len(failed) - 2)
    return round(score, 6)


def expression_variants(expression: str, failed: set[str], max_variants: int) -> list[tuple[str, str]]:
    variants: list[tuple[str, str]] = []

    def add(label: str, expr: str) -> None:
        if len(variants) >= max_variants:
            return
        expr = str(expr or "").strip()
        if not expr:
            return
        if any(existing == expr for _, existing in variants):
            return
        variants.append((label, expr))

    lower_expression = expression.lower()
    concentrated = "CONCENTRATED_WEIGHT" in failed
    concentration_and_subuni = concentrated and "LOW_SUB_UNIVERSE_SHARPE" in failed
    if concentrated:
        if "ts_backfill(" not in lower_expression and "group_rank(" not in lower_expression:
            add("expr_group_rank_subindustry_backfill60", f"group_rank(ts_backfill({expression}, 60), subindustry)")
            add("expr_group_rank_industry_backfill60", f"group_rank(ts_backfill({expression}, 60), industry)")
        if "ts_backfill(" not in lower_expression and "group_zscore(" not in lower_expression:
            add("expr_group_zscore_subindustry_backfill60", f"group_zscore(ts_backfill({expression}, 60), subindustry)")
            add("expr_group_zscore_industry_backfill60", f"group_zscore(ts_backfill({expression}, 60), industry)")
        if (
            "ts_backfill(" not in lower_expression
            and "winsorize(" not in lower_expression
            and "group_rank(" not in lower_expression
        ):
            add(
                "expr_group_rank_subindustry_backfill60_winsorize2",
                f"group_rank(winsorize(ts_backfill({expression}, 60), std=2), subindustry)",
            )
            add(
                "expr_group_rank_industry_backfill60_winsorize2",
                f"group_rank(winsorize(ts_backfill({expression}, 60), std=2), industry)",
            )
        if "ts_backfill(" not in lower_expression and "rank(" not in lower_expression:
            add("expr_rank_backfill60", f"rank(ts_backfill({expression}, 60))")
        if "group_rank(" not in lower_expression:
            add("expr_group_rank_subindustry_current", f"group_rank({expression}, subindustry)")
            add("expr_group_rank_industry_current", f"group_rank({expression}, industry)")
        if "group_zscore(" not in lower_expression:
            add("expr_group_zscore_subindustry_current", f"group_zscore({expression}, subindustry)")
            add("expr_group_zscore_industry_current", f"group_zscore({expression}, industry)")
        if "group_rank(" not in lower_expression and "winsorize(" not in lower_expression:
            add("expr_group_rank_subindustry_winsorize2", f"group_rank(winsorize({expression}, std=2), subindustry)")
            add("expr_group_rank_industry_winsorize2", f"group_rank(winsorize({expression}, std=2), industry)")
        if "group_zscore(" not in lower_expression and "winsorize(" not in lower_expression:
            add("expr_group_zscore_subindustry_winsorize2", f"group_zscore(winsorize({expression}, std=2), subindustry)")
            add("expr_group_zscore_industry_winsorize2", f"group_zscore(winsorize({expression}, std=2), industry)")
        if "rank(" not in lower_expression and "winsorize(" not in lower_expression:
            add("expr_rank_winsorize2", f"rank(winsorize({expression}, std=2))")
        if "zscore(" not in lower_expression and "winsorize(" not in lower_expression:
            add("expr_zscore_winsorize2", f"zscore(winsorize({expression}, std=2))")
    if "LOW_SUB_UNIVERSE_SHARPE" in failed:
        if "group_rank(" not in lower_expression:
            add("expr_group_rank_industry", f"group_rank({expression}, industry)")
            add("expr_group_rank_subindustry", f"group_rank({expression}, subindustry)")
        if "group_zscore(" not in lower_expression:
            add("expr_group_zscore_industry", f"group_zscore({expression}, industry)")
            add("expr_group_zscore_subindustry", f"group_zscore({expression}, subindustry)")
        if "rank(" not in lower_expression:
            add("expr_rank_subuni", f"rank({expression})")
    if concentrated:
        if "winsorize(" not in lower_expression:
            add("expr_winsorize2", f"winsorize({expression}, std=2)")
            add("expr_winsorize3", f"winsorize({expression}, std=3)")
            add("expr_winsorize4", f"winsorize({expression}, std=4)")
        if "rank(" not in lower_expression:
            add("expr_rank", f"rank({expression})")
        if "zscore(" not in lower_expression:
            add("expr_zscore", f"zscore({expression})")
        if "signed_power(" not in lower_expression:
            add("expr_signed_power_0p5", f"signed_power({expression}, 0.5)")
            add("expr_signed_power_0p25", f"signed_power({expression}, 0.25)")
        if "hump(" not in lower_expression:
            add("expr_hump", f"hump({expression}, 0.01)")
        if "ts_backfill(" not in lower_expression:
            add("expr_ts_backfill20", f"ts_backfill({expression}, 20)")
    if concentration_and_subuni:
        if "group_rank(" not in lower_expression and "winsorize(" not in lower_expression:
            add("expr_group_rank_industry_winsorize2", f"group_rank(winsorize({expression}, std=2), industry)")
            add("expr_group_rank_subindustry_winsorize2", f"group_rank(winsorize({expression}, std=2), subindustry)")
        if "trade_when(" not in lower_expression:
            add("expr_trade_when_liquid", f"trade_when(ts_rank(volume, 60) > 0.2, {expression}, -1)")
    if "HIGH_TURNOVER" in failed:
        if "ts_mean(" not in expression:
            add("expr_ts_mean5", f"ts_mean({expression}, 5)")
            add("expr_ts_mean10", f"ts_mean({expression}, 10)")
        if "ts_rank(" not in expression:
            add("expr_ts_rank20", f"ts_rank({expression}, 20)")
        if "trade_when(" not in expression:
            add("expr_trade_when_volume", f"trade_when(ts_rank(volume, 20) > 0.45, {expression}, -1)")
    if "LOW_TURNOVER" in failed:
        if "ts_delta(" not in expression:
            add("expr_delta5", f"ts_delta({expression}, 5)")
        if "ts_mean(" not in expression:
            add("expr_ts_mean3", f"ts_mean({expression}, 3)")
    if "LOW_FITNESS" in failed:
        if "rank(" not in lower_expression:
            add("expr_rank_fitness", f"rank({expression})")
        if "ts_rank(" not in lower_expression:
            add("expr_ts_rank60", f"ts_rank({expression}, 60)")
    if not variants:
        add("expr_base", expression)
    return variants[:max_variants]


def settings_variants(settings: dict[str, Any], failed: set[str], max_variants: int) -> list[tuple[str, dict[str, Any]]]:
    variants: list[tuple[str, dict[str, Any]]] = []

    def key(row: dict[str, Any]) -> tuple:
        return (
            str(row.get("neutralization") or ""),
            str(row.get("universe") or ""),
            int(row.get("delay") or 0),
            int(row.get("decay") or 0),
            float(row.get("truncation") or 0.0),
        )

    seen = {key(settings)}

    def add(label: str, changes: dict[str, Any]) -> None:
        if len(variants) >= max_variants:
            return
        candidate = copy.deepcopy(settings)
        candidate.update(changes)
        candidate.setdefault("language", "FASTEXPR")
        candidate.setdefault("instrumentType", "EQUITY")
        candidate.setdefault("region", "USA")
        variant_key = key(candidate)
        if variant_key in seen:
            return
        seen.add(variant_key)
        variants.append((label, candidate))

    if "CONCENTRATED_WEIGHT" in failed:
        for truncation in [0.1, 0.08, 0.05, 0.03, 0.01]:
            if float(settings.get("truncation") or 0.0) != truncation:
                add(f"trunc_{str(truncation).replace('.', 'p')}", {"truncation": truncation})
        for neutralization in ["SUBINDUSTRY", "INDUSTRY", "SECTOR", "MARKET"]:
            if str(settings.get("neutralization") or "") != neutralization:
                add(f"neu_{slugify(neutralization)}", {"neutralization": neutralization})
    if "LOW_SUB_UNIVERSE_SHARPE" in failed:
        for universe in ["TOP1000", "TOP3000"]:
            if str(settings.get("universe") or "") != universe:
                add(f"uni_{slugify(universe)}", {"universe": universe})
        for neutralization in ["INDUSTRY", "SECTOR", "SUBINDUSTRY", "MARKET"]:
            if str(settings.get("neutralization") or "") != neutralization:
                add(f"subuni_neu_{slugify(neutralization)}", {"neutralization": neutralization})
    if "HIGH_TURNOVER" in failed or "LOW_FITNESS" in failed:
        current = int(settings.get("decay") or 0)
        for decay in [2, 4, 6, 10, 12, 20]:
            if current != decay:
                add(f"decay_{decay}", {"decay": decay})
    if "LOW_TURNOVER" in failed:
        current = int(settings.get("decay") or 0)
        for decay in [0, 2, 4]:
            if current != decay:
                add(f"decay_{decay}", {"decay": decay})
    if not variants:
        variants.append(("basecfg", copy.deepcopy(settings)))
    return variants[:max_variants]


def build_template(payload: dict[str, Any], name: str, expression: str, settings: dict[str, Any], labels: list[str]) -> dict[str, Any]:
    details = payload.get("alpha_details") or {}
    family_tag = repair_family_tag(payload)
    return {
        "name": name,
        "type": "REGULAR",
        "category": str(payload.get("category") or (details.get("category") or "FUNDAMENTAL")).upper(),
        # Repair descendants are not waiting sources. Only sync_repair_wait_tags.py
        # may put 1REPAIR on first-generation high-grade failed source alphas.
        "tags": [family_tag],
        "description": (
            "Conservative repair variant for high-grade failed alpha. "
            "Normal color/tag rules apply only after this variant is simulated."
        ),
        "settings": settings,
        "expression": expression,
    }


def repair_fingerprint(expression: str, settings: dict[str, Any]) -> str:
    return build_alpha_fingerprint(
        {
            "type": "REGULAR",
            "expression": expression,
            "settings": settings,
        }
    )


def repair_combinations(
    expression: str,
    settings: dict[str, Any],
    failed: set[str],
    max_variants: int,
) -> list[tuple[str, str, str, dict[str, Any]]]:
    expression_budget = max(1, min(14, max_variants + 8))
    setting_budget = max(1, min(10, max_variants + 4))
    exprs = expression_variants(expression, failed, max_variants=expression_budget)
    setting_rows = settings_variants(settings, failed, max_variants=setting_budget)
    base_setting = ("basecfg", copy.deepcopy(settings))
    first_setting = setting_rows[0] if setting_rows else base_setting
    rows: list[tuple[str, str, str, dict[str, Any]]] = []
    seen: set[str] = set()

    def add(expr_label: str, expr: str, setting_label: str, setting: dict[str, Any]) -> None:
        signature = json.dumps({"expression": expr, "settings": setting}, sort_keys=True, ensure_ascii=False)
        if signature in seen:
            return
        seen.add(signature)
        rows.append((expr_label, expr, setting_label, copy.deepcopy(setting)))

    for expr_label, expr in exprs:
        if "CONCENTRATED_WEIGHT" in failed:
            add(expr_label, expr, first_setting[0], first_setting[1])
        add(expr_label, expr, base_setting[0], base_setting[1])

    for setting_label, setting in setting_rows:
        add("expr_base", expression, setting_label, setting)

    for expr_label, expr in exprs:
        for setting_label, setting in setting_rows:
            add(expr_label, expr, setting_label, setting)

    def combo_priority(row: tuple[str, str, str, dict[str, Any]]) -> tuple[int, int, int, int, int, int]:
        expr_label, expr, setting_label, setting = row
        expr_text = str(expr or "").lower()
        is_backfilled = int("ts_backfill(" in expr_text)
        is_group_spread = int(("group_rank(" in expr_text) or ("group_zscore(" in expr_text))
        is_winsorized = int("winsorize(" in expr_text)
        is_balanced_trunc = int(float(setting.get("truncation") or 0.0) in {0.08, 0.1})
        is_tight_trunc = int(float(setting.get("truncation") or 0.0) <= 0.03)
        is_subindustry = int(str(setting.get("neutralization") or "").upper() == "SUBINDUSTRY")
        if "CONCENTRATED_WEIGHT" in failed:
            return (is_backfilled, is_group_spread, is_winsorized, is_balanced_trunc, is_tight_trunc, is_subindustry)
        return (0, 0, 0, 0, is_tight_trunc, is_subindustry)

    rows.sort(key=combo_priority, reverse=True)
    return rows[:max_variants]


def load_lifecycle() -> dict[str, Any]:
    if not LIFECYCLE_PATH.exists():
        return {"schema_version": 1, "families": {}}
    try:
        payload = json.loads(LIFECYCLE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"schema_version": 1, "families": {}}
    if not isinstance(payload, dict):
        return {"schema_version": 1, "families": {}}
    payload.setdefault("schema_version", 1)
    payload.setdefault("families", {})
    return payload


def save_lifecycle(payload: dict[str, Any]) -> None:
    payload["updated_at"] = utc_now()
    try:
        LIFECYCLE_PATH.parent.mkdir(parents=True, exist_ok=True)
        LIFECYCLE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        payload["path"] = str(LIFECYCLE_PATH.relative_to(ROOT_DIR))
    except PermissionError:
        FALLBACK_LIFECYCLE_PATH.parent.mkdir(parents=True, exist_ok=True)
        FALLBACK_LIFECYCLE_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        payload["path"] = str(FALLBACK_LIFECYCLE_PATH.relative_to(ROOT_DIR))


def load_repair_family_actions() -> dict[str, dict[str, Any]]:
    if not REPAIR_FAMILY_ACTIONS_PATH.exists():
        return {}
    try:
        payload = json.loads(REPAIR_FAMILY_ACTIONS_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}
    if not isinstance(payload, dict):
        return {}
    actions = payload.get("actions")
    if isinstance(actions, list):
        return {
            str(row.get("family_tag") or "").upper(): row
            for row in actions
            if isinstance(row, dict) and str(row.get("family_tag") or "").strip()
        }
    if isinstance(actions, dict):
        return {str(key).upper(): value for key, value in actions.items() if isinstance(value, dict)}
    return {}


def update_lifecycle(max_variants_per_family: int = 36) -> dict[str, Any]:
    lifecycle = load_lifecycle()
    families = lifecycle.setdefault("families", {})
    repair_family_actions = load_repair_family_actions()
    payloads = list(iter_all_result_payloads())
    family_payloads: dict[str, list[dict[str, Any]]] = {}
    for payload in payloads:
        expression = str(payload.get("expression") or "")
        if not expression:
            continue
        family_payloads.setdefault(repair_family_tag(payload), []).append(payload)

    for family_tag, rows in family_payloads.items():
        repair_waiting = [payload for payload in rows if is_high_grade_failed(payload) and not is_repair_result(payload)]
        if not repair_waiting and family_tag not in families:
            continue
        entry = families.setdefault(
            family_tag,
            {
                "family_tag": family_tag,
                "created_at": utc_now(),
                "status": "pending_repair",
                "attempted_variants": [],
                "terminal_reasons": [],
            },
        )
        submit_eligible = [payload for payload in rows if is_submit_eligible(payload)]
        repair_results = [payload for payload in rows if is_repair_result(payload)]
        attempted = list(entry.get("attempted_variants") or [])
        for payload in repair_results:
            alpha_id = str(payload.get("alpha_id") or "")
            if alpha_id and alpha_id not in attempted:
                attempted.append(alpha_id)
        # Keep the full attempted ledger. Truncating this list caused lifecycle
        # accounting and user-visible 1REPAIR state to diverge from reality.
        entry["attempted_variants"] = attempted
        entry["waiting_source_count"] = len(repair_waiting)
        entry["repair_result_count"] = len(repair_results)
        entry["submit_eligible_count"] = len(submit_eligible)
        entry["last_seen_at"] = utc_now()
        if submit_eligible:
            entry["status"] = "ready_to_submit_best"
            entry["best_alpha_id"] = str(submit_eligible[0].get("alpha_id") or "")
            entry["terminal_reasons"] = []
        elif (
            len(attempted) >= max_variants_per_family
            and repair_waiting
            and len(attempted) >= max(72, len(repair_waiting) * 6)
        ):
            entry["status"] = "exhausted"
            entry["terminal_reasons"] = ["bounded_repair_budget_exhausted"]
        elif repair_results:
            entry["status"] = "repair_running"
            entry["terminal_reasons"] = []
        elif repair_waiting:
            entry["status"] = "pending_repair"

    for family_tag, action in repair_family_actions.items():
        decision = str(action.get("decision") or "").lower()
        if decision not in {"retire", "exhausted", "terminal"}:
            continue
        entry = families.setdefault(
            family_tag,
            {
                "family_tag": family_tag,
                "created_at": utc_now(),
                "attempted_variants": [],
            },
        )
        entry["status"] = "exhausted"
        entry["terminal_reasons"] = list(action.get("terminal_reasons") or [str(action.get("reason") or "repair_family_terminal")])
        entry["repair_family_action"] = action
        entry["last_seen_at"] = utc_now()
    lifecycle["family_count"] = len(families)
    lifecycle["status_counts"] = dict(Counter(str(row.get("status") or "unknown") for row in families.values()))
    sources = lifecycle.setdefault("sources", {})
    repair_results_by_source: dict[str, list[dict[str, Any]]] = {}
    for payload in payloads:
        if not is_repair_result(payload):
            continue
        source_slug = repair_result_source_slug(payload)
        if source_slug:
            repair_results_by_source.setdefault(source_slug, []).append(payload)

    source_status_counts: Counter[str] = Counter()
    for payload in payloads:
        if not is_high_grade_failed(payload) or is_repair_result(payload):
            continue
        source_id = alpha_id_of(payload)
        if not source_id:
            continue
        family_entry = families.get(repair_family_tag(payload)) or {}
        family_status = str(family_entry.get("status") or "")
        source_slug = repair_source_slug(payload)
        source_results = repair_results_by_source.get(source_slug, [])
        source_entry = sources.setdefault(
            source_id,
            {
                "source_alpha_id": source_id,
                "created_at": utc_now(),
                "family_tag": repair_family_tag(payload),
            },
        )
        source_entry["family_tag"] = repair_family_tag(payload)
        source_entry["grade"] = str((payload.get("alpha_details") or {}).get("grade") or payload.get("grade") or "")
        source_entry["failed_checks"] = failed_check_names(payload)
        source_entry["repair_result_count"] = len(source_results)
        source_entry["last_seen_at"] = utc_now()
        eligible = [row for row in source_results if is_submit_eligible(row)]
        if family_status in {"correlation_terminal", "exhausted", "ready_to_submit_best"}:
            source_entry["status"] = family_status
            source_entry["terminal_reasons"] = list(family_entry.get("terminal_reasons") or [f"family_{family_status}"])
        elif eligible:
            source_entry["status"] = "ready_to_submit_best"
            source_entry["best_alpha_id"] = alpha_id_of(eligible[0])
            source_entry["terminal_reasons"] = []
        elif len(source_results) >= max_variants_per_family:
            source_entry["status"] = "exhausted"
            source_entry["terminal_reasons"] = ["source_repair_budget_exhausted"]
        elif source_results:
            source_entry["status"] = "repair_running"
            source_entry["terminal_reasons"] = []
        else:
            source_entry["status"] = "pending_repair"
            source_entry["terminal_reasons"] = []
        source_status_counts[str(source_entry.get("status") or "unknown")] += 1
    lifecycle["source_count"] = len(sources)
    lifecycle["source_status_counts"] = dict(source_status_counts)
    save_lifecycle(lifecycle)
    return lifecycle


def repair_jobs(
    max_sources: int = 12,
    max_variants_per_source: int = 6,
    max_jobs: int = 36,
) -> tuple[list[dict[str, Any]], list[str], dict[str, Any]]:
    lifecycle = update_lifecycle(max_variants_per_family=max(12, max_sources * max_variants_per_source))
    terminal_families = {
        family
        for family, row in (lifecycle.get("families") or {}).items()
        if str(row.get("status") or "") in {"correlation_terminal", "exhausted", "ready_to_submit_best"}
    }
    source_lifecycle = lifecycle.get("sources") or {}
    terminal_sources = {
        source_id
        for source_id, row in source_lifecycle.items()
        if str(row.get("status") or "") in {"correlation_terminal", "exhausted", "ready_to_submit_best"}
    }
    payloads = list(iter_all_result_payloads())
    candidates = [
        payload
        for payload in payloads
        if is_high_grade_failed(payload)
        and not is_repair_result(payload)
        and alpha_id_of(payload) not in terminal_sources
        and repair_family_tag(payload) not in terminal_families
    ]
    candidates.sort(key=repair_priority, reverse=True)
    selected_sources = candidates[:max_sources]
    jobs: list[dict[str, Any]] = []
    templates: list[str] = []
    source_summaries: list[dict[str, Any]] = []
    seen_hashes: set[str] = set()
    tested_fingerprints = load_tested_fingerprints()
    stale_sources: list[dict[str, Any]] = []

    for source in selected_sources:
        if len(jobs) >= max_jobs:
            break
        source_id = str(source.get("alpha_id") or ((source.get("alpha_details") or {}).get("id")) or "alpha")
        base_expression = str(source.get("expression") or "").strip()
        base_settings = copy.deepcopy(source.get("settings") or {})
        failed = set(failed_check_names(source))
        made_for_source = 0
        skipped_tested = 0
        skipped_duplicate = 0
        skipped_invalid = 0
        for expr_label, expr, setting_label, settings in repair_combinations(
            base_expression,
            base_settings,
            failed,
            max_variants=max_variants_per_source * 4,
        ):
            if made_for_source >= max_variants_per_source or len(jobs) >= max_jobs:
                break
            signature = json.dumps({"expression": expr, "settings": settings}, sort_keys=True, ensure_ascii=False)
            expr_hash = template_hash(signature)
            if expr_hash in seen_hashes:
                skipped_duplicate += 1
                continue
            fingerprint = repair_fingerprint(expr, settings)
            if fingerprint in tested_fingerprints:
                skipped_tested += 1
                continue
            name = slugify(f"repair_{source_id}_{expr_label}_{setting_label}", limit=120)
            template = build_template(source, name, expr, settings, labels=[expr_label, setting_label])
            validation = validate_template_payload(Path("<high_grade_repair>"), template)
            if not validation.valid:
                skipped_invalid += 1
                continue
            seen_hashes.add(expr_hash)
            relative_template = Path("result_store") / "supply" / "templates" / "high_grade_repair" / f"{name}.yaml"
            output_path = ROOT_DIR / relative_template
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(yaml.safe_dump(template, sort_keys=False, allow_unicode=True), encoding="utf-8")
            templates.append(str(relative_template).replace("\\", "/"))
            job = {
                "name": name,
                "inventory_name": f"repair_{source_id}",
                "template": str(relative_template).replace("\\", "/"),
                "refresh_inventory": False,
                "supply_mode": "exploit",
                "category": str(template.get("category") or "FUNDAMENTAL").lower(),
                "limit": 1,
                "selection_limit": 1,
                "inventory_fetch_limit": 1,
                "max_workers": 1,
                "top": 1,
                "check_retries": 0,
                "fast_local_only": True,
                "fixed_expression_template": True,
                "source_alpha_id": source_id,
                "source_expression_hash": template_hash(base_expression),
                "source_effective_pass_count": pass_count(source),
                "source_failed_checks": sorted(failed),
                "source_grade": str((source.get("alpha_details") or {}).get("grade") or source.get("grade") or ""),
                "repair_family_tag": repair_family_tag(source),
                "repair_engine": "high_grade_failed_repair",
                "repair_labels": [expr_label, setting_label],
                "supply_priority": repair_priority(source) + 1.0 - made_for_source * 0.08,
                "tags": list(template.get("tags") or []),
            }
            jobs.append(job)
            made_for_source += 1
        if made_for_source:
            source_summaries.append(
                {
                    "alpha_id": source_id,
                    "grade": (source.get("alpha_details") or {}).get("grade") or source.get("grade"),
                    "failed_checks": sorted(failed),
                    "pass_count": pass_count(source),
                    "repair_variants": made_for_source,
                    "skipped_tested": skipped_tested,
                    "skipped_duplicate": skipped_duplicate,
                    "skipped_invalid": skipped_invalid,
                    "priority": repair_priority(source),
                    "family_tag": repair_family_tag(source),
                }
            )
        else:
            stale_sources.append(
                {
                    "alpha_id": source_id,
                    "grade": (source.get("alpha_details") or {}).get("grade") or source.get("grade"),
                    "failed_checks": sorted(failed),
                    "pass_count": pass_count(source),
                    "priority": repair_priority(source),
                    "family_tag": repair_family_tag(source),
                    "reason": "no_fresh_repair_candidate_after_dedupe",
                    "skipped_tested": skipped_tested,
                    "skipped_duplicate": skipped_duplicate,
                    "skipped_invalid": skipped_invalid,
                }
            )

    summary = {
        "schema_version": 1,
        "generated_at": utc_now(),
        "candidate_count": len(candidates),
        "selected_source_count": len(source_summaries),
        "job_count": len(jobs),
        "template_count": len(templates),
        "failed_check_counts": dict(Counter(name for source in selected_sources for name in failed_check_names(source)).most_common()),
        "sources": source_summaries,
        "stale_sources": stale_sources,
        "stale_source_count": len(stale_sources),
        "tested_fingerprint_count": len(tested_fingerprints),
        "repair_tag": REPAIR_TAG,
        "family_tag_prefix": REPAIR_FAMILY_TAG_PREFIX,
        "lifecycle": {
            "path": lifecycle.get("path") or str(LIFECYCLE_PATH.relative_to(ROOT_DIR)),
            "status_counts": lifecycle.get("status_counts") or {},
            "terminal_family_count": len(terminal_families),
        },
    }
    try:
        REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        REPORT_PATH.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        summary["report_path"] = str(REPORT_PATH.relative_to(ROOT_DIR))
    except PermissionError:
        FALLBACK_REPORT_PATH.parent.mkdir(parents=True, exist_ok=True)
        FALLBACK_REPORT_PATH.write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
        summary["report_path"] = str(FALLBACK_REPORT_PATH.relative_to(ROOT_DIR))
    return jobs, templates, summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Build conservative repair jobs for high-grade failed alphas.")
    parser.add_argument("--max-sources", type=int, default=12)
    parser.add_argument("--max-variants-per-source", type=int, default=6)
    parser.add_argument("--max-jobs", type=int, default=36)
    args = parser.parse_args()
    jobs, templates, summary = repair_jobs(
        max_sources=args.max_sources,
        max_variants_per_source=args.max_variants_per_source,
        max_jobs=args.max_jobs,
    )
    print(json.dumps({**summary, "jobs": [job.get("name") for job in jobs], "templates": templates}, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
