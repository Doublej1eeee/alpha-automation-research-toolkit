#!/usr/bin/env python
"""Sync platform name/category/tags/description/color from local results."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from brain_client import (  # noqa: E402
    CREDENTIALS_FILE,
    derive_auto_color,
    fetch_alpha_details,
    format_color_label,
    has_expected_grade_tag,
    iter_all_result_payloads,
    load_credentials,
    login,
    save_alpha_result,
    sync_alpha_properties,
    sync_auto_color,
)

def iter_target_results(only_null_name: bool, only_unsynced_color: bool) -> list[dict]:
    payloads = []
    for payload in iter_all_result_payloads():
        alpha_id = payload.get("alpha_id")
        alpha_details = payload.get("alpha_details") or {}
        if not alpha_id or not alpha_details:
            continue
        if alpha_details.get("status") == "SUBMITTED":
            continue
        if only_null_name and alpha_details.get("name"):
            continue
        if only_unsynced_color:
            local_rule_color = format_color_label(derive_auto_color(alpha_details))
            platform_color = format_color_label(alpha_details.get("color"))
            if local_rule_color == platform_color and has_expected_grade_tag(alpha_details):
                continue
        payloads.append(payload)
    payloads.sort(key=lambda item: item.get("alpha_id", ""), reverse=True)
    return payloads


def main() -> None:
    parser = argparse.ArgumentParser(description="Sync platform properties and color from local results.")
    parser.add_argument("--limit", type=int, default=50, help="How many result files to sync. Default: 50.")
    parser.add_argument(
        "--only-null-name",
        action="store_true",
        help="Only sync alphas whose platform name is currently null/blank in local snapshot.",
    )
    parser.add_argument(
        "--only-unsynced-color",
        action="store_true",
        help="Only sync alphas whose platform color does not match local derived color.",
    )
    args = parser.parse_args()

    result_payloads = iter_target_results(
        only_null_name=args.only_null_name,
        only_unsynced_color=args.only_unsynced_color,
    )
    if args.limit:
        result_payloads = result_payloads[: args.limit]

    print(f"Target result payloads: {len(result_payloads)}")
    if not result_payloads:
        return

    username, password = load_credentials(CREDENTIALS_FILE)
    session = login(username, password)

    for payload in result_payloads:
        alpha_id = payload["alpha_id"]
        config = {
            "name": payload.get("name"),
            "category": payload.get("category"),
            "tags": payload.get("tags"),
            "color": payload.get("color"),
            "description": payload.get("description"),
            "expression": payload.get("expression"),
            "settings": payload.get("settings"),
        }

        try:
            sync_alpha_properties(session, alpha_id, config)
            latest = fetch_alpha_details(session, alpha_id)
            merged = dict(payload.get("alpha_details") or {})
            if latest:
                merged.update(latest)
            rule_color = derive_auto_color(merged)
            sync_auto_color(session, alpha_id, merged)
            latest = fetch_alpha_details(session, alpha_id)
            save_alpha_result(
                alpha_id,
                config,
                latest,
                source_file=payload.get("source_file"),
                batch_name=payload.get("batch_name"),
                storage_mode=payload.get("storage_mode") or "light",
            )
            print(
                f"{alpha_id} | synced | "
                f"name={latest.get('name') or payload.get('name')} | "
                f"color={latest.get('color') or format_color_label(rule_color)}"
            )
        except Exception as exc:
            print(f"{alpha_id} | sync failed | {exc}")


if __name__ == "__main__":
    main()
