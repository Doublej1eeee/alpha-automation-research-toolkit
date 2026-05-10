#!/usr/bin/env python
"""Fetch and archive WorldQuant BRAIN community/forum pages for later study.

This is a conservative authenticated crawler:
- reuses the existing BRAIN login flow
- only crawls the same host
- follows HTML links up to a page limit
- saves raw html + extracted text + a scored index

It is meant to build a reusable local knowledge base, not to spam the site.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from collections import Counter, deque
from html.parser import HTMLParser
from pathlib import Path
from typing import Iterable
from urllib.parse import urljoin, urlparse

import requests

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from brain_client import CREDENTIALS_FILE, load_credentials, login  # noqa: E402


KNOWLEDGE_DIR = ROOT_DIR / "memory" / "learning_sources" / "brain_forum"
RAW_DIR = KNOWLEDGE_DIR / "raw_html"
TEXT_DIR = KNOWLEDGE_DIR / "texts"
INDEX_DIR = KNOWLEDGE_DIR / "indexes"

DEFAULT_SEEDS = [
    "https://platform.worldquantbrain.com/community",
    "https://platform.worldquantbrain.com/learn",
    "https://support.worldquantbrain.com/hc/en-us/community/topics",
    "https://support.worldquantbrain.com/hc/en-us/categories",
]

DEFAULT_ALLOWED_PREFIXES = [
    "/community",
    "/learn",
    "/discussion",
    "/forum",
    "/hc/en-us/community",
    "/hc/en-us/articles",
    "/hc/en-us/categories",
]

DEFAULT_ALLOWED_HOSTS = {
    "platform.worldquantbrain.com",
    "support.worldquantbrain.com",
}

USEFUL_KEYWORDS = [
    "alpha",
    "sharpe",
    "fitness",
    "turnover",
    "drawdown",
    "margin",
    "self correlation",
    "correlation",
    "submission",
    "submit",
    "neutralization",
    "decay",
    "truncation",
    "operator",
    "datafield",
    "dataset",
    "template",
    "signal",
    "factor",
    "rank",
    "ts_rank",
    "group_rank",
    "brain",
    "worldquant",
]


class LinkAndTextExtractor(HTMLParser):
    """Very small HTML extractor without extra dependencies."""

    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []
        self.text_parts: list[str] = []
        self.title_parts: list[str] = []
        self._in_script = False
        self._in_style = False
        self._in_title = False

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag == "script":
            self._in_script = True
        elif tag == "style":
            self._in_style = True
        elif tag == "title":
            self._in_title = True

        if tag == "a":
            for key, value in attrs:
                if key == "href" and value:
                    self.links.append(value)

    def handle_endtag(self, tag: str) -> None:
        if tag == "script":
            self._in_script = False
        elif tag == "style":
            self._in_style = False
        elif tag == "title":
            self._in_title = False

    def handle_data(self, data: str) -> None:
        if self._in_script or self._in_style:
            return
        text = " ".join(data.split())
        if not text:
            return
        self.text_parts.append(text)
        if self._in_title:
            self.title_parts.append(text)

    @property
    def title(self) -> str:
        return " ".join(self.title_parts).strip()

    @property
    def text(self) -> str:
        return "\n".join(self.text_parts).strip()


def ensure_dirs() -> None:
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    TEXT_DIR.mkdir(parents=True, exist_ok=True)
    INDEX_DIR.mkdir(parents=True, exist_ok=True)


def normalize_url(url: str) -> str:
    parsed = urlparse(url)
    normalized = parsed._replace(fragment="")
    return normalized.geturl()


def is_allowed_url(url: str, allowed_hosts: set[str], allowed_prefixes: Iterable[str]) -> bool:
    parsed = urlparse(url)
    if parsed.scheme not in {"http", "https"}:
        return False
    if parsed.netloc not in allowed_hosts:
        return False
    if not any(parsed.path.startswith(prefix) for prefix in allowed_prefixes):
        return False
    return True


def url_to_slug(url: str) -> str:
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()[:12]
    parsed = urlparse(url)
    path = parsed.path.strip("/").replace("/", "__") or "root"
    safe = "".join(ch if ch.isalnum() or ch in {"_", "-"} else "_" for ch in path)
    return f"{safe}_{digest}"


def score_text(text: str) -> tuple[int, dict[str, int]]:
    lowered = text.lower()
    counts = Counter()
    for keyword in USEFUL_KEYWORDS:
        hit = lowered.count(keyword)
        if hit:
            counts[keyword] = hit
    score = sum(counts.values())
    return score, dict(counts)


def fetch_page(session: requests.Session, url: str, timeout: int) -> tuple[str, str]:
    response = session.get(url, timeout=timeout)
    response.raise_for_status()
    return response.text, response.headers.get("Content-Type", "")


def save_page_assets(url: str, html: str, title: str, text: str, content_type: str) -> dict:
    slug = url_to_slug(url)
    raw_path = RAW_DIR / f"{slug}.html"
    text_path = TEXT_DIR / f"{slug}.txt"
    meta_path = INDEX_DIR / f"{slug}.json"

    score, keyword_hits = score_text(text)

    raw_path.write_text(html, encoding="utf-8")
    text_path.write_text(text, encoding="utf-8")
    metadata = {
        "url": url,
        "slug": slug,
        "title": title,
        "content_type": content_type,
        "score": score,
        "keyword_hits": keyword_hits,
        "raw_path": str(raw_path.relative_to(ROOT_DIR)),
        "text_path": str(text_path.relative_to(ROOT_DIR)),
    }
    meta_path.write_text(json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8")
    return metadata


def is_shell_page(text: str) -> bool:
    lowered = text.lower()
    return (
        "enable javascript to run this app" in lowered
        or lowered.strip() == "worldquant brain"
    )


def crawl(
    session: requests.Session,
    seeds: list[str],
    allowed_hosts: set[str],
    allowed_prefixes: list[str],
    max_pages: int,
    delay_seconds: float,
    timeout: int,
) -> list[dict]:
    queue = deque(normalize_url(url) for url in seeds)
    seen: set[str] = set()
    records: list[dict] = []

    while queue and len(records) < max_pages:
        url = queue.popleft()
        if url in seen:
            continue
        seen.add(url)

        if not is_allowed_url(url, allowed_hosts, allowed_prefixes):
            continue

        try:
            html, content_type = fetch_page(session, url, timeout=timeout)
        except Exception as exc:
            records.append(
                {
                    "url": url,
                    "error": str(exc),
                    "score": 0,
                    "keyword_hits": {},
                }
            )
            time.sleep(delay_seconds)
            continue

        if "html" not in content_type.lower():
            records.append(
                {
                    "url": url,
                    "content_type": content_type,
                    "skipped": "non-html",
                    "score": 0,
                    "keyword_hits": {},
                }
            )
            time.sleep(delay_seconds)
            continue

        extractor = LinkAndTextExtractor()
        extractor.feed(html)

        record = save_page_assets(
            url=url,
            html=html,
            title=extractor.title,
            text=extractor.text,
            content_type=content_type,
        )
        if is_shell_page(extractor.text):
            record["shell_page"] = True
            record["score"] = 0
            record["keyword_hits"] = {}
        records.append(record)

        for href in extractor.links:
            absolute = normalize_url(urljoin(url, href))
            if absolute not in seen and is_allowed_url(absolute, allowed_hosts, allowed_prefixes):
                queue.append(absolute)

        time.sleep(delay_seconds)

    return records


def load_seed_file(path: Path) -> list[str]:
    urls = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        urls.append(line)
    return urls


def save_run_index(records: list[dict], run_name: str) -> Path:
    INDEX_DIR.mkdir(parents=True, exist_ok=True)
    path = INDEX_DIR / f"{run_name}.json"
    ranked = sorted(records, key=lambda row: row.get("score", 0), reverse=True)
    path.write_text(json.dumps(ranked, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch and archive BRAIN community/forum pages.")
    parser.add_argument("--seed-url", action="append", help="Seed URL. Can be passed multiple times.")
    parser.add_argument("--seed-file", help="Text file with one seed URL per line.")
    parser.add_argument("--max-pages", type=int, default=20, help="Maximum pages to crawl. Default: 20.")
    parser.add_argument("--delay-seconds", type=float, default=1.5, help="Delay between requests. Default: 1.5.")
    parser.add_argument("--timeout", type=int, default=20, help="Request timeout in seconds. Default: 20.")
    parser.add_argument("--name", default="community_run", help="Output index filename stem.")
    parser.add_argument(
        "--allow-prefix",
        action="append",
        help="Allowed path prefix such as /community or /learn. Can be used multiple times.",
    )
    args = parser.parse_args()

    ensure_dirs()

    seeds = list(args.seed_url or [])
    if args.seed_file:
        seeds.extend(load_seed_file(Path(args.seed_file)))
    if not seeds:
        seeds = list(DEFAULT_SEEDS)

    allowed_prefixes = args.allow_prefix or list(DEFAULT_ALLOWED_PREFIXES)
    allowed_hosts = set(DEFAULT_ALLOWED_HOSTS)

    username, password = load_credentials(CREDENTIALS_FILE)
    session = login(username, password)

    records = crawl(
        session=session,
        seeds=seeds,
        allowed_hosts=allowed_hosts,
        allowed_prefixes=allowed_prefixes,
        max_pages=args.max_pages,
        delay_seconds=args.delay_seconds,
        timeout=args.timeout,
    )

    index_path = save_run_index(records, args.name)
    print(f"Crawled records: {len(records)}")
    print(f"Index: {index_path.relative_to(ROOT_DIR)}")
    top = sorted(records, key=lambda row: row.get("score", 0), reverse=True)[:10]
    if top:
        print("Top useful pages:")
        for row in top:
            print(f" - score={row.get('score', 0)} | {row.get('title', '')} | {row['url']}")


if __name__ == "__main__":
    main()
