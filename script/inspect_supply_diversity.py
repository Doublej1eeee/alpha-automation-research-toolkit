#!/usr/bin/env python
"""Inspect generated supply job diversity."""

from __future__ import annotations

from collections import Counter
import json
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
SUPPLY_JOBS_FILE = ROOT_DIR / "result_store" / "supply" / "supply_jobs.json"


def main() -> None:
    payload = json.loads(SUPPLY_JOBS_FILE.read_text(encoding="utf-8"))
    jobs = payload.get("jobs") or []
    clusters: Counter[str] = Counter()
    datasets: Counter[str] = Counter()
    tiers: Counter[str] = Counter()
    family_lanes: Counter[str] = Counter()
    for job in jobs:
        clusters.update(str(item) for item in (job.get("field_clusters") or []) if item)
        family = str(job.get("raw_alpha_family") or "")
        if family:
            family_lanes[family] += 1
        for spec in job.get("slot_dataset_plan") or []:
            if not isinstance(spec, dict):
                continue
            dataset_id = str(spec.get("dataset_id") or "")
            if dataset_id:
                datasets[dataset_id] += 1
            tier = str(spec.get("dataset_novelty_tier") or "")
            if tier:
                tiers[tier] += 1
    print("jobs", len(jobs), "templates", payload.get("template_count"))
    print("field_clusters", dict(clusters))
    print("datasets", dict(datasets))
    print("dataset_tiers", dict(tiers))
    print("family_lanes", dict(family_lanes))
    for job in jobs[:12]:
        print(
            "sample",
            job.get("name"),
            [
                (
                    spec.get("dataset_id"),
                    spec.get("dataset_name"),
                    spec.get("dataset_novelty_tier"),
                )
                for spec in (job.get("slot_dataset_plan") or [])
                if isinstance(spec, dict)
            ],
        )


if __name__ == "__main__":
    main()
