#!/usr/bin/env python
"""Summarize outcome quality by dataset, semantic tags, and settings clusters.

This is an offline feedback layer. It reuses the current result index and
current supply job metadata to explain which datasets / semantic tags /
settings clusters are producing pass7/pass8/submit-ready outcomes.
"""

from __future__ import annotations

import argparse
from collections import Counter
import json
from pathlib import Path
import sys
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from script.family_feedback_report import (  # noqa: E402
    FINAL_GATE,
    PASS_GATE,
    failed_checks,
    family_job_map,
    infer_family_from_result,
    load_json,
    load_jsonl,
    parse_slot_miner_source,
    pass_count,
    raw_family_names,
    safe_float,
    self_corr_status,
)


RESULTS_INDEX = ROOT_DIR / "result_store" / "index" / "alpha_catalog.jsonl"
SUPPLY_JOBS = ROOT_DIR / "result_store" / "supply" / "supply_jobs.json"
JOB_METADATA_ARCHIVE = ROOT_DIR / "result_store" / "analysis" / "job_metadata_archive.json"
OUTPUT_JSON = ROOT_DIR / "result_store" / "analysis" / "dataset_feedback_report.json"
OUTPUT_MD = ROOT_DIR / "result_store" / "analysis" / "dataset_feedback_report.md"
FALLBACK_DIR = Path("C:/tmp/learning_dataset_feedback_report")


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


def load_job_metadata_archive() -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    payload = load_json(JOB_METADATA_ARCHIVE)
    jobs_by_name = payload.get("jobs_by_name") if isinstance(payload, dict) else {}
    templates_by_path = payload.get("templates_by_path") if isinstance(payload, dict) else {}
    if not isinstance(jobs_by_name, dict):
        jobs_by_name = {}
    if not isinstance(templates_by_path, dict):
        templates_by_path = {}
    return (
        {str(key): value for key, value in jobs_by_name.items() if isinstance(value, dict)},
        {str(key): str(value) for key, value in templates_by_path.items()},
    )


def _bucket() -> dict[str, Any]:
    return {
        "tested": 0,
        "pass7_count": 0,
        "pass8_count": 0,
        "submit_ready_count": 0,
        "self_corr_fail_count": 0,
        "failed_checks": Counter(),
        "raw_families": Counter(),
        "mechanisms": Counter(),
        "sharpe_values": [],
        "fitness_values": [],
    }


def _finalize(name: str, payload: dict[str, Any]) -> dict[str, Any]:
    sharpe_values = payload.pop("sharpe_values")
    fitness_values = payload.pop("fitness_values")
    payload["name"] = name
    payload["avg_sharpe"] = round(sum(sharpe_values) / len(sharpe_values), 4) if sharpe_values else 0.0
    payload["avg_fitness"] = round(sum(fitness_values) / len(fitness_values), 4) if fitness_values else 0.0
    payload["best_sharpe"] = max(sharpe_values) if sharpe_values else 0.0
    payload["best_fitness"] = max(fitness_values) if fitness_values else 0.0
    payload["failed_checks"] = dict(payload["failed_checks"].most_common())
    payload["raw_families"] = dict(payload["raw_families"].most_common(8))
    payload["mechanisms"] = dict(payload["mechanisms"].most_common(8))
    return payload


def planned_exposure_rows(jobs: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    datasets: Counter[str] = Counter()
    semantic_tags: Counter[str] = Counter()
    settings_clusters: Counter[str] = Counter()
    raw_families: Counter[str] = Counter()

    for job in jobs:
        family = str(job.get("raw_alpha_family") or job.get("source_family_key") or "")
        if family:
            raw_families[family] += 1
        for spec in (job.get("slot_dataset_plan") or []):
            if isinstance(spec, dict) and spec.get("dataset_id"):
                datasets[str(spec.get("dataset_id"))] += 1
        for tag in job.get("field_semantic_tags") or []:
            if str(tag):
                semantic_tags[str(tag)] += 1
        cluster = str(job.get("settings_cluster") or "")
        if cluster:
            settings_clusters[cluster] += 1

    def rows(counter: Counter[str], limit: int = 30) -> list[dict[str, Any]]:
        return [{"name": name, "job_count": count} for name, count in counter.most_common(limit)]

    return {
        "datasets": rows(datasets),
        "semantic_tags": rows(semantic_tags),
        "settings_clusters": rows(settings_clusters),
        "raw_families": rows(raw_families),
    }


def summarize() -> dict[str, Any]:
    jobs = load_supply_jobs()
    name_to_job = family_job_map(jobs)
    archive_jobs, archive_templates = load_job_metadata_archive()
    name_to_job.update({name: job for name, job in archive_jobs.items() if name not in name_to_job})
    known_families = raw_family_names()

    dataset_rows: dict[str, dict[str, Any]] = {}
    semantic_rows: dict[str, dict[str, Any]] = {}
    settings_rows: dict[str, dict[str, Any]] = {}
    result_count = 0
    matched_result_count = 0
    unmatched_result_count = 0

    for row in load_results():
        result_count += 1
        source_file = str(row.get("source_file") or "")
        if not source_file:
            unmatched_result_count += 1
            continue
        parsed = parse_slot_miner_source(source_file)
        job_name = str(parsed.get("job_name") or "")
        if not job_name:
            job_name = Path(source_file).stem
        job = name_to_job.get(job_name)
        if not job:
            template_name = str(parsed.get("template_file") or "")
            archived_job_name = archive_templates.get(template_name)
            if archived_job_name:
                job = name_to_job.get(archived_job_name)
        if not job:
            unmatched_result_count += 1
            continue
        matched_result_count += 1
        family = str(job.get("raw_alpha_family") or infer_family_from_result(row, known_families) or "")
        mechanism_id = str(job.get("mechanism_id") or "")
        alpha_details = row.get("alpha_details") or {}
        is_block = alpha_details.get("is") or {}
        pc = pass_count(alpha_details)
        corr = self_corr_status(alpha_details)
        sharpe = safe_float(is_block.get("sharpe"))
        fitness = safe_float(is_block.get("fitness"))

        slot_plan = [spec for spec in (job.get("slot_dataset_plan") or []) if isinstance(spec, dict)]
        dataset_ids = list(dict.fromkeys(str(spec.get("dataset_id") or "") for spec in slot_plan if spec.get("dataset_id")))
        semantic_tags = list(dict.fromkeys(str(tag) for tag in (job.get("field_semantic_tags") or []) if str(tag)))
        settings_cluster = str(job.get("settings_cluster") or "")

        def update_bucket(bucket: dict[str, Any]) -> None:
            bucket["tested"] += 1
            if pc >= PASS_GATE:
                bucket["pass7_count"] += 1
            if pc >= FINAL_GATE:
                bucket["pass8_count"] += 1
            if pc >= FINAL_GATE and corr == "PASS":
                bucket["submit_ready_count"] += 1
            if corr == "FAIL":
                bucket["self_corr_fail_count"] += 1
            for item in failed_checks(alpha_details):
                bucket["failed_checks"][item] += 1
            if family:
                bucket["raw_families"][family] += 1
            if mechanism_id:
                bucket["mechanisms"][mechanism_id] += 1
            bucket["sharpe_values"].append(sharpe)
            bucket["fitness_values"].append(fitness)

        for dataset_id in dataset_ids:
            if not dataset_id:
                continue
            update_bucket(dataset_rows.setdefault(dataset_id, _bucket()))
        for semantic_tag in semantic_tags:
            update_bucket(semantic_rows.setdefault(semantic_tag, _bucket()))
        if settings_cluster:
            update_bucket(settings_rows.setdefault(settings_cluster, _bucket()))

    datasets = [_finalize(name, payload) for name, payload in dataset_rows.items()]
    semantics = [_finalize(name, payload) for name, payload in semantic_rows.items()]
    settings = [_finalize(name, payload) for name, payload in settings_rows.items()]

    datasets.sort(key=lambda item: (-int(item.get("submit_ready_count") or 0), -int(item.get("pass7_count") or 0), -safe_float(item.get("avg_sharpe")), str(item.get("name") or "")))
    semantics.sort(key=lambda item: (-int(item.get("submit_ready_count") or 0), -int(item.get("pass7_count") or 0), -safe_float(item.get("avg_sharpe")), str(item.get("name") or "")))
    settings.sort(key=lambda item: (-int(item.get("submit_ready_count") or 0), -int(item.get("pass7_count") or 0), -safe_float(item.get("avg_sharpe")), str(item.get("name") or "")))

    return {
        "schema_version": 1,
        "result_count": result_count,
        "matched_result_count": matched_result_count,
        "unmatched_result_count": unmatched_result_count,
        "job_metadata_archive_count": len(archive_jobs),
        "planned_exposure": planned_exposure_rows(list(name_to_job.values())),
        "dataset_count": len(datasets),
        "semantic_tag_count": len(semantics),
        "settings_cluster_count": len(settings),
        "datasets": datasets,
        "semantic_tags": semantics,
        "settings_clusters": settings,
    }


def render_section(title: str, rows: list[dict[str, Any]], limit: int = 20) -> list[str]:
    lines = [f"## {title}", ""]
    for row in rows[:limit]:
        lines.extend(
            [
                f"### {row.get('name')}",
                f"- Tested: {row.get('tested')} | 7pass: {row.get('pass7_count')} | 8pass: {row.get('pass8_count')} | submit_ready: {row.get('submit_ready_count')}",
                f"- Self-corr fail: {row.get('self_corr_fail_count')}",
                f"- Avg Sharpe/Fitness: {row.get('avg_sharpe')}/{row.get('avg_fitness')}",
                f"- Best Sharpe/Fitness: {row.get('best_sharpe')}/{row.get('best_fitness')}",
                f"- Mechanisms: {row.get('mechanisms')}",
                f"- Raw families: {row.get('raw_families')}",
                f"- Failed checks: {row.get('failed_checks')}",
                "",
            ]
        )
    return lines


def render_exposure_section(payload: dict[str, Any]) -> list[str]:
    exposure = payload.get("planned_exposure") or {}
    lines = ["## Current Planned Exposure", ""]
    for title, key in [
        ("Datasets", "datasets"),
        ("Semantic Tags", "semantic_tags"),
        ("Settings Clusters", "settings_clusters"),
        ("Raw Families", "raw_families"),
    ]:
        rows = exposure.get(key) or []
        lines.append(f"### {title}")
        if not rows:
            lines.extend(["- <none>", ""])
            continue
        for row in rows[:20]:
            lines.append(f"- {row.get('name')}: {row.get('job_count')} jobs")
        lines.append("")
    return lines


def render_markdown(payload: dict[str, Any]) -> str:
    lines = [
        "# Dataset Feedback Report",
        "",
        f"- Results: {payload.get('result_count')} | matched: {payload.get('matched_result_count')} | unmatched: {payload.get('unmatched_result_count')}",
        f"- Archived job metadata: {payload.get('job_metadata_archive_count')}",
        f"- Dataset count: {payload.get('dataset_count')}",
        f"- Semantic tag count: {payload.get('semantic_tag_count')}",
        f"- Settings cluster count: {payload.get('settings_cluster_count')}",
        "",
    ]
    lines.extend(render_exposure_section(payload))
    lines.extend(render_section("Top Datasets", payload.get("datasets") or []))
    lines.extend(render_section("Top Semantic Tags", payload.get("semantic_tags") or []))
    lines.extend(render_section("Top Settings Clusters", payload.get("settings_clusters") or []))
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
            return json_path
        except PermissionError:
            continue
    return None


def main() -> None:
    parser = argparse.ArgumentParser(description="Build dataset/semantic/settings feedback report.")
    parser.parse_args()
    payload = summarize()
    output = write(payload)
    print(f"Datasets: {payload['dataset_count']}")
    print(f"Semantic tags: {payload['semantic_tag_count']}")
    print(f"Settings clusters: {payload['settings_cluster_count']}")
    print(f"Matched results: {payload['matched_result_count']}/{payload['result_count']}")
    if output:
        try:
            print(f"JSON: {output.relative_to(ROOT_DIR)}")
        except ValueError:
            print(f"JSON: {output}")
    else:
        print("JSON: <write skipped: permission denied>")


if __name__ == "__main__":
    main()
