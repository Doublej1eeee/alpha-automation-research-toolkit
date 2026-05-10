"""Mobile-first runtime dashboard for the cloud alpha miner.

The dashboard intentionally uses only the Python standard library so it can run
on the cloud host without changing the mining environment.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import argparse
import base64
import html
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any
from urllib.parse import parse_qs, urlparse


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from brain_client import build_alpha_fingerprint, derive_auto_grade_tag, extract_effective_checks  # noqa: E402
from script.batch_runtime import load_tested_fingerprints  # noqa: E402
from script.cache_platform_truth import build_cache as build_platform_truth_cache  # noqa: E402
from script.submit_template_loop import extract_field_slots, load_template, replace_field_map  # noqa: E402
STATE_PATH = ROOT_DIR / "result_store" / "slot_miner" / "cloud_slot_miner.json"
SUPPLY_PATH = ROOT_DIR / "result_store" / "supply" / "supply_jobs.json"
REPAIR_REPORT_PATH = ROOT_DIR / "result_store" / "analysis" / "high_grade_repair_report.json"
REPAIR_LIFECYCLE_PATH = ROOT_DIR / "result_store" / "analysis" / "high_grade_repair_lifecycle.json"
DAILY_SNAPSHOT_PATH = ROOT_DIR / "result_store" / "analysis" / "dashboard_daily_snapshots.json"
PLATFORM_TRUTH_PATH = ROOT_DIR / "result_store" / "analysis" / "platform_truth_cache.json"
ALPHA_TRUTH_STATE_PATH = ROOT_DIR / "result_store" / "analysis" / "alpha_truth_state.json"
INVENTORY_DIR = ROOT_DIR / "result_store" / "inventories"
BATCH_DIR = ROOT_DIR / "result_store" / "batches"
RAW_ALPHA_PATH = ROOT_DIR / "alpha_generation" / "raw_alpha_ai.md"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def read_json(path: Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def parse_dt(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        text = str(value).replace("Z", "+00:00")
        dt = datetime.fromisoformat(text)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(timezone.utc)
    except Exception:
        return None


def read_service_lines(minutes: int = 90, limit: int = 500) -> list[str]:
    since = f"{max(1, int(minutes))} minutes ago"
    cmd = [
        "journalctl",
        "-u",
        "learning-continuous",
        "--since",
        since,
        "--no-pager",
        "-o",
        "short-iso",
    ]
    try:
        result = subprocess.run(cmd, cwd=ROOT_DIR, text=True, capture_output=True, timeout=8, check=False)
    except Exception:
        return []
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    return lines[-limit:]


def read_slot_event_lines(hours: int = 24, limit: int = 8000) -> list[str]:
    since = f"{max(1, int(hours))} hours ago"
    cmd = [
        "journalctl",
        "-u",
        "learning-continuous",
        "--since",
        since,
        "--no-pager",
        "-o",
        "short-iso",
    ]
    try:
        result = subprocess.run(cmd, cwd=ROOT_DIR, text=True, capture_output=True, timeout=12, check=False)
    except Exception:
        return []
    lines = [
        line
        for line in result.stdout.splitlines()
        if "[SLOT DONE]" in line or "[SLOT ERROR]" in line
    ]
    return lines[-limit:]


def parse_journal_dt(line: str) -> datetime | None:
    text = line.split(" ", 1)[0].strip()
    try:
        return datetime.fromisoformat(text).astimezone(timezone.utc)
    except Exception:
        return None


def slot_events_from_journal(lines: list[str]) -> list[dict[str, Any]]:
    events: list[dict[str, Any]] = []
    for line in lines:
        finished_at = parse_journal_dt(line)
        if not finished_at:
            continue
        marker = "[SLOT DONE]" if "[SLOT DONE]" in line else "[SLOT ERROR]"
        payload = line.split(marker, 1)[1].strip()
        job_name = payload.split(" | ", 1)[0].strip()
        events.append(
            {
                "finished_at": finished_at,
                "status": "completed" if marker == "[SLOT DONE]" else "error",
                "job_name": job_name,
                "is_repair": job_name.startswith("repair_"),
                "is_raw": job_name.startswith("raw_seed_") or "raw_seed_" in job_name,
            }
        )
    return events


def parse_running_channels(lines: list[str]) -> list[dict[str, Any]]:
    starts: dict[str, dict[str, Any]] = {}
    done_names: set[str] = set()
    for line in lines:
        if "[SLOT DONE]" in line or "[SLOT ERROR]" in line:
            name = line.split("] ", 1)[-1].split(" | ", 1)[0].strip()
            if name:
                done_names.add(name)
            continue
        if "[SLOT START]" not in line:
            continue
        payload = line.split("[SLOT START]", 1)[1].strip()
        name = payload.split(" | ", 1)[0].strip()
        if not name:
            continue
        fields = {}
        for part in payload.split(" | ")[1:]:
            if "=" in part:
                key, value = part.split("=", 1)
                fields[key.strip()] = value.strip()
        starts[name] = {
            "name": name,
            "mode": fields.get("mode", "-"),
            "mechanism": fields.get("mechanism", "-"),
            "target_mechanism": fields.get("target_mechanism", "-"),
            "plan": fields.get("plan", "-"),
            "fields": fields.get("fields", "{}"),
            "is_repair": name.startswith("repair_") or "repair" in fields.get("mode", "").lower(),
        }
    active = [row for name, row in starts.items() if name not in done_names]
    return active[-8:]


def parse_latest_slot_status(lines: list[str]) -> dict[str, Any]:
    latest = ""
    for line in lines:
        if "Slot miner status:" in line:
            latest = line
    if not latest:
        return {}
    payload = latest.split("Slot miner status:", 1)[1]
    result: dict[str, Any] = {"raw": payload.strip()}
    for key, value in re.findall(r"([A-Za-z_]+)=([0-9]+(?:/[0-9]+)?|[A-Za-z0-9_.:-]+)", payload):
        if "/" in value:
            left, _, right = value.partition("/")
            if left.isdigit() and right.isdigit():
                result[key] = int(left)
                result[f"{key}_limit"] = int(right)
                continue
        if value.isdigit():
            result[key] = int(value)
        else:
            result[key] = value.strip()
    return result


def safe_int(value: Any, default: int = 0) -> int:
    if isinstance(value, int):
        return value
    text = str(value or "").strip()
    if re.fullmatch(r"\d+", text):
        return int(text)
    if re.fullmatch(r"\d+\.0+", text):
        return int(float(text))
    return default


def history_rows(state: dict[str, Any]) -> list[dict[str, Any]]:
    rows = state.get("history") or []
    return [row for row in rows if isinstance(row, dict)]


def bucket_stats(rows: list[dict[str, Any]], event_rows: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    now = utc_now()
    events = event_rows or []
    if not events:
        for row in rows:
            dt = parse_dt(row.get("finished_at"))
            if not dt:
                continue
            events.append(
                {
                    "finished_at": dt,
                    "status": "completed" if row.get("status") == "completed" else "error",
                    "job_name": str(row.get("job_name") or ""),
                    "is_repair": str(row.get("job_name") or "").startswith("repair_"),
                    "is_raw": bool(row.get("raw_alpha_family")),
                }
            )
    windows = {}
    for hours in [1, 3, 6, 12, 24]:
        cutoff = now - timedelta(hours=hours)
        selected = [row for row in events if row.get("finished_at") and row.get("finished_at") >= cutoff]
        completed = [row for row in selected if row.get("status") == "completed"]
        errors = [row for row in selected if row.get("status") == "error"]
        repair = [row for row in completed if row.get("is_repair")]
        raw = [row for row in completed if row.get("is_raw")]
        windows[f"{hours}h"] = {
            "completed": len(completed),
            "errors": len(errors),
            "per_hour": round(len(completed) / hours, 2),
            "repair_completed": len(repair),
            "raw_completed": len(raw),
        }

    hourly: list[dict[str, Any]] = []
    for offset in range(23, -1, -1):
        start = now.replace(minute=0, second=0, microsecond=0) - timedelta(hours=offset)
        end = start + timedelta(hours=1)
        rows_in_hour = [
            row
            for row in events
            if row.get("finished_at") and start <= row.get("finished_at") < end
        ]
        completed = [row for row in rows_in_hour if row.get("status") == "completed"]
        hourly.append(
            {
                "hour": start.astimezone().strftime("%m-%d %H:00"),
                "completed": len(completed),
                "errors": len([row for row in rows_in_hour if row.get("status") == "error"]),
                "repair": len([row for row in completed if row.get("is_repair")]),
                "raw": len([row for row in completed if row.get("is_raw")]),
            }
        )
    return {"windows": windows, "hourly": hourly}


def supply_jobs() -> list[dict[str, Any]]:
    payload = read_json(SUPPLY_PATH, {})
    if isinstance(payload, dict):
        jobs = payload.get("jobs") or []
    elif isinstance(payload, list):
        jobs = payload
    else:
        jobs = []
    return [job for job in jobs if isinstance(job, dict)]


def raw_pool_families() -> list[str]:
    if not RAW_ALPHA_PATH.exists():
        return []
    families: list[str] = []
    for line in RAW_ALPHA_PATH.read_text(encoding="utf-8", errors="ignore").splitlines():
        stripped = line.strip()
        if stripped.startswith("ALPHA:"):
            match = re.search(r"\bfamily=([^|]+)", stripped)
            if match:
                families.append(match.group(1).strip())
            continue
        if stripped.startswith("## "):
            families.append(stripped.strip("# ").strip())
        elif stripped.startswith("- id:") or stripped.startswith("id:"):
            families.append(stripped.split(":", 1)[1].strip())
    seen: set[str] = set()
    result = []
    for family in families:
        key = family.lower()
        if key and key not in seen:
            seen.add(key)
            result.append(family)
    return result


def archived_raw_families() -> list[str]:
    payload = read_json(ROOT_DIR / "result_store" / "analysis" / "raw_family_archive.json", {})
    used = payload.get("used_families") or []
    return [str(item) for item in used if str(item).strip()]


def batch_family_counts() -> dict[str, int]:
    counts: Counter[str] = Counter()
    if not BATCH_DIR.exists():
        return {}
    for path in BATCH_DIR.glob("*.jsonl"):
        text = path.name
        if "raw_seed_" in text:
            family = text.split("raw_seed_", 1)[1].split("_lane", 1)[0].replace(".jsonl", "")
            counts[family] += sum(1 for _ in path.open("r", encoding="utf-8", errors="ignore"))
    return dict(counts)


def inventory_text_path(name: str) -> Path:
    return INVENTORY_DIR / f"{name}.txt"


def load_inventory_field_list(name: str) -> list[str]:
    path = inventory_text_path(name)
    if not path.exists():
        return []
    try:
        return [line.strip() for line in path.read_text(encoding="utf-8", errors="ignore").splitlines() if line.strip()]
    except Exception:
        return []


def resolve_template_path(path_text: str) -> Path:
    path = Path(path_text)
    if not path.exists():
        path = ROOT_DIR / path_text
    return path


def inventory_name_for_job(job: dict[str, Any]) -> str:
    template = job.get("template")
    return str(job.get("inventory_name") or job.get("name") or Path(str(template)).stem)


def fields_for_slot(job: dict[str, Any], slot: str) -> list[str]:
    slot_specs = job.get("slot_inventories") or {}
    if isinstance(slot_specs, dict):
        spec = slot_specs.get(slot) or {}
        if isinstance(spec, dict):
            name = str(spec.get("inventory_name") or "")
            if name:
                return load_inventory_field_list(name)
    return load_inventory_field_list(inventory_name_for_job(job))


def iter_sample_field_maps(job: dict[str, Any], limit: int = 120) -> tuple[list[dict[str, str]], int]:
    template_path = resolve_template_path(str(job.get("template") or ""))
    if not template_path.exists():
        return [], 0
    try:
        template = load_template(template_path)
        slots = extract_field_slots(template)
    except Exception:
        return [], 0
    if "FIELD" in slots and any(slot.startswith("FIELD_") for slot in slots):
        slots = [slot for slot in slots if slot != "FIELD"]
    if not slots:
        return [{}], 1
    job_cap = int(job.get("estimated_field_map_cap") or job.get("max_pair_field_maps") or 0)
    slot_fields = {slot: fields_for_slot(job, slot) for slot in slots}
    if not all(slot_fields.values()):
        return [], 0
    if len(slots) == 1:
        fields = slot_fields[slots[0]]
        trimmed = fields[: min(len(fields), 40)]
        estimated = len(fields)
        if job_cap > 0:
            estimated = min(estimated, job_cap)
        return ([{slots[0]: field} for field in trimmed[:limit]], estimated)
    if len(slots) == 2:
        maps: list[dict[str, str]] = []
        left_fields = slot_fields[slots[0]]
        right_fields = slot_fields[slots[1]]
        for left in left_fields[: min(len(left_fields), 40)]:
            for right in right_fields[: min(len(right_fields), 40)]:
                if left == right:
                    continue
                maps.append({slots[0]: left, slots[1]: right})
                if len(maps) >= limit:
                    estimated = len(left_fields) * max(0, len(right_fields) - 1)
                    return maps, min(estimated, job_cap) if job_cap > 0 else estimated
        estimated = len(left_fields) * max(0, len(right_fields) - 1)
        return maps, min(estimated, job_cap) if job_cap > 0 else estimated
    estimate = 1
    for slot in slots:
        estimate *= max(1, len(slot_fields[slot]))
    maps: list[dict[str, str]] = []
    max_rows = max(1, limit)
    for index in range(max_rows):
        field_map = {}
        for slot in slots:
            fields = slot_fields[slot]
            field_map[slot] = fields[index % len(fields)]
        maps.append(field_map)
    if job_cap > 0:
        estimate = min(estimate, job_cap)
    return maps, estimate


def estimate_raw_capacity(raw_jobs: list[dict[str, Any]]) -> dict[str, Any]:
    tested = load_tested_fingerprints()
    family_estimates: defaultdict[str, dict[str, int]] = defaultdict(
        lambda: {
            "jobs": 0,
            "sampled": 0,
            "fresh_sample": 0,
            "estimated_total": 0,
            "estimated_fresh": 0,
        }
    )
    total_estimated = 0
    total_estimated_fresh = 0
    for job in raw_jobs:
        family = str(job.get("raw_alpha_family") or "-")
        template_path = resolve_template_path(str(job.get("template") or ""))
        if not template_path.exists():
            continue
        try:
            template = load_template(template_path)
        except Exception:
            continue
        field_maps, estimated_total = iter_sample_field_maps(job)
        if estimated_total <= 0:
            continue
        fresh_sample = 0
        for field_map in field_maps:
            try:
                config = replace_field_map(template, field_map)
                fingerprint = build_alpha_fingerprint(config)
            except Exception:
                continue
            if fingerprint not in tested:
                fresh_sample += 1
        sampled = len(field_maps)
        ratio = (fresh_sample / sampled) if sampled else 0.0
        estimated_fresh = int(round(estimated_total * ratio))
        family_estimates[family]["jobs"] += 1
        family_estimates[family]["sampled"] += sampled
        family_estimates[family]["fresh_sample"] += fresh_sample
        family_estimates[family]["estimated_total"] += estimated_total
        family_estimates[family]["estimated_fresh"] += estimated_fresh
        total_estimated += estimated_total
        total_estimated_fresh += estimated_fresh
    ranked = sorted(
        [{"family": family, **stats} for family, stats in family_estimates.items()],
        key=lambda row: row["estimated_fresh"],
        reverse=True,
    )
    return {
        "estimated_total_candidates": total_estimated,
        "estimated_fresh_candidates": total_estimated_fresh,
        "families": ranked[:30],
    }


GRADE_ORDER = ["1AVERAGE", "1GOOD", "1EXCELLENT", "1SPECTACULAR"]
REPAIR_TAG = "1REPAIR"
PLATFORM_GRADE_ORDER = ["AVERAGE", "GOOD", "EXCELLENT", "SPECTACULAR", "INFERIOR"]


def alpha_truth_records() -> dict[str, dict[str, Any]]:
    payload = read_json(ALPHA_TRUTH_STATE_PATH, {})
    records = payload.get("alphas") if isinstance(payload, dict) else {}
    return records if isinstance(records, dict) else {}


def platform_ids_by_grade_status() -> dict[str, dict[str, set[str]]]:
    payload = read_json(PLATFORM_TRUTH_PATH, {})
    tags = payload.get("tags") if isinstance(payload, dict) else {}
    output: dict[str, dict[str, set[str]]] = {}
    if not isinstance(tags, dict):
        return output
    for grade in GRADE_ORDER:
        by_status = ((tags.get(grade) or {}).get("ids_by_status")) or {}
        output[grade] = {
            "unsubmitted": {str(item) for item in (by_status.get("unsubmitted") or []) if str(item)},
            "submitted": {str(item) for item in (by_status.get("submitted") or []) if str(item)},
            "other": {str(item) for item in (by_status.get("other") or []) if str(item)},
        }
    return output


def extract_family_from_source(row: dict[str, Any]) -> str:
    parts = [
        row.get("raw_alpha_family"),
        row.get("source_file"),
        row.get("batch_name"),
        row.get("name"),
    ]
    details = row.get("alpha_details") or {}
    parts.extend([details.get("raw_alpha_family"), details.get("name")])
    source_text = " ".join(str(part or "") for part in parts)
    match = re.search(r"(?:raw_seed_|raw_alpha_)(.+?)(?:_lane\d+|\.yaml|:|>|$)", source_text)
    if match:
        return match.group(1)
    for token in source_text.replace(">", " ").replace("<", " ").split():
        if token.startswith("research_paper_") or token.startswith("credit_recovery_"):
            return token.split(":", 1)[0]
    return ""


def family_rows_from_batches(family: str) -> dict[str, dict[str, Any]]:
    rows: dict[str, tuple[datetime, dict[str, Any]]] = {}
    if not family or not BATCH_DIR.exists():
        return {}
    for path in BATCH_DIR.glob("*.jsonl"):
        if family not in path.name:
            continue
        try:
            handle = path.open("r", encoding="utf-8", errors="ignore")
        except Exception:
            continue
        with handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if family not in str(row.get("source_file") or row.get("batch_name") or path.name):
                    continue
                details = row.get("alpha_details") or {}
                alpha_id = str(details.get("id") or row.get("alpha_id") or "").strip()
                if not alpha_id:
                    continue
                stamp = (
                    parse_dt(details.get("dateModified"))
                    or parse_dt(details.get("dateCreated"))
                    or datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
                )
                if alpha_id not in rows or stamp > rows[alpha_id][0]:
                    rows[alpha_id] = (stamp, row)
    return {alpha_id: row for alpha_id, (_, row) in rows.items()}


def family_batch_total_rows(family: str) -> int:
    if not family or not BATCH_DIR.exists():
        return 0
    total = 0
    for path in BATCH_DIR.glob("*.jsonl"):
        if family not in path.name:
            continue
        try:
            handle = path.open("r", encoding="utf-8", errors="ignore")
        except Exception:
            continue
        with handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                if family not in str(row.get("source_file") or row.get("batch_name") or path.name):
                    continue
                total += 1
    return total


def grade_label_from_details(details: dict[str, Any], row: dict[str, Any]) -> str:
    grade_tag = derive_auto_grade_tag(details)
    if grade_tag:
        return grade_tag
    grade = str(details.get("grade") or row.get("grade") or "UNKNOWN").strip().upper()
    if grade in {"AVERAGE", "GOOD", "EXCELLENT", "SPECTACULAR"}:
        return f"1{grade}"
    return grade or "UNKNOWN"


def platform_grade_label(details: dict[str, Any], row: dict[str, Any]) -> str:
    grade = str(details.get("grade") or row.get("grade") or "UNKNOWN").strip().upper()
    return grade or "UNKNOWN"


def has_full_submit_pass(details: dict[str, Any], row: dict[str, Any]) -> bool:
    checks = extract_effective_checks(details)
    pass_count = sum(
        1
        for check in checks
        if str(check.get("result") or "").upper() == "PASS"
        or (
            str(check.get("result") or "").upper() == "FAIL"
            and str(check.get("name") or "").upper() == "ALREADY_SUBMITTED"
        )
    )
    failed = [
        check
        for check in checks
        if str(check.get("result") or "").upper() == "FAIL"
        and str(check.get("name") or "").upper() != "ALREADY_SUBMITTED"
    ]
    pending = [check for check in checks if str(check.get("result") or "").upper() == "PENDING"]
    return pass_count >= 8 and not failed and not pending


def is_user_submittable(details: dict[str, Any], row: dict[str, Any]) -> bool:
    checks = extract_effective_checks(details)
    pass_count = sum(
        1
        for check in checks
        if str(check.get("result") or "").upper() == "PASS"
        or (
            str(check.get("result") or "").upper() == "FAIL"
            and str(check.get("name") or "").upper() == "ALREADY_SUBMITTED"
        )
    )
    failed = [
        check
        for check in checks
        if str(check.get("result") or "").upper() == "FAIL"
        and str(check.get("name") or "").upper() != "ALREADY_SUBMITTED"
    ]
    return pass_count >= 7 and not failed


def tag_list(details: dict[str, Any], row: dict[str, Any]) -> list[str]:
    tags = details.get("tags") or row.get("tags") or []
    return [str(tag).upper() for tag in tags if str(tag).strip()]


def latest_alpha_rows(limit_files: int = 500) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    if not BATCH_DIR.exists():
        return latest
    paths = sorted(BATCH_DIR.glob("*.jsonl"), key=lambda path: path.stat().st_mtime, reverse=True)[:limit_files]
    for path in paths:
        try:
            handle = path.open("r", encoding="utf-8", errors="ignore")
        except Exception:
            continue
        with handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                details = row.get("alpha_details") or {}
                alpha_id = str(details.get("id") or row.get("alpha_id") or "").strip()
                if not alpha_id or alpha_id in latest:
                    continue
                latest[alpha_id] = row
    return latest


def source_repair_grade(tags: list[str], platform_grade: str) -> str | None:
    if platform_grade in {"EXCELLENT", "SPECTACULAR"}:
        return f"1{platform_grade}"
    for tag in tags:
        if tag in {"1EXCELLENT", "1SPECTACULAR"}:
            return tag
    return None


def family_result_summary(family: str) -> dict[str, Any]:
    if not family:
        return {}
    rows_by_alpha = family_rows_from_batches(family)
    total_backtests = family_batch_total_rows(family)

    total = len(rows_by_alpha)
    submit_counts: Counter[str] = Counter()
    submitted_counts: Counter[str] = Counter()
    fail_counts: Counter[str] = Counter()
    repair_counts: Counter[str] = Counter()
    platform_ids = platform_ids_by_grade_status()
    for row in rows_by_alpha.values():
        details = row.get("alpha_details") or {}
        platform_grade = platform_grade_label(details, row)
        grade_key = f"1{platform_grade}" if platform_grade in PLATFORM_GRADE_ORDER else "UNKNOWN"
        tags = tag_list(details, row)
        alpha_id = str(details.get("id") or row.get("alpha_id") or "").strip()
        status = str(details.get("status") or row.get("status") or "").strip().upper()
        for grade in GRADE_ORDER:
            if alpha_id in ((platform_ids.get(grade) or {}).get("unsubmitted") or set()):
                submit_counts[grade] += 1
                break
            if alpha_id in ((platform_ids.get(grade) or {}).get("submitted") or set()):
                submitted_counts[grade] += 1
                break
        else:
            repair_grade = source_repair_grade(tags, platform_grade)
            if REPAIR_TAG in tags and repair_grade:
                repair_counts[repair_grade] += 1
            fail_counts[grade_key] += 1
            continue
        repair_grade = source_repair_grade(tags, platform_grade)
        if REPAIR_TAG in tags and repair_grade:
            repair_counts[repair_grade] += 1
        continue

    return {
        "tested": total,
        "unique_alphas": total,
        "total_backtests": total_backtests or total,
        "submittable_total": sum(submit_counts.values()),
        "submitted_total": sum(submitted_counts.values()),
        "failed_total": sum(fail_counts.values()),
        "submittable_by_grade": {grade: submit_counts.get(grade, 0) for grade in GRADE_ORDER},
        "submitted_by_grade": {grade: submitted_counts.get(grade, 0) for grade in GRADE_ORDER},
        "failed_by_grade": {f"1{grade}": fail_counts.get(f"1{grade}", 0) for grade in PLATFORM_GRADE_ORDER},
        "repair_by_grade": {grade: repair_counts.get(grade, 0) for grade in ["1EXCELLENT", "1SPECTACULAR"]},
        "source": "platform_ids_mapped_to_batch_family",
    }


def current_family_runtime_from_state(family: str, state: dict[str, Any]) -> dict[str, Any]:
    if not family:
        return {}
    family = str(family)
    times: list[datetime] = []
    history = state.get("history") or []
    for row in history:
        if not isinstance(row, dict):
            continue
        if str(row.get("raw_alpha_family") or "") != family:
            continue
        for key in ("started_at", "finished_at", "ts"):
            parsed = parse_dt(row.get(key))
            if parsed:
                times.append(parsed)
    times.sort()
    if not times:
        return {}
    return {
        "first_runtime_at": times[0],
        "last_runtime_at": times[-1],
        "history_event_count": len(times),
    }


def grade_counts_from_batches(limit_files: int = 0) -> dict[str, Any]:
    platform_truth = read_json(PLATFORM_TRUTH_PATH, {})
    platform_tags = platform_truth.get("tags") if isinstance(platform_truth, dict) else {}
    if isinstance(platform_tags, dict) and platform_tags:
        submittable_counts: dict[str, int] = {}
        submitted_counts: dict[str, int] = {}
        for grade in GRADE_ORDER:
            counts = ((platform_tags.get(grade) or {}).get("counts") or {})
            submittable_counts[grade] = int(counts.get("unsubmitted") or 0)
            submitted_counts[grade] = int(counts.get("submitted") or 0)
        repair_counts = (((platform_tags.get(REPAIR_TAG) or {}).get("counts")) or {})
        return {
            "submittable_counts": submittable_counts,
            "submitted_counts": submitted_counts,
            "submittable_source": "platform_truth_cache",
            "platform_cache_generated_at": platform_truth.get("generated_at"),
            "repair_waiting_counts": {
                "1REPAIR": int(repair_counts.get("unsubmitted") or repair_counts.get("total") or 0)
            },
        }
    truth = alpha_truth_records()
    if truth:
        submittable_counts: Counter[str] = Counter()
        submitted_counts: Counter[str] = Counter()
        repair_waiting_counts: Counter[str] = Counter()
        for row in truth.values():
            if not isinstance(row, dict):
                continue
            tags = [str(tag).upper() for tag in (row.get("tags") or []) if str(tag).strip()]
            grade_tag = str(row.get("rule_grade_tag") or "").upper()
            status = str(row.get("status") or "").upper()
            if row.get("is_submit_ready") and grade_tag in GRADE_ORDER and REPAIR_TAG not in tags:
                if status == "UNSUBMITTED":
                    submittable_counts[grade_tag] += 1
                elif status == "ACTIVE":
                    submitted_counts[grade_tag] += 1
            repair_grade = next((tag for tag in tags if tag in {"1EXCELLENT", "1SPECTACULAR"}), None)
            if REPAIR_TAG in tags and repair_grade:
                repair_waiting_counts[repair_grade] += 1
        return {
            "submittable_counts": {grade: int(submittable_counts.get(grade, 0)) for grade in GRADE_ORDER},
            "submitted_counts": {grade: int(submitted_counts.get(grade, 0)) for grade in GRADE_ORDER},
            "submittable_source": "alpha_truth_state",
            "repair_waiting_counts": {
                grade: repair_waiting_counts.get(grade, 0) for grade in ["1EXCELLENT", "1SPECTACULAR"]
            },
        }
    submittable_counts: Counter[str] = Counter()
    submitted_counts: Counter[str] = Counter()
    repair_waiting_counts: Counter[str] = Counter()
    rows_by_alpha = latest_alpha_rows_all() if not limit_files else latest_alpha_rows(limit_files=limit_files)
    for row in rows_by_alpha.values():
        details = row.get("alpha_details") or {}
        platform_grade = platform_grade_label(details, row)
        tags = tag_list(details, row)
        grade_key = f"1{platform_grade}" if platform_grade in {"AVERAGE", "GOOD", "EXCELLENT", "SPECTACULAR"} else None
        status = str(details.get("status") or row.get("status") or "").strip().upper()
        if grade_key and has_full_submit_pass(details, row) and REPAIR_TAG not in tags:
            if status == "UNSUBMITTED":
                submittable_counts[grade_key] += 1
            elif status == "ACTIVE":
                submitted_counts[grade_key] += 1
        repair_grade = source_repair_grade(tags, platform_grade)
        if REPAIR_TAG in tags and repair_grade:
            repair_waiting_counts[repair_grade] += 1
    return {
        "submittable_counts": {grade: int(submittable_counts.get(grade, 0)) for grade in GRADE_ORDER},
        "submitted_counts": {grade: int(submitted_counts.get(grade, 0)) for grade in GRADE_ORDER},
        "submittable_source": "cloud_result_store",
        "repair_waiting_counts": {
            grade: repair_waiting_counts.get(grade, 0) for grade in ["1EXCELLENT", "1SPECTACULAR"]
        },
    }


def local_tz() -> timezone:
    return timezone(timedelta(hours=8))


def date_key_local(dt: datetime | None) -> str:
    if not dt:
        return ""
    return dt.astimezone(local_tz()).strftime("%Y-%m-%d")


def append_daily_snapshot_if_needed(status: dict[str, Any]) -> None:
    day_key = date_key_local(utc_now())
    snapshot = {
        "date": day_key,
        "captured_at": utc_now().isoformat().replace("+00:00", "Z"),
        "service": status.get("service"),
        "totals": status.get("totals") or {},
        "quality": status.get("quality") or {},
        "channels": {
            "active_count": ((status.get("channels") or {}).get("active_count")),
            "target_active": ((status.get("channels") or {}).get("target_active")),
            "repair_active_count": ((status.get("channels") or {}).get("repair_active_count")),
            "repair_active_limit": ((status.get("channels") or {}).get("repair_active_limit")),
        },
    }
    existing: list[dict[str, Any]] = read_json(DAILY_SNAPSHOT_PATH, [])
    if not isinstance(existing, list):
        existing = []
    if existing and isinstance(existing[-1], dict) and str(existing[-1].get("date") or "") == day_key:
        existing[-1] = snapshot
    else:
        existing.append(snapshot)
    existing = existing[-120:]
    DAILY_SNAPSHOT_PATH.parent.mkdir(parents=True, exist_ok=True)
    DAILY_SNAPSHOT_PATH.write_text(json.dumps(existing, ensure_ascii=False, indent=2), encoding="utf-8")


def refresh_platform_truth_cache() -> dict[str, Any]:
    payload = build_platform_truth_cache()
    PLATFORM_TRUTH_PATH.parent.mkdir(parents=True, exist_ok=True)
    PLATFORM_TRUTH_PATH.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return {
        "ok": True,
        "generated_at": payload.get("generated_at"),
        "source": payload.get("source"),
        "quality": grade_counts_from_batches(),
    }


def parse_date_param(text: str | None) -> datetime | None:
    value = str(text or "").strip()
    if not value:
        return None
    try:
        return datetime.fromisoformat(value).replace(tzinfo=local_tz())
    except Exception:
        return None


def latest_alpha_rows_all() -> dict[str, dict[str, Any]]:
    latest: dict[str, tuple[datetime, dict[str, Any]]] = {}
    if not BATCH_DIR.exists():
        return {}
    for path in BATCH_DIR.glob("*.jsonl"):
        try:
            handle = path.open("r", encoding="utf-8", errors="ignore")
        except Exception:
            continue
        with handle:
            for line in handle:
                try:
                    row = json.loads(line)
                except Exception:
                    continue
                details = row.get("alpha_details") or {}
                alpha_id = str(details.get("id") or row.get("alpha_id") or "").strip()
                if not alpha_id:
                    continue
                stamp = (
                    parse_dt(details.get("dateModified"))
                    or parse_dt(details.get("dateCreated"))
                    or datetime.fromtimestamp(path.stat().st_mtime, timezone.utc)
                )
                if alpha_id not in latest or stamp > latest[alpha_id][0]:
                    latest[alpha_id] = (stamp, row)
    return {alpha_id: row for alpha_id, (_, row) in latest.items()}


def daily_activity_summary(days: int = 15, start: str | None = None, end: str | None = None, limit_files: int = 600) -> dict[str, Any]:
    end_dt = parse_date_param(end) or utc_now().astimezone(local_tz())
    start_dt = parse_date_param(start) or (end_dt - timedelta(days=max(0, int(days) - 1)))
    start_day = start_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    end_day = end_dt.replace(hour=0, minute=0, second=0, microsecond=0)
    if end_day < start_day:
        start_day, end_day = end_day, start_day
    day_keys: list[str] = []
    cursor = start_day
    while cursor <= end_day:
        day_keys.append(cursor.strftime("%Y-%m-%d"))
        cursor += timedelta(days=1)

    by_day: dict[str, dict[str, Any]] = {
        key: {
            "date": key,
            "backtests": 0,
            "errors": 0,
            "repair_backtests": 0,
            "raw_backtests": 0,
        "submittable": Counter(),
        "submitted": Counter(),
        "repair_waiting": Counter(),
        }
        for key in day_keys
    }

    platform_truth = read_json(PLATFORM_TRUTH_PATH, {})
    platform_activities = (platform_truth.get("activities") or {}) if isinstance(platform_truth, dict) else {}
    platform_sim_daily = {
        str(item.get("date") or ""): int(item.get("value") or 0)
        for item in (((platform_activities.get("simulations") or {}).get("daily")) or [])
        if isinstance(item, dict)
    }
    platform_sub_daily = {
        str(item.get("date") or ""): int(item.get("value") or 0)
        for item in (((platform_activities.get("submissions") or {}).get("daily")) or [])
        if isinstance(item, dict)
    }

    seen_created_ids: set[str] = set()
    latest_rows = latest_alpha_rows_all()
    for alpha_id, row in latest_rows.items():
        details = row.get("alpha_details") or {}
        created = parse_dt(details.get("dateCreated") or row.get("dateCreated"))
        created_key = date_key_local(created)
        if created_key in by_day and alpha_id not in seen_created_ids:
            by_day[created_key]["backtests"] += 1
            seen_created_ids.add(alpha_id)
            source_text = str(row.get("source_file") or row.get("batch_name") or "")
            if "repair_" in source_text or "high_grade_repair" in source_text:
                by_day[created_key]["repair_backtests"] += 1
            if "raw_seed_" in source_text:
                by_day[created_key]["raw_backtests"] += 1

        # Daily quality is intentionally not reconstructed from the latest alpha
        # snapshot. A latest tag/status tells us the current state, not the
        # historical state on the created date, so showing it as daily history
        # would be misleading without a persisted daily snapshot.

    event_lines = read_slot_event_lines(hours=max(48, len(day_keys) * 30), limit=20000)
    for event in slot_events_from_journal(event_lines):
        key = date_key_local(event.get("finished_at"))
        if key in by_day and event.get("status") == "error":
            by_day[key]["errors"] += 1

    snapshots = read_json(DAILY_SNAPSHOT_PATH, [])
    snapshot_by_day = {
        str(item.get("date") or ""): item
        for item in snapshots
        if isinstance(item, dict) and str(item.get("date") or "") in by_day
    }

    rows: list[dict[str, Any]] = []
    for key in day_keys:
        day = by_day[key]
        snapshot = snapshot_by_day.get(key) or {}
        snap_quality = snapshot.get("quality") or {}
        rows.append(
            {
                "date": key,
                "backtests": int(platform_sim_daily.get(key, day["backtests"])),
                "platform_submissions": int(platform_sub_daily.get(key, 0)),
                "errors": int(day["errors"]),
                "repair_backtests": int(day["repair_backtests"]),
                "raw_backtests": int(day["raw_backtests"]),
                "submittable_total": sum(int(v or 0) for v in (snap_quality.get("submittable_counts") or {}).values()) if snapshot else None,
                "submittable_by_grade": snap_quality.get("submittable_counts") or {},
                "submitted_total": sum(int(v or 0) for v in (snap_quality.get("submitted_counts") or {}).values()) if snapshot else None,
                "submitted_by_grade": snap_quality.get("submitted_counts") or {},
                "repair_waiting_total": sum(int(v or 0) for v in (snap_quality.get("repair_waiting_counts") or {}).values()) if snapshot else None,
                "repair_waiting_by_grade": snap_quality.get("repair_waiting_counts") or {},
                "snapshot_reliable": bool(snapshot),
            }
        )
    return {
        "start": day_keys[0] if day_keys else "",
        "end": day_keys[-1] if day_keys else "",
        "days": len(day_keys),
        "backtest_source": "cloud_result_store.alpha_details.dateCreated",
        "quality_source": "cloud_daily_snapshots_if_available",
        "quality_reliable": any(bool(row.get("snapshot_reliable")) for row in rows),
        "rows": rows,
    }


def current_raw_family_summary(state: dict[str, Any], supply_payload: dict[str, Any]) -> dict[str, Any]:
    rotation = (supply_payload.get("raw_alpha_rotation") or {}) if isinstance(supply_payload, dict) else {}
    selected = list(rotation.get("selected_families") or [])
    family = str(selected[0] if selected else "").strip()
    if not family:
        return {}

    quality_stats = ((rotation.get("previous_family_quality_stats") or {}).get(family)) or {}
    result_summary = family_result_summary(family)
    family_batch_rows = family_rows_from_batches(family)
    batch_times = []
    for row in family_batch_rows.values():
        details = row.get("alpha_details") or {}
        parsed = parse_dt(details.get("dateCreated") or row.get("dateCreated"))
        if parsed:
            batch_times.append(parsed)
    batch_times.sort()
    runtime = current_family_runtime_from_state(family, state)
    first_seen = batch_times[0] if batch_times else runtime.get("first_runtime_at")
    last_seen = batch_times[-1] if batch_times else runtime.get("last_runtime_at")
    rotation_completed_count = int(((rotation.get("previous_family_completed_counts") or {}).get(family)) or 0)
    total_backtests = int(result_summary.get("total_backtests") or 0)
    unique_alphas = int(result_summary.get("unique_alphas") or result_summary.get("tested") or 0)
    completed_count = total_backtests or rotation_completed_count or unique_alphas
    active_hours = 0.0
    elapsed_hours = 0.0
    if first_seen and last_seen:
        active_hours = round((last_seen - first_seen).total_seconds() / 3600.0, 1)
        elapsed_hours = round((utc_now() - first_seen).total_seconds() / 3600.0, 1)
    return {
        "family": family,
        "active_hours": active_hours,
        "elapsed_hours": elapsed_hours,
        "completed": completed_count,
        "rotation_completed": rotation_completed_count,
        "unique_alphas": unique_alphas,
        "total_backtests": total_backtests or completed_count,
        "first_runtime_at": first_seen.isoformat().replace("+00:00", "Z") if first_seen else "",
        "last_runtime_at": last_seen.isoformat().replace("+00:00", "Z") if last_seen else "",
        "runtime_event_count": int(runtime.get("history_event_count") or 0),
        "carry_reason": str(((rotation.get("previous_family_carry_reasons") or {}).get(family)) or ""),
        "rotation_override": str(rotation.get("rotation_override") or ""),
        "min_completed_before_switch": int(rotation.get("min_completed_before_switch") or 0),
        "max_completed_before_switch": int(rotation.get("max_completed_before_switch") or 0),
        "high_grade_bonus_completed_before_switch": int(rotation.get("high_grade_bonus_completed_before_switch") or 0),
        "recent_quality_stats": quality_stats,
        "result_summary": result_summary,
    }


def service_active() -> str:
    try:
        result = subprocess.run(
            ["systemctl", "is-active", "learning-continuous"],
            text=True,
            capture_output=True,
            timeout=4,
            check=False,
        )
        return result.stdout.strip() or "unknown"
    except Exception:
        return "unknown"


def build_status() -> dict[str, Any]:
    state = read_json(STATE_PATH, {})
    rows = history_rows(state)
    supply_payload = read_json(SUPPLY_PATH, {})
    jobs = supply_payload.get("jobs") if isinstance(supply_payload, dict) else supply_payload
    if not isinstance(jobs, list):
        jobs = []
    jobs = [job for job in jobs if isinstance(job, dict)]
    lines = read_service_lines()
    event_lines = read_slot_event_lines(hours=30)
    journal_events = slot_events_from_journal(event_lines)
    latest_runtime = parse_latest_slot_status(lines)
    active_channels = parse_running_channels(lines)
    running_cap = safe_int(latest_runtime.get("running"), 0)
    if running_cap > 0 and len(active_channels) > running_cap:
        active_channels = active_channels[-running_cap:]
    if running_cap > 0 and len(active_channels) < running_cap:
        for index in range(len(active_channels), running_cap):
            active_channels.append(
                {
                    "name": f"active_slot_{index + 1}_details_pending",
                    "mode": "-",
                    "mechanism": "-",
                    "target_mechanism": "-",
                    "plan": "-",
                    "fields": "{}",
                    "is_repair": False,
                }
            )
    repair_jobs = [job for job in jobs if str(job.get("repair_engine") or "") == "high_grade_failed_repair"]
    raw_jobs = [job for job in jobs if job.get("raw_alpha_family")]
    raw_counts = batch_family_counts()
    raw_pool = raw_pool_families()
    archived_raw = archived_raw_families()
    archived_keys = {item.lower() for item in archived_raw}
    available_raw_pool = [family for family in raw_pool if family.lower() not in archived_keys]
    family_supply = Counter(str(job.get("raw_alpha_family") or "-") for job in raw_jobs)
    raw_capacity = estimate_raw_capacity(raw_jobs)
    repair_report = read_json(REPAIR_REPORT_PATH, {})
    repair_lifecycle = read_json(REPAIR_LIFECYCLE_PATH, {})
    current_scan = state.get("candidate_scan_stats") or {}
    current_family = current_raw_family_summary(state, supply_payload if isinstance(supply_payload, dict) else {})

    status = {
        "generated_at": utc_now().isoformat().replace("+00:00", "Z"),
        "service": service_active(),
        "totals": {
            "completed": int(state.get("completed") or 0),
            "errors": int(state.get("errors") or 0),
            "supply_jobs": len(jobs),
            "repair_jobs": len(repair_jobs),
            "raw_jobs": len(raw_jobs),
            "raw_pool_families": len(raw_pool),
            "raw_pool_available_families": len(available_raw_pool),
            "raw_pool_used_families": len(archived_raw),
            "tested_fingerprints": safe_int(latest_runtime.get("tested"), safe_int(current_scan.get("tested"), 0)),
            "estimated_raw_fresh_candidates": int(raw_capacity.get("estimated_fresh_candidates") or 0),
        },
        "speed": bucket_stats(rows, journal_events),
        "channels": {
            "active": active_channels,
            "active_count": safe_int(latest_runtime.get("running"), len(active_channels)),
            "target_active": safe_int(latest_runtime.get("target_active"), 3),
            "repair_active_count": safe_int(
                latest_runtime.get("repair_running"),
                len([row for row in active_channels if row.get("is_repair")]),
            ),
            "repair_active_limit": safe_int(latest_runtime.get("repair_running_limit"), 1),
            "latest_runtime": latest_runtime,
            "latest_scan": current_scan,
        },
        "repair": {
            "jobs": len(repair_jobs),
            "first_jobs": [job.get("name") for job in repair_jobs[:12]],
            "report": repair_report,
            "lifecycle": repair_lifecycle,
        },
        "raw_families": {
            "pool_count": len(raw_pool),
            "available_pool_count": len(available_raw_pool),
            "used_pool_count": len(archived_raw),
            "supply_counts": dict(family_supply.most_common(30)),
            "completed_counts": dict(sorted(raw_counts.items(), key=lambda item: item[1], reverse=True)[:50]),
            "pool_preview": raw_pool[:60],
            "available_pool_preview": available_raw_pool[:60],
            "used_pool_preview": archived_raw[:60],
            "capacity": raw_capacity,
            "current_family": current_family,
        },
        "quality": grade_counts_from_batches(),
    }
    append_daily_snapshot_if_needed(status)
    return status


def json_response(handler: BaseHTTPRequestHandler, payload: Any, status: int = 200) -> None:
    body = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def text_response(handler: BaseHTTPRequestHandler, body: str, content_type: str = "text/html; charset=utf-8") -> None:
    payload = body.encode("utf-8")
    handler.send_response(200)
    handler.send_header("Content-Type", content_type)
    handler.send_header("Cache-Control", "no-store")
    handler.send_header("Content-Length", str(len(payload)))
    handler.end_headers()
    handler.wfile.write(payload)


HTML = r"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
  <meta name="theme-color" content="#101417">
  <link rel="manifest" href="/manifest.json">
  <title>Alpha Cloud</title>
  <style>
    :root {
      color-scheme: dark;
      --bg: #101417;
      --panel: #171d21;
      --panel2: #1e262b;
      --text: #eef3f0;
      --muted: #95a49d;
      --line: #2b3539;
      --ok: #72d18a;
      --warn: #e2c166;
      --bad: #ee7b72;
      --blue: #73b7ff;
    }
    * { box-sizing: border-box; }
    html, body { width: 100%; max-width: 100%; overflow-x: hidden; }
    body {
      margin: 0;
      font-family: system-ui, -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif;
      background: var(--bg);
      color: var(--text);
      letter-spacing: 0;
    }
    header {
      position: sticky;
      top: 0;
      z-index: 10;
      padding: 14px 14px 10px;
      background: rgba(16, 20, 23, .94);
      border-bottom: 1px solid var(--line);
      backdrop-filter: blur(10px);
    }
    h1 { margin: 0; font-size: 20px; font-weight: 700; }
    .sub { margin-top: 4px; color: var(--muted); font-size: 12px; }
    main { width: 100%; padding: 12px; display: grid; gap: 12px; max-width: 920px; margin: 0 auto; overflow-x: hidden; }
    section { width: 100%; min-width: 0; background: var(--panel); border: 1px solid var(--line); border-radius: 8px; padding: 12px; overflow: hidden; }
    h2 { margin: 0 0 10px; font-size: 15px; font-weight: 700; }
    .grid { display: grid; grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 8px; }
    .metric { min-width: 0; max-width: 100%; background: var(--panel2); border: 1px solid var(--line); border-radius: 7px; padding: 10px; min-height: 70px; overflow: hidden; }
    .label { color: var(--muted); font-size: 12px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
    .value { margin-top: 5px; font-size: 23px; font-weight: 760; line-height: 1.05; overflow-wrap: anywhere; word-break: break-word; }
    .small { font-size: 12px; color: var(--muted); margin-top: 5px; overflow-wrap: anywhere; }
    .ok { color: var(--ok); }
    .warn { color: var(--warn); }
    .bad { color: var(--bad); }
    .blue { color: var(--blue); }
    .table-wrap { width: 100%; max-width: 100%; overflow: hidden; }
    table { width: 100%; max-width: 100%; border-collapse: collapse; font-size: 12px; table-layout: fixed; }
    th, td { text-align: left; padding: 7px 4px; border-bottom: 1px solid var(--line); vertical-align: top; overflow-wrap: anywhere; word-break: break-word; min-width: 0; }
    th { color: var(--muted); font-weight: 600; }
    .bar { height: 8px; background: #263035; border-radius: 999px; overflow: hidden; margin-top: 5px; }
    .bar > span { display: block; height: 100%; background: var(--blue); width: 0; }
    .chip { display: inline-block; padding: 3px 7px; border-radius: 999px; border: 1px solid var(--line); background: var(--panel2); font-size: 11px; margin: 2px 3px 2px 0; color: var(--muted); }
    .mono { font-family: ui-monospace, SFMono-Regular, Consolas, "Liberation Mono", monospace; overflow-wrap: anywhere; }
    .stack { display: grid; gap: 8px; }
    .controls { display: grid; grid-template-columns: 1fr 1fr auto; gap: 8px; align-items: end; margin-bottom: 10px; }
    input, button { width: 100%; border: 1px solid var(--line); border-radius: 6px; background: var(--panel2); color: var(--text); padding: 8px; font: inherit; }
    button { cursor: pointer; color: var(--bg); background: var(--blue); font-weight: 700; }
    @media (min-width: 760px) {
      .grid { grid-template-columns: repeat(4, minmax(0, 1fr)); }
      main { grid-template-columns: repeat(2, minmax(0, 1fr)); }
      .wide { grid-column: 1 / -1; }
    }
    @media (max-width: 759px) {
      header { padding: 12px 10px 8px; }
      main { padding: 8px; gap: 8px; }
      section { padding: 9px; border-radius: 7px; }
      h1 { font-size: 18px; }
      h2 { font-size: 14px; }
      .grid { gap: 6px; }
      .metric { padding: 8px; min-height: 62px; }
      .value { font-size: 18px; }
      table { font-size: 11px; }
      th, td { padding: 6px 3px; }
      #channels th:nth-child(4), #channels td:nth-child(4) { display: none; }
      #speed th:nth-child(4), #speed td:nth-child(4) { display: none; }
      .controls { grid-template-columns: 1fr; }
    }
  </style>
</head>
<body>
  <header>
    <h1>Alpha Cloud</h1>
    <div class="sub" id="stamp">loading</div>
  </header>
  <main>
    <section class="wide">
      <h2>实时概览</h2>
      <div class="grid" id="overview"></div>
    </section>
    <section>
      <h2>通道状态</h2>
      <div id="channels"></div>
    </section>
    <section>
      <h2>测速</h2>
      <div id="speed"></div>
    </section>
    <section class="wide">
      <h2>每小时完成量</h2>
      <div id="hourly"></div>
    </section>
    <section class="wide">
      <h2>每日统计</h2>
      <div class="controls">
        <label><div class="label">开始日期</div><input id="daily-start" type="date"></label>
        <label><div class="label">结束日期</div><input id="daily-end" type="date"></label>
        <button id="daily-load" type="button">查询</button>
      </div>
      <div id="daily"></div>
    </section>
    <section>
      <h2>原始家族</h2>
      <div id="families"></div>
    </section>
    <section class="wide">
      <h2>可提交与待修复</h2>
      <div id="quality"></div>
    </section>
  </main>
  <script>
    const fmt = (v) => (v === undefined || v === null || v === '') ? '-' : v;
    const esc = (s) => String(fmt(s)).replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
    const metric = (label, value, sub='', cls='') => `<div class="metric"><div class="label">${esc(label)}</div><div class="value ${cls}">${esc(value)}</div><div class="small">${esc(sub)}</div></div>`;
    function table(rows, cols) {
      if (!rows.length) return '<div class="small">暂无数据</div>';
      return `<div class="table-wrap"><table><thead><tr>${cols.map(c=>`<th>${esc(c[0])}</th>`).join('')}</tr></thead><tbody>${rows.map(r=>`<tr>${cols.map(c=>`<td>${esc(c[1](r))}</td>`).join('')}</tr>`).join('')}</tbody></table></div>`;
    }
    function pairs(obj, max=12) {
      return Object.entries(obj || {}).slice(0, max).map(([k,v]) => ({k,v}));
    }
    function shortFamily(s) {
      s = String(fmt(s));
      if (s.length <= 34) return s;
      return `${s.slice(0, 16)}…${s.slice(-14)}`;
    }
    const chipSummary = (obj) => pairs(obj || {}).map(x => `${x.k}:${x.v}`).join(' ');
    const isoDate = (d) => d.toISOString().slice(0, 10);
    async function loadDaily() {
      const start = document.getElementById('daily-start').value;
      const end = document.getElementById('daily-end').value;
      const qs = new URLSearchParams();
      if (start) qs.set('start', start);
      if (end) qs.set('end', end);
      if (!start && !end) qs.set('days', '15');
      const res = await fetch(`/api/daily?${qs.toString()}`, {cache: 'no-store'});
      const d = await res.json();
      document.getElementById('daily').innerHTML = table(d.rows || [], [
        ['日期', r => r.date],
        ['回测', r => r.backtests],
        ['repair/raw', r => `${r.repair_backtests}/${r.raw_backtests}`],
        ['已提交', r => r.platform_submissions || 0],
        ['质量快照', r => r.snapshot_reliable ? `可提:${r.submittable_total} 已提:${r.submitted_total} 待修:${r.repair_waiting_total}` : '未快照']
      ]) + `<div class="small">source: ${esc(d.backtest_source || '-')}</div>` +
      `<div class="small">回测/已提交优先使用平台真实缓存；每日质量使用云端当日快照，没有快照的历史日期显示“未快照”。</div>`;
    }
    async function refreshPlatformTruth() {
      document.getElementById('stamp').textContent = 'refreshing platform truth...';
      const res = await fetch('/api/platform-refresh', {cache: 'no-store'});
      if (!res.ok) throw new Error(await res.text());
      return await res.json();
    }
    async function refresh() {
      const res = await fetch('/api/status', {cache: 'no-store'});
      const d = await res.json();
      document.getElementById('stamp').textContent = `${d.service} · ${d.generated_at}`;
      const w1 = d.speed.windows['1h'] || {};
      const w24 = d.speed.windows['24h'] || {};
      document.getElementById('overview').innerHTML = [
        metric('服务', d.service, 'learning-continuous', d.service === 'active' ? 'ok' : 'bad'),
        metric('通道', `${d.channels.active_count}/${d.channels.target_active || 3}`, `repair ${d.channels.repair_active_count}/${d.channels.repair_active_limit || 1}`, d.channels.active_count >= (d.channels.target_active || 3) ? 'ok' : 'warn'),
        metric('1小时', w1.completed, `${w1.per_hour || 0}/h · err ${w1.errors || 0}`, 'blue'),
        metric('24小时', w24.completed, `${w24.per_hour || 0}/h · err ${w24.errors || 0}`, 'blue'),
        metric('总完成', d.totals.completed, `errors ${d.totals.errors}`),
        metric('供给池', d.totals.supply_jobs, `repair ${d.totals.repair_jobs} · raw ${d.totals.raw_jobs}`),
        metric('原始可用池', d.totals.raw_pool_available_families, `used ${d.totals.raw_pool_used_families} / total ${d.totals.raw_pool_families}`),
        metric('可测原始候选估算', d.totals.estimated_raw_fresh_candidates, 'fresh raw estimate', 'blue'),
      ].join('');
      document.getElementById('channels').innerHTML = table(d.channels.active || [], [
        ['任务', r => r.name],
        ['类型', r => r.is_repair ? 'repair' : r.mode],
        ['家族/机制', r => r.mechanism || r.target_mechanism],
        ['字段', r => r.fields]
      ]);
      document.getElementById('speed').innerHTML = table(Object.entries(d.speed.windows).map(([k,v]) => ({k,...v})), [
        ['窗口', r => r.k],
        ['完成', r => r.completed],
        ['/小时', r => r.per_hour],
        ['repair/raw', r => `${r.repair_completed}/${r.raw_completed}`]
      ]);
      document.getElementById('hourly').innerHTML = table(d.speed.hourly, [
        ['小时', r => r.hour],
        ['完成', r => r.completed],
        ['repair', r => r.repair],
        ['错误', r => r.errors]
      ]);
      const cf = d.raw_families.current_family || {};
      const fs = cf.result_summary || {};
      document.getElementById('families').innerHTML =
        `<div class="stack">` +
        metric('当前家族', shortFamily(cf.family || '-'), `总回测 ${cf.total_backtests || fs.total_backtests || cf.completed || 0} · 唯一 ${cf.unique_alphas || fs.unique_alphas || fs.tested || 0}`, 'blue') +
        metric('家族时间', `${cf.active_hours || 0}h / ${cf.elapsed_hours || 0}h`, '真实测时长 / 自然时长') +
        `<div class="small">能提交 ${esc(fs.submittable_total || 0)}</div>${pairs(fs.submittable_by_grade || {}).map(x => `<span class="chip">${esc(x.k)} ${esc(x.v)}</span>`).join('')}` +
        `<div class="small">已提交 ${esc(fs.submitted_total || 0)}</div>${pairs(fs.submitted_by_grade || {}).map(x => `<span class="chip">${esc(x.k)} ${esc(x.v)}</span>`).join('')}` +
        `<div class="small">不能提交 ${esc(fs.failed_total || 0)}</div>${pairs(fs.failed_by_grade || {}).map(x => `<span class="chip">${esc(x.k)} ${esc(x.v)}</span>`).join('')}` +
        `<div class="small">待修</div>${pairs(fs.repair_by_grade || {}).map(x => `<span class="chip">${esc(x.k)} ${esc(x.v)}</span>`).join('')}` +
        `<div class="small">当前原始候选估算</div>` +
        table((d.raw_families.capacity && d.raw_families.capacity.families || []).slice(0, 3), [['家族', r => r.family], ['未测估算', r => r.estimated_fresh]]) +
        `</div>`;
      document.getElementById('quality').innerHTML =
        `<div class="small">真实可提交</div>${pairs(d.quality.submittable_counts).map(x => `<span class="chip">${esc(x.k)} ${esc(x.v)}</span>`).join('')}` +
        `<div class="small">真实已提交</div>${pairs(d.quality.submitted_counts).map(x => `<span class="chip">${esc(x.k)} ${esc(x.v)}</span>`).join('')}` +
        `<div class="small">真实待修</div>${pairs(d.quality.repair_waiting_counts).map(x => `<span class="chip">${esc(x.k)} ${esc(x.v)}</span>`).join('')}` +
        `<div class="small">source: ${esc(d.quality.submittable_source || 'cloud_result_store')} ${esc(d.quality.platform_cache_generated_at || '')}</div>`;
    }
    const today = new Date();
    const startDefault = new Date(today);
    startDefault.setDate(today.getDate() - 14);
    document.getElementById('daily-start').value = isoDate(startDefault);
    document.getElementById('daily-end').value = isoDate(today);
    document.getElementById('daily-load').addEventListener('click', () => loadDaily().catch(err => { document.getElementById('daily').textContent = err; }));
    refreshPlatformTruth().then(refresh).then(loadDaily).catch(err => { document.getElementById('stamp').textContent = err; });
    setInterval(refresh, 15000);
    if ('serviceWorker' in navigator) navigator.serviceWorker.register('/sw.js').catch(() => {});
  </script>
</body>
</html>
"""


MANIFEST = {
    "name": "Alpha Cloud",
    "short_name": "AlphaCloud",
    "start_url": "/",
    "display": "standalone",
    "background_color": "#101417",
    "theme_color": "#101417",
    "icons": [],
}


def dashboard_password() -> str:
    return str(os.environ.get("DASHBOARD_PASSWORD") or "").strip()


class Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt: str, *args: Any) -> None:
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def authorized(self) -> bool:
        password = dashboard_password()
        if not password:
            return True
        header = self.headers.get("Authorization") or ""
        if not header.startswith("Basic "):
            return False
        try:
            decoded = base64.b64decode(header.split(" ", 1)[1]).decode("utf-8")
        except Exception:
            return False
        username, _, supplied = decoded.partition(":")
        return username == "alpha" and supplied == password

    def require_auth(self) -> bool:
        if self.authorized():
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="Alpha Cloud"')
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write("Authentication required.\n".encode("utf-8"))
        return False

    def do_HEAD(self) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def do_GET(self) -> None:
        parsed = urlparse(self.path)
        path = parsed.path
        if path == "/health":
            json_response(self, {"ok": True, "service": service_active()})
            return
        if not self.require_auth():
            return
        if path == "/api/status":
            json_response(self, build_status())
            return
        if path == "/api/platform-refresh":
            try:
                json_response(self, refresh_platform_truth_cache())
            except Exception as exc:
                json_response(self, {"ok": False, "error": str(exc)}, status=500)
            return
        if path == "/api/daily":
            params = parse_qs(parsed.query)
            start = (params.get("start") or [""])[0]
            end = (params.get("end") or [""])[0]
            try:
                days = int((params.get("days") or ["15"])[0])
            except Exception:
                days = 15
            json_response(self, daily_activity_summary(days=days, start=start, end=end))
            return
        if path == "/manifest.json":
            json_response(self, MANIFEST)
            return
        if path == "/sw.js":
            text_response(self, "self.addEventListener('fetch', event => {});\n", "application/javascript; charset=utf-8")
            return
        if path == "/":
            text_response(self, HTML)
            return
        text_response(self, f"<h1>404</h1><p>{html.escape(path)}</p>")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"Alpha Cloud dashboard listening on http://{args.host}:{args.port}", flush=True)
    server.serve_forever()


if __name__ == "__main__":
    main()
