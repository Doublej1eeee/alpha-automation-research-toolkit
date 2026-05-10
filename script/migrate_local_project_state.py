#!/usr/bin/env python
"""Rebuild local result payloads and experience notes from current result sources."""

from __future__ import annotations

import json
import sys
from pathlib import Path


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from brain_client import (  # noqa: E402
    EXPERIENCE_NOTE_DIRS,
    EXPERIENCE_LOGS_DIR,
    derive_auto_color,
    format_color_label,
    iter_all_result_payloads,
    persist_alpha_state,
)


def clear_experience_notes() -> None:
    for directory in EXPERIENCE_NOTE_DIRS:
        for note_path in directory.glob("*.md"):
            note_path.unlink()
        gitkeep = directory / ".gitkeep"
        if not gitkeep.exists():
            gitkeep.write_text("", encoding="utf-8")
    if EXPERIENCE_LOGS_DIR.exists():
        for log_path in EXPERIENCE_LOGS_DIR.glob("*.jsonl"):
            log_path.unlink()


def build_config_from_payload(payload: dict) -> dict:
    return {
        "name": payload.get("name"),
        "category": payload.get("category"),
        "tags": payload.get("tags"),
        "color": payload.get("color"),
        "description": payload.get("description"),
        "expression": payload.get("expression"),
        "settings": payload.get("settings"),
    }


def migrate_one(payload: dict) -> str | None:
    alpha_id = payload.get("alpha_id")
    alpha_details = payload.get("alpha_details") or {}
    if not alpha_id or not alpha_details:
        return None

    config = build_config_from_payload(payload)
    persist_alpha_state(
        alpha_id,
        config,
        alpha_details,
        source_file=payload.get("source_file"),
        batch_name=payload.get("batch_name"),
        storage_mode=payload.get("storage_mode") or "light",
    )
    return format_color_label(derive_auto_color(alpha_details))


def main() -> None:
    clear_experience_notes()

    counts = {
        "RED": 0,
        "WHITE": 0,
        "YELLOW": 0,
        "GREEN": 0,
        "BLUE": 0,
        "PURPLE": 0,
    }

    migrated = 0
    for payload in iter_all_result_payloads():
        color = migrate_one(payload)
        if not color:
            continue
        migrated += 1
        counts[color] = counts.get(color, 0) + 1

    print(f"Migrated local result files: {migrated}")
    for color in ["RED", "WHITE", "YELLOW", "GREEN", "BLUE", "PURPLE"]:
        print(f" - {color}: {counts.get(color, 0)}")


if __name__ == "__main__":
    main()
