#!/usr/bin/env python
"""Fetch official BRAIN learning knowledge from authenticated JSON APIs.

This script builds a reusable local knowledge base from the structured APIs
that are exposed to logged-in users, instead of scraping SPA HTML shells.
"""

from __future__ import annotations

import argparse
import json
import sys
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from brain_client import CREDENTIALS_FILE, load_credentials, login  # noqa: E402


BASE_DIR = ROOT_DIR / "memory" / "learning_sources" / "tool_docs"
TUTORIALS_DIR = BASE_DIR / "tutorials"
TUTORIAL_PAGES_DIR = BASE_DIR / "tutorial_pages"
OPERATORS_DIR = BASE_DIR / "operators"
INDEX_DIR = BASE_DIR / "indexes"


class PlainTextHTMLParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.parts: list[str] = []

    def handle_data(self, data: str) -> None:
        text = " ".join(data.split())
        if text:
            self.parts.append(text)

    @property
    def text(self) -> str:
        return "\n".join(self.parts).strip()


def ensure_dirs() -> None:
    for path in [TUTORIALS_DIR, TUTORIAL_PAGES_DIR, OPERATORS_DIR, INDEX_DIR]:
        path.mkdir(parents=True, exist_ok=True)


def fetch_json(session, url: str) -> Any:
    response = session.get(url, timeout=30)
    response.raise_for_status()
    return response.json()


def extract_text_from_html(html: str) -> str:
    parser = PlainTextHTMLParser()
    parser.feed(html)
    return parser.text


def render_content_blocks(content_blocks: list[dict[str, Any]]) -> str:
    lines: list[str] = []
    for block in content_blocks:
        block_type = block.get("type")
        value = block.get("value")
        if block_type == "TEXT" and isinstance(value, str):
            text = extract_text_from_html(value)
            if text:
                lines.append(text)
        elif block_type == "HEADING" and isinstance(value, dict):
            heading = str(value.get("content", "")).strip()
            level = str(value.get("level", "1")).strip()
            if heading:
                prefix = "#" * max(1, min(int(level or "1"), 6))
                lines.append(f"{prefix} {heading}")
        elif block_type == "IMAGE":
            lines.append("[IMAGE]")
        elif block_type == "VIDEO":
            lines.append("[VIDEO]")
        elif isinstance(value, str):
            text = extract_text_from_html(value)
            if text:
                lines.append(text)
    return "\n\n".join(line for line in lines if line).strip()


def fetch_tutorial_catalog(session) -> list[dict[str, Any]]:
    tutorials = fetch_json(session, "https://api.worldquantbrain.com/tutorials?limit=100")
    return tutorials.get("results", [])


def fetch_tutorial_page(session, page_id: str) -> dict[str, Any]:
    return fetch_json(session, f"https://api.worldquantbrain.com/tutorial-pages/{page_id}")


def fetch_operators(session) -> list[dict[str, Any]]:
    return fetch_json(session, "https://api.worldquantbrain.com/operators")


def save_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def save_text(path: Path, text: str) -> None:
    path.write_text(text, encoding="utf-8")


def build_tutorial_knowledge(session, limit_pages: int | None = None) -> dict[str, Any]:
    tutorials = fetch_tutorial_catalog(session)
    save_json(TUTORIALS_DIR / "catalog.json", tutorials)

    page_summaries: list[dict[str, Any]] = []
    fetched = 0
    for tutorial in tutorials:
        tutorial_id = tutorial.get("id")
        tutorial_title = tutorial.get("title")
        tutorial_group_dir = TUTORIAL_PAGES_DIR / str(tutorial_id)
        tutorial_group_dir.mkdir(parents=True, exist_ok=True)

        for page in tutorial.get("pages", []):
            if limit_pages is not None and fetched >= limit_pages:
                break
            page_id = page["id"]
            page_payload = fetch_tutorial_page(session, page_id)
            rendered = render_content_blocks(page_payload.get("content", []))

            save_json(tutorial_group_dir / f"{page_id}.json", page_payload)
            save_text(tutorial_group_dir / f"{page_id}.md", rendered)

            page_summaries.append(
                {
                    "tutorial_id": tutorial_id,
                    "tutorial_title": tutorial_title,
                    "page_id": page_id,
                    "page_title": page_payload.get("title"),
                    "content_blocks": len(page_payload.get("content", [])),
                    "text_length": len(rendered),
                    "json_path": str((tutorial_group_dir / f"{page_id}.json").relative_to(ROOT_DIR)),
                    "text_path": str((tutorial_group_dir / f"{page_id}.md").relative_to(ROOT_DIR)),
                }
            )
            fetched += 1
        if limit_pages is not None and fetched >= limit_pages:
            break

    summary = {
        "tutorial_count": len(tutorials),
        "page_count": len(page_summaries),
        "pages": page_summaries,
    }
    save_json(INDEX_DIR / "tutorial_pages_index.json", summary)
    return summary


def build_operator_knowledge(session) -> dict[str, Any]:
    operators = fetch_operators(session)
    save_json(OPERATORS_DIR / "operators.json", operators)

    by_category: dict[str, list[dict[str, Any]]] = {}
    for operator in operators:
        by_category.setdefault(operator.get("category", "UNKNOWN"), []).append(operator)

    category_index = []
    for category, entries in sorted(by_category.items()):
        slug = category.lower().replace(" ", "_").replace("/", "_")
        save_json(OPERATORS_DIR / f"{slug}.json", entries)
        category_index.append(
            {
                "category": category,
                "count": len(entries),
                "path": str((OPERATORS_DIR / f"{slug}.json").relative_to(ROOT_DIR)),
            }
        )

    summary = {
        "operator_count": len(operators),
        "categories": category_index,
    }
    save_json(INDEX_DIR / "operators_index.json", summary)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch official BRAIN tutorial/operator knowledge.")
    parser.add_argument("--limit-pages", type=int, help="Only fetch the first N tutorial pages.")
    args = parser.parse_args()

    ensure_dirs()
    username, password = load_credentials(CREDENTIALS_FILE)
    session = login(username, password)

    tutorial_summary = build_tutorial_knowledge(session, limit_pages=args.limit_pages)
    operator_summary = build_operator_knowledge(session)

    print(f"Tutorial groups: {tutorial_summary['tutorial_count']}")
    print(f"Tutorial pages fetched: {tutorial_summary['page_count']}")
    print(f"Operators fetched: {operator_summary['operator_count']}")
    print(f"Knowledge root: {BASE_DIR.relative_to(ROOT_DIR)}")


if __name__ == "__main__":
    main()
