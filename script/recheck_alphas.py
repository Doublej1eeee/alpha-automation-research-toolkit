#!/usr/bin/env python
"""Recheck pending alpha /check results and rebuild local state."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path
from time import sleep


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from brain_client import (  # noqa: E402
    CREDENTIALS_FILE,
    derive_auto_color,
    format_color_label,
    get_check_buckets,
    has_expected_grade_tag,
    iter_all_result_payloads,
    load_credentials,
    login,
    persist_alpha_state,
    refresh_alpha_state,
    sync_auto_color,
)


def load_pending_candidates(results_dir: Path | None = None) -> list[dict]:
    candidates = []
    seen_alpha_ids: set[str] = set()
    for payload in iter_all_result_payloads():
        alpha_id = payload.get("alpha_id")
        alpha_details = payload.get("alpha_details") or {}
        if not alpha_id or not alpha_details:
            continue
        if alpha_id in seen_alpha_ids:
            continue

        _, failed, pending = get_check_buckets(alpha_details)
        derived_color = derive_auto_color(alpha_details)
        platform_color = alpha_details.get("color")
        unsynced_color = bool(
            derived_color
            and format_color_label(derived_color) != format_color_label(platform_color)
        )
        missing_grade_tag = bool(derived_color and not has_expected_grade_tag(alpha_details))
        if failed:
            continue
        if not pending and not unsynced_color and not missing_grade_tag:
            continue

        seen_alpha_ids.add(alpha_id)
        priority_bucket = 0 if pending else 1
        priority_fitness = -999 if pending else -998

        is_block = alpha_details.get("is") or {}
        candidates.append(
            {
                "alpha_id": alpha_id,
                "name": (payload.get("display") or {}).get("name") or payload.get("name") or alpha_id,
                "config": {
                    "name": payload.get("name"),
                    "category": payload.get("category"),
                    "tags": payload.get("tags"),
                    "color": payload.get("color"),
                    "description": payload.get("description"),
                    "expression": payload.get("expression"),
                    "settings": payload.get("settings"),
                },
                "source_file": payload.get("source_file"),
                "batch_name": payload.get("batch_name"),
                "storage_mode": payload.get("storage_mode") or "light",
                "needs_color_sync": unsynced_color,
                "needs_grade_tag_sync": missing_grade_tag,
                "platform_color": platform_color,
                "derived_color": format_color_label(derived_color),
                "pending_checks": len(pending),
                "priority_bucket": priority_bucket,
                "fitness": is_block.get("fitness") or -999,
                "sharpe": is_block.get("sharpe") or -999,
                "returns": is_block.get("returns") or -999,
                "turnover": is_block.get("turnover") or 999,
                "sort_fitness": is_block.get("fitness") or priority_fitness,
            }
        )

    candidates.sort(
        key=lambda item: (
            item["priority_bucket"],
            -item["sort_fitness"],
            -item["sharpe"],
            -item["returns"],
            item["turnover"],
            item["alpha_id"],
        )
    )
    return candidates


def main() -> None:
    parser = argparse.ArgumentParser(description="Recheck pending alphas using the safe /check API.")
    parser.add_argument("--limit", type=int, default=12, help="How many pending alphas to track.")
    parser.add_argument(
        "--alpha-id",
        action="append",
        default=[],
        help="Specific alpha id to recheck. Can be passed multiple times.",
    )
    parser.add_argument("--rounds", type=int, default=1, help="How many recheck rounds to run.")
    parser.add_argument("--sleep-seconds", type=int, default=60, help="Sleep seconds between rounds.")
    parser.add_argument(
        "--sync-platform-color",
        action="store_true",
        help="Also sync the derived color back to platform after each refresh.",
    )
    args = parser.parse_args()

    candidates = load_pending_candidates(None)
    if args.alpha_id:
        wanted = {alpha_id.strip() for alpha_id in args.alpha_id if alpha_id.strip()}
        candidates = [item for item in candidates if item["alpha_id"] in wanted]
    else:
        candidates = candidates[: args.limit]
    print(f"Pending candidates selected: {len(candidates)}")
    for item in candidates:
        print(
            f" - {item['alpha_id']} | {item['name']} | "
            f"Sharpe={item['sharpe']} | Fitness={item['fitness']} | "
            f"Pending={item['pending_checks']} | ColorSync={item['needs_color_sync']}"
        )
    if not candidates:
        return

    username, password = load_credentials(CREDENTIALS_FILE)
    session = login(username, password)
    unresolved = {item["alpha_id"]: item for item in candidates}

    for round_index in range(1, args.rounds + 1):
        print("=" * 70)
        print(f"Round {round_index}/{args.rounds} | unresolved={len(unresolved)}")
        resolved_ids = []

        for alpha_id, item in list(unresolved.items()):
            merged_alpha_details, _ = refresh_alpha_state(session, alpha_id, check_retries=2, check_retry_delay_seconds=8)
            if args.sync_platform_color:
                sync_auto_color(session, alpha_id, merged_alpha_details)
                merged_alpha_details, _ = refresh_alpha_state(session, alpha_id, check_retries=0, check_retry_delay_seconds=8)

            persist_alpha_state(
                alpha_id,
                item["config"],
                merged_alpha_details,
                source_file=item["source_file"],
                batch_name=item["batch_name"],
                storage_mode=item["storage_mode"],
            )

            _, failed, pending = get_check_buckets(merged_alpha_details)
            color = format_color_label(derive_auto_color(merged_alpha_details))
            platform_color = format_color_label(merged_alpha_details.get("color"))

            if failed:
                print(
                    f"{alpha_id} | {item['name']} | {color} | "
                    f"failed={','.join(check.get('name', '') for check in failed)}"
                )
                resolved_ids.append(alpha_id)
            elif not pending:
                print(f"{alpha_id} | {item['name']} | {color} | platform={platform_color}")
                resolved_ids.append(alpha_id)
            else:
                print(
                    f"{alpha_id} | {item['name']} | WHITE | "
                    f"still pending | platform={platform_color}"
                )

        for alpha_id in resolved_ids:
            unresolved.pop(alpha_id, None)

        if not unresolved or round_index == args.rounds:
            break
        sleep(args.sleep_seconds)

    print("=" * 70)
    print(f"Still pending: {len(unresolved)}")
    for alpha_id, item in unresolved.items():
        print(f"PENDING | {alpha_id} | {item['name']}")


if __name__ == "__main__":
    main()
