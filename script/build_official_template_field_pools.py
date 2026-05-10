#!/usr/bin/env python
"""Build curated field pools for the official template library."""

from __future__ import annotations

import json
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
RAW_DIR = ROOT_DIR / "crawler" / "datafields" / "raw"
CURATED_DIR = ROOT_DIR / "crawler" / "datafields" / "curated"


ANALYST_PATH = RAW_DIR / "analyst_120_analyst_usa_1_top3000.json"
FUNDAMENTAL_PATH = RAW_DIR / "fundamental_120_fundamental_usa_1_top3000.json"
MODEL_PATH = RAW_DIR / "model_80_model_usa_1_top3000.json"
SENTIMENT_PATH = RAW_DIR / "sentiment_80_sentiment_usa_1_top3000.json"


def load_rows(path: Path) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        rows = payload.get("results") or []
        return rows if isinstance(rows, list) else []
    return payload if isinstance(payload, list) else []


def save_pool(stem: str, rows: list[dict], note: str) -> None:
    CURATED_DIR.mkdir(parents=True, exist_ok=True)
    txt_path = CURATED_DIR / f"{stem}.txt"
    json_path = CURATED_DIR / f"{stem}.json"

    txt_path.write_text(
        "\n".join(row["id"] for row in rows) + "\n",
        encoding="utf-8",
    )
    payload = {
        "name": stem,
        "note": note,
        "count": len(rows),
        "fields": rows,
    }
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def select_by_ids(rows: list[dict], ids: list[str]) -> list[dict]:
    lookup = {row["id"]: row for row in rows}
    selected = []
    for field_id in ids:
        if field_id in lookup:
            selected.append(lookup[field_id])
    return selected


def slim(row: dict) -> dict:
    return {
        "id": row["id"],
        "description": row.get("description"),
        "dataset": row.get("dataset", {}).get("id"),
        "category": row.get("category", {}).get("id"),
        "subcategory": row.get("subcategory", {}).get("id"),
        "type": row.get("type"),
        "coverage": row.get("coverage"),
        "alphaCount": row.get("alphaCount"),
        "userCount": row.get("userCount"),
    }


def main() -> None:
    analyst_rows = load_rows(ANALYST_PATH)
    fundamental_rows = load_rows(FUNDAMENTAL_PATH)
    model_rows = load_rows(MODEL_PATH)
    sentiment_rows = load_rows(SENTIMENT_PATH)

    ratio_close_ids = [
        "actual_dividend_value_quarterly",
        "actual_cashflow_per_share_value_quarterly",
        "actual_eps_value_quarterly",
        "actual_sales_value_annual",
        "actual_sales_value_quarterly",
        "anl4_af_cfps_value",
        "anl4_af_div_value",
        "anl4_af_eps_value",
        "anl4_afv4_cfps_high",
        "anl4_afv4_cfps_low",
        "anl4_afv4_cfps_mean",
        "anl4_afv4_cfps_median",
        "anl4_afv4_div_high",
        "anl4_afv4_div_low",
        "anl4_afv4_div_mean",
        "anl4_afv4_div_median",
        "anl4_afv4_eps_high",
        "anl4_afv4_eps_low",
    ]
    ts_rank_ids = [
        "current_ratio",
        "employee",
        "fn_accum_depr_depletion_and_amortization_ppne_a",
        "fn_accum_oth_income_loss_net_of_tax_a",
        "fn_allocated_share_based_compensation_expense_a",
        "fn_allocated_share_based_compensation_expense_q",
        "fn_allowance_for_doubtful_accounts_receivable_a",
        "fn_amortization_of_intangible_assets_a",
        "fn_assets_fair_val_a",
        "fn_assets_fair_val_l1_a",
        "fn_assets_fair_val_l2_a",
        "fn_assets_fair_val_l3_a",
        "fscore_bfl_growth",
        "fscore_bfl_momentum",
        "fscore_bfl_profitability",
        "fscore_bfl_quality",
        "fscore_bfl_surface",
        "fscore_bfl_total",
        "fscore_bfl_value",
        "relative_valuation_rank_derivative",
    ]
    neg_ts_rank_ids = [
        "debt",
        "debt_lt",
        "debt_st",
        "fn_accrued_liab_a",
        "fn_accrued_liab_curr_a",
        "fn_accrued_liab_curr_q",
        "fn_accrued_liab_q",
        "fn_allowance_for_doubtful_accounts_receivable_a",
        "fn_allowance_for_doubtful_accounts_receivable_q",
        "fn_amortization_of_intangible_assets_a",
        "fn_amortization_of_intangible_assets_q",
        "fn_assets_fair_val_a",
        "fn_assets_fair_val_l1_a",
        "fn_assets_fair_val_l2_a",
        "fn_assets_fair_val_l3_a",
        "systematic_risk_last_30_days",
        "systematic_risk_last_60_days",
        "unsystematic_risk_last_30_days",
    ]
    stability_ids = [
        "snt1_d1_dynamicfocusrank",
        "snt1_d1_fundamentalfocusrank",
        "snt1_d1_stockrank",
        "snt1_d1_earningsrevision",
        "snt1_d1_earningssurprise",
        "snt1_d1_earningstorpedo",
        "snt1_d1_netearningsrevision",
        "snt1_d1_longtermepsgrowthest",
        "snt1_d1_dtstsespe",
        "analyst_revision_rank_derivative",
        "cashflow_efficiency_rank_derivative",
        "earnings_certainty_rank_derivative",
        "growth_potential_rank_derivative",
        "multi_factor_acceleration_score_derivative",
        "multi_factor_static_score_derivative",
        "relative_valuation_rank_derivative",
    ]

    ratio_rows = [slim(row) for row in select_by_ids(analyst_rows, ratio_close_ids)]
    ts_rank_rows = [
        slim(row)
        for row in (
            select_by_ids(fundamental_rows, ts_rank_ids[:12]) +
            select_by_ids(model_rows, ts_rank_ids[12:])
        )
    ]
    neg_rows = [
        slim(row)
        for row in (
            select_by_ids(fundamental_rows, neg_ts_rank_ids[:16]) +
            select_by_ids(model_rows, neg_ts_rank_ids[16:])
        )
    ]
    stability_rows = [
        slim(row)
        for row in (
            select_by_ids(sentiment_rows, stability_ids[:9]) +
            select_by_ids(model_rows, stability_ids[9:])
        )
    ]

    save_pool(
        "official_ratio_close_candidates",
        ratio_rows,
        "适配 official_group_rank_ratio_close_60_template 的候选字段。优先选可与价格构成估值/收益率含义的 analyst 矩阵字段。",
    )
    save_pool(
        "official_ts_rank_candidates",
        ts_rank_rows,
        "适配 official_ts_rank_252_template 的候选字段。以基本面慢变量和低拥挤模型分数字段为主。",
    )
    save_pool(
        "official_neg_ts_rank_candidates",
        neg_rows,
        "适配 official_neg_ts_rank_126_template 的候选字段。以负债、风险、资产公允价值和风险暴露类字段为主。",
    )
    save_pool(
        "official_stability_candidates",
        stability_rows,
        "适配 official_neg_ts_std_dev_10_template 的候选字段。以短期情绪稳定性和模型导数稳定性字段为主。",
    )

    print("Saved curated official template field pools:")
    for stem, rows in [
        ("official_ratio_close_candidates", ratio_rows),
        ("official_ts_rank_candidates", ts_rank_rows),
        ("official_neg_ts_rank_candidates", neg_rows),
        ("official_stability_candidates", stability_rows),
    ]:
        print(f"- {stem}: {len(rows)} fields")


if __name__ == "__main__":
    main()
