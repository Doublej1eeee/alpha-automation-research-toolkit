#!/usr/bin/env python
"""Shared helpers for batch alpha submission scripts."""

from __future__ import annotations

import csv
import json
from datetime import datetime, timezone
from pathlib import Path

from brain_client import (
    RESULTS_DIR,
    RESULTS_INDEX_FINGERPRINTS_FILE,
    build_alpha_fingerprint,
    build_default_settings,
    derive_auto_color,
    extract_effective_checks,
    format_color_label,
    get_check_buckets,
    load_alpha_config,
)


SUMMARIES_DIR = RESULTS_DIR / "summaries"
MANIFEST_DIR = RESULTS_DIR / "loop_manifests"


def utc_now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def display_path(path: Path, root_dir: Path) -> str:
    resolved = path.resolve()
    try:
        return str(resolved.relative_to(root_dir))
    except ValueError:
        return str(resolved)


def load_tested_fingerprints() -> set[str]:
    fingerprints: set[str] = set()
    if RESULTS_INDEX_FINGERPRINTS_FILE.exists():
        with RESULTS_INDEX_FINGERPRINTS_FILE.open("r", encoding="utf-8") as file:
            for line in file:
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except Exception:
                    continue
                fingerprint = payload.get("fingerprint")
                if fingerprint:
                    fingerprints.add(fingerprint)
        if fingerprints:
            return fingerprints

    setting_keys = set(build_default_settings().keys())
    for result_file in RESULTS_DIR.glob("*.json"):
        try:
            payload = json.loads(result_file.read_text(encoding="utf-8"))
        except Exception:
            continue
        fingerprint = payload.get("fingerprint")
        if fingerprint:
            fingerprints.add(fingerprint)
            continue
        alpha_details = payload.get("alpha_details") or {}
        details_settings = alpha_details.get("settings")
        details_expression = ((alpha_details.get("regular") or {}).get("code"))
        details_type = alpha_details.get("type") or "REGULAR"
        if details_expression is not None and details_settings is not None:
            filtered_settings = {
                key: value
                for key, value in details_settings.items()
                if key in setting_keys
            }
            legacy = json.dumps(
                {
                    "type": details_type,
                    "settings": filtered_settings,
                    "expression": details_expression,
                },
                sort_keys=True,
                ensure_ascii=False,
            )
            fingerprints.add(legacy)
    return fingerprints


def load_existing_rows(stem: str) -> list[dict]:
    summary_path = SUMMARIES_DIR / f"{stem}.json"
    if not summary_path.exists():
        return []
    try:
        payload = json.loads(summary_path.read_text(encoding="utf-8"))
    except Exception:
        return []
    return payload if isinstance(payload, list) else []


def load_existing_attempted_fingerprints(stem: str) -> set[str]:
    fingerprints: set[str] = set()
    for row in load_existing_rows(stem):
        fingerprint = row.get("fingerprint")
        if fingerprint:
            fingerprints.add(fingerprint)
    return fingerprints


def skip_tested_alpha_files(
    alpha_files: list[Path],
    rerun: bool,
    attempted_fingerprints: set[str] | None = None,
) -> tuple[list[Path], list[Path]]:
    if rerun and not attempted_fingerprints:
        return alpha_files, []

    tested = set() if rerun else load_tested_fingerprints()
    attempted = attempted_fingerprints or set()
    fresh: list[Path] = []
    skipped: list[Path] = []

    for alpha_file in alpha_files:
        try:
            config = load_alpha_config(alpha_file)
            fingerprint = build_alpha_fingerprint(config)
        except Exception:
            fresh.append(alpha_file)
            continue

        if fingerprint in tested or fingerprint in attempted:
            skipped.append(alpha_file)
        else:
            fresh.append(alpha_file)

    return fresh, skipped


def build_row(item: dict) -> dict:
    if item.get("error"):
        return {
            "name": item["name"],
            "file": item.get("file"),
            "field": item.get("field"),
            "fingerprint": item.get("fingerprint"),
            "alpha_id": None,
            "rule_color": "ERROR",
            "grade": "ERROR",
            "status": "ERROR",
            "stage": None,
            "sharpe": None,
            "fitness": None,
            "returns": None,
            "turnover": None,
            "drawdown": None,
            "margin": None,
            "passed_checks": 0,
            "failed_checks": 0,
            "pending_checks": 0,
            "failed_check_names": "",
            "pending_check_names": "",
            "effective_check_count": 0,
            "effective_check_source": None,
            "result_path": None,
            "experience_path": None,
            "error": item["error"],
        }

    details = item["alpha_details"]
    is_block = details.get("is") or {}
    passed, failed, pending = get_check_buckets(details)
    effective_checks = extract_effective_checks(details)
    return {
        "name": item["name"],
        "file": item.get("file"),
        "field": item.get("field"),
        "fingerprint": item.get("fingerprint"),
        "alpha_id": item["alpha_id"],
        "rule_color": format_color_label(derive_auto_color(details)),
        "grade": details.get("grade"),
        "status": details.get("status"),
        "stage": details.get("stage"),
        "sharpe": is_block.get("sharpe"),
        "fitness": is_block.get("fitness"),
        "returns": is_block.get("returns"),
        "turnover": is_block.get("turnover"),
        "drawdown": is_block.get("drawdown"),
        "margin": is_block.get("margin"),
        "passed_checks": len(passed),
        "failed_checks": len(failed),
        "pending_checks": len(pending),
        "failed_check_names": ",".join(check.get("name", "") for check in failed),
        "pending_check_names": ",".join(check.get("name", "") for check in pending),
        "effective_check_source": (
            "submit_preview"
            if ((details.get("submitPreview") or {}).get("is") or {}).get("checks")
            else "details"
        ),
        "effective_check_count": len(effective_checks),
        "result_path": item.get("result_path"),
        "experience_path": item.get("experience_path"),
        "error": "",
    }


def save_summary_rows(rows: list[dict], stem: str) -> tuple[Path, Path]:
    SUMMARIES_DIR.mkdir(parents=True, exist_ok=True)
    json_path = SUMMARIES_DIR / f"{stem}.json"
    csv_path = SUMMARIES_DIR / f"{stem}.csv"

    json_path.write_text(
        json.dumps(rows, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    if rows:
        fieldnames = sorted({key for row in rows for key in row.keys()})
        with csv_path.open("w", newline="", encoding="utf-8-sig") as f:
            writer = csv.DictWriter(f, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(rows)
    else:
        csv_path.write_text("", encoding="utf-8")

    return json_path, csv_path


def save_outputs(rows: list[dict], manifest: dict, stem: str) -> tuple[Path, Path, Path]:
    MANIFEST_DIR.mkdir(parents=True, exist_ok=True)
    manifest_path = MANIFEST_DIR / f"{stem}.json"
    json_path, csv_path = save_summary_rows(rows, stem)
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return manifest_path, json_path, csv_path


def update_manifest_progress(
    manifest: dict,
    rows: list[dict],
    total_records: int,
    skipped_records: int,
    queued_records: int,
    finished: bool,
) -> dict:
    completed = len(rows)
    error_count = sum(1 for row in rows if row.get("status") == "ERROR")
    pending_check_count = sum(1 for row in rows if (row.get("pending_checks") or 0) > 0)
    failed_check_count = sum(1 for row in rows if (row.get("failed_checks") or 0) > 0)
    passed_clean_count = sum(
        1
        for row in rows
        if row.get("status") != "ERROR"
        and (row.get("failed_checks") or 0) == 0
        and (row.get("pending_checks") or 0) == 0
    )
    manifest["progress"] = {
        "updated_at": utc_now(),
        "total_records": total_records,
        "skipped_records": skipped_records,
        "queued_records": queued_records,
        "completed_records": completed,
        "remaining_records": max(queued_records - completed, 0),
        "error_records": error_count,
        "pending_check_records": pending_check_count,
        "clean_pass_records": passed_clean_count,
        "failed_check_records": failed_check_count,
        "status": "completed" if finished else "running",
    }
    return manifest


def print_leaderboard(rows: list[dict], top_n: int) -> None:
    ranked = sorted(
        rows,
        key=lambda row: (
            1 if row["status"] == "ERROR" else 0,
            row["failed_checks"],
            row["pending_checks"],
            -(row["fitness"] or -999),
            -(row["sharpe"] or -999),
        ),
    )
    print("=" * 60)
    print("Leaderboard")
    for idx, row in enumerate(ranked[:top_n], start=1):
        if row["status"] == "ERROR":
            print(f"{idx}. {row['name']} | ERROR | {row['error']}")
            continue
        target = row.get("field") or row.get("file")
        print(
            f"{idx}. {row['name']} | Target={target} | "
            f"Color={row.get('rule_color')} | Sharpe={row['sharpe']} | "
            f"Fitness={row['fitness']} | Returns={row['returns']} | "
            f"Turnover={row['turnover']} | Failed={row['failed_checks']} | "
            f"Pending={row['pending_checks']} | AlphaID={row['alpha_id']}"
        )
