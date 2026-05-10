#!/usr/bin/env python
"""Incrementally fetch USA research resources for raw-alpha discovery.

Active sources only:
- papers: arXiv q-fin full-text papers
- reports: configured USA company full-text report/news sources

Storage:
- formal fetched resources -> memory/learning_sources/*
- runtime state -> temp/us_research_sync/*
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import os
import html
import json
from pathlib import Path
import re
import sys
import time
from typing import Any
import unicodedata
import xml.etree.ElementTree as ET

import requests
from pypdf import PdfReader


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from script.extract_research_alpha_families import run as extract_research_families  # noqa: E402


USER_AGENT = "learning-us-research-sync/1.0 contact@example.com"

CRAWLER_ROOT = ROOT_DIR / "crawler" / "usa_research"
SOURCE_CONFIG_PATH = CRAWLER_ROOT / "sources.json"

MEMORY_ROOT = ROOT_DIR / "memory" / "learning_sources"
PAPERS_ROOT = MEMORY_ROOT / "quant_papers" / "usa_equity"
REPORTS_ROOT = MEMORY_ROOT / "research_reports" / "usa_equity"

PAPERS_METADATA_DIR = PAPERS_ROOT / "metadata"
PAPERS_PDF_DIR = PAPERS_ROOT / "pdfs"
PAPERS_TEXT_DIR = PAPERS_ROOT / "texts"
PAPERS_INDEX_DIR = PAPERS_ROOT / "indexes"

REPORT_COMPANY_NEWS_ROOT = REPORTS_ROOT / "company_news"
REPORT_COMPANY_NEWS_METADATA_DIR = REPORT_COMPANY_NEWS_ROOT / "metadata"
REPORT_COMPANY_NEWS_RAW_DIR = REPORT_COMPANY_NEWS_ROOT / "raw"
REPORT_COMPANY_NEWS_INDEX_DIR = REPORT_COMPANY_NEWS_ROOT / "indexes"

REPORT_SEC_ROOT = REPORTS_ROOT / "sec_filings"
REPORT_SEC_METADATA_DIR = REPORT_SEC_ROOT / "metadata"
REPORT_SEC_RAW_DIR = REPORT_SEC_ROOT / "raw"
REPORT_SEC_INDEX_DIR = REPORT_SEC_ROOT / "indexes"

TEMP_ROOT = ROOT_DIR / "temp" / "us_research_sync"
STATE_PATH = TEMP_ROOT / "sync_state.json"
SUMMARY_PATH = TEMP_ROOT / "latest_summary.json"
PROCESSED_SOURCES_PATH = ROOT_DIR / "memory" / "processed_research_sources.jsonl"

ARXIV_API_URL = "https://export.arxiv.org/api/query"
ARXIV_RSS_URL = "https://rss.arxiv.org/rss/q-fin"
ARXIV_CATEGORY_QUERY = "cat:q-fin.*"

SEC_COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
SEC_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
SEC_ARCHIVES_URL = "https://www.sec.gov/Archives/edgar/data/{cik_no_zero}/{accession_no_dash}/{primary_document}"
SEC_FORMS = {"10-K", "10-Q", "8-K"}
SEC_TICKERS = [
    "AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "TSLA", "AVGO", "JPM", "LLY",
    "V", "UNH", "XOM", "MA", "COST", "WMT", "HD", "PG", "JNJ", "BAC",
    "NFLX", "CRM", "ORCL", "AMD", "ADBE", "KO", "PEP", "CSCO", "CVX", "MRK",
    "PFE", "TMO", "ABT", "MCD", "DIS", "NKE", "INTC", "QCOM", "TXN", "IBM",
]

FINANCE_HINTS = {
    "finance",
    "financial",
    "market",
    "markets",
    "trading",
    "equity",
    "earnings",
    "analyst",
    "stock",
    "stocks",
    "return",
    "returns",
    "portfolio",
    "asset",
    "assets",
    "investment",
    "investor",
    "anomaly",
    "sentiment",
    "credit",
    "valuation",
}

REPORT_SIGNAL_HINTS = {
    "earnings",
    "results",
    "guidance",
    "outlook",
    "revenue",
    "margin",
    "dividend",
    "repurchase",
    "buyback",
    "chief executive officer",
    "ceo",
    "chief financial officer",
    "cfo",
    "board",
    "annual meeting",
    "quarter",
    "conference call",
}

PDF_GARBLED_REPLACEMENTS = {
    "閳?": "-",
    "閳?": '"',
    "閳?": '"',
    "閳?": "'",
    "閳?": "'",
    "閳?": "-",
    "閿熸枻鎷?": " ",
}

COMPANY_REPORT_MIN_TEXT_LENGTH = 1200
PAPER_MIN_TEXT_LENGTH = 1800


@dataclass
class SyncSummary:
    started_at: str
    finished_at: str
    new_arxiv_papers: int
    new_company_reports: int
    accepted_alpha_families: int
    source_errors: dict[str, str]


@dataclass
class ReportSource:
    provider: str
    source_id: str
    mode: str
    url: str


def ensure_directories() -> None:
    for path in [
        PAPERS_METADATA_DIR,
        PAPERS_PDF_DIR,
        PAPERS_TEXT_DIR,
        PAPERS_INDEX_DIR,
        REPORT_COMPANY_NEWS_METADATA_DIR,
        REPORT_COMPANY_NEWS_RAW_DIR,
        REPORT_COMPANY_NEWS_INDEX_DIR,
        REPORT_SEC_METADATA_DIR,
        REPORT_SEC_RAW_DIR,
        REPORT_SEC_INDEX_DIR,
        TEMP_ROOT,
    ]:
        path.mkdir(parents=True, exist_ok=True)


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def iso_now() -> str:
    return utc_now().replace(microsecond=0).isoformat()


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def save_json(path: Path, payload: Any) -> None:
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def load_state() -> dict[str, Any]:
    state = load_json(
        STATE_PATH,
        {
            "last_run_at": None,
            "arxiv_seen_ids": [],
            "company_report_seen_ids": [],
        },
    )
    state.setdefault("arxiv_seen_ids", [])
    state.setdefault("company_report_seen_ids", [])
    state.setdefault("sec_seen_ids", [])
    apply_processed_sources_to_state(state)
    return state


def apply_processed_sources_to_state(state: dict[str, Any]) -> None:
    if not PROCESSED_SOURCES_PATH.exists():
        return
    arxiv_seen = set(state.setdefault("arxiv_seen_ids", []))
    company_seen = set(state.setdefault("company_report_seen_ids", []))
    sec_seen = set(state.setdefault("sec_seen_ids", []))
    with PROCESSED_SOURCES_PATH.open("r", encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            source_type = str(row.get("source_type") or "")
            source_id = str(row.get("source_id") or "")
            if not source_id:
                continue
            if source_type == "paper":
                arxiv_seen.add(source_id)
            elif source_type == "report":
                company_seen.add(source_id)
            elif source_type == "sec":
                sec_seen.add(source_id)
    state["arxiv_seen_ids"] = sorted(arxiv_seen)
    state["company_report_seen_ids"] = sorted(company_seen)
    state["sec_seen_ids"] = sorted(sec_seen)


def create_session() -> requests.Session:
    sess = requests.Session()
    sess.headers.update(
        {
            "User-Agent": build_user_agent(),
            "Accept": "application/json, text/xml, application/atom+xml;q=0.9, */*;q=0.8",
        }
    )
    return sess


def build_user_agent() -> str:
    return os.environ.get("SEC_USER_AGENT") or USER_AGENT


def safe_slug(value: str) -> str:
    slug = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    return slug[:180] or "untitled"


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def normalize_text_content(text: str) -> str:
    text = unicodedata.normalize("NFKC", text)
    text = html.unescape(text)
    for bad, good in PDF_GARBLED_REPLACEMENTS.items():
        text = text.replace(bad, good)
    text = text.replace("\x00", " ")
    text = text.replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def normalize_keyword_text(text: str) -> str:
    text = normalize_text_content(text).lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def text_quality_stats(text: str) -> dict[str, float]:
    total = max(len(text), 1)
    weird = sum(1 for ch in text if ch in {"閳?", "閿?", "锟?"})
    alpha = sum(1 for ch in text if ("A" <= ch <= "Z") or ("a" <= ch <= "z"))
    newline_count = text.count("\n")
    return {
        "length": float(len(text)),
        "weird_ratio": weird / total,
        "alpha_ratio": alpha / total,
        "newline_ratio": newline_count / total,
    }


def is_usable_full_text(text: str, *, min_length: int) -> bool:
    stats = text_quality_stats(text)
    if stats["length"] < min_length:
        return False
    if stats["weird_ratio"] > 0.02:
        return False
    if stats["alpha_ratio"] < 0.45:
        return False
    return True


def extract_pdf_text_basic(pdf_bytes: bytes) -> str:
    try:
        import io

        reader = PdfReader(io.BytesIO(pdf_bytes))
        pages: list[str] = []
        for page in reader.pages[:40]:
            text = page.extract_text() or ""
            text = normalize_text_content(text)
            if text:
                pages.append(text)
        extracted = "\n\n".join(pages).strip()
        if extracted:
            return extracted
    except Exception:
        pass

    decoded = pdf_bytes.decode("latin-1", errors="ignore")
    matches = re.findall(r"\(([^()]{20,2000})\)", decoded)
    lines: list[str] = []
    for match in matches:
        text = match.replace("\\n", " ").replace("\\r", " ").replace("\\t", " ")
        text = re.sub(r"\\[0-7]{1,3}", " ", text)
        text = re.sub(r"\\.", "", text)
        text = normalize_text_content(" ".join(text.split()))
        if len(text) >= 20:
            lines.append(text)
    seen: set[str] = set()
    unique_lines: list[str] = []
    for line in lines:
        if line in seen:
            continue
        seen.add(line)
        unique_lines.append(line)
    return normalize_text_content("\n".join(unique_lines[:400]))


def save_pdf_and_text(stem: str, response: requests.Response) -> bool:
    content_type = response.headers.get("Content-Type", "")
    if not response.ok or "application/pdf" not in content_type:
        return False
    pdf_path = PAPERS_PDF_DIR / f"{stem}.pdf"
    pdf_path.write_bytes(response.content)
    extracted_text = extract_pdf_text_basic(response.content)
    if is_usable_full_text(extracted_text, min_length=PAPER_MIN_TEXT_LENGTH):
        (PAPERS_TEXT_DIR / f"{stem}.txt").write_text(extracted_text, encoding="utf-8")
        return True
    return False


def should_download_arxiv_full_text(title: str, summary: str, categories: list[str]) -> bool:
    categories_lower = [str(item).lower() for item in categories]
    if any(category.startswith("q-fin") for category in categories_lower):
        return True
    text = f"{title} {summary}".lower()
    positive = sum(1 for hint in FINANCE_HINTS if hint in text)
    return positive >= 3


def fetch_arxiv_papers(sess: requests.Session, state: dict[str, Any], lookback_days: int) -> int:
    seen_ids = set(state["arxiv_seen_ids"])
    start = utc_now() - timedelta(days=lookback_days)
    start_token = start.strftime("%Y%m%d0000")
    query = f"{ARXIV_CATEGORY_QUERY} AND submittedDate:[{start_token} TO *]"
    params = {
        "search_query": query,
        "sortBy": "submittedDate",
        "sortOrder": "descending",
        "start": 0,
        "max_results": 50,
    }
    response = sess.get(ARXIV_API_URL, params=params, timeout=60)
    response.raise_for_status()

    ns = {"atom": "http://www.w3.org/2005/Atom"}
    root = ET.fromstring(response.text)
    new_count = 0
    catalog_path = PAPERS_INDEX_DIR / "arxiv_catalog.jsonl"

    for entry in root.findall("atom:entry", ns):
        paper_id = (entry.findtext("atom:id", default="", namespaces=ns) or "").strip().split("/")[-1]
        if not paper_id or paper_id in seen_ids:
            continue
        title = " ".join((entry.findtext("atom:title", default="", namespaces=ns) or "").split())
        summary = " ".join((entry.findtext("atom:summary", default="", namespaces=ns) or "").split())
        published = entry.findtext("atom:published", default="", namespaces=ns) or ""
        updated = entry.findtext("atom:updated", default="", namespaces=ns) or ""
        categories = [node.attrib.get("term", "") for node in entry.findall("atom:category", ns)]
        pdf_url = None
        for link in entry.findall("atom:link", ns):
            if link.attrib.get("title") == "pdf":
                pdf_url = link.attrib.get("href")
                break

        record = {
            "source": "arxiv",
            "id": paper_id,
            "title": title,
            "summary": summary,
            "published": published,
            "updated": updated,
            "categories": categories,
            "pdf_url": pdf_url,
            "fetched_at": iso_now(),
        }

        save_json(PAPERS_METADATA_DIR / f"arxiv_{safe_slug(paper_id)}.json", record)
        append_jsonl(catalog_path, record)

        if pdf_url and should_download_arxiv_full_text(title, summary, categories):
            pdf_response = sess.get(pdf_url, timeout=120)
            save_pdf_and_text(f"arxiv_{safe_slug(paper_id)}", pdf_response)

        seen_ids.add(paper_id)
        new_count += 1

    state["arxiv_seen_ids"] = sorted(seen_ids)
    return new_count


def count_usable_paper_texts() -> int:
    if not PAPERS_TEXT_DIR.exists():
        return 0
    count = 0
    for path in PAPERS_TEXT_DIR.glob("*.txt"):
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        if is_usable_full_text(text, min_length=PAPER_MIN_TEXT_LENGTH):
            count += 1
    return count


def fetch_arxiv_papers_to_target(
    sess: requests.Session,
    state: dict[str, Any],
    *,
    target_texts: int,
    max_scan: int,
) -> int:
    if target_texts <= 0 or count_usable_paper_texts() >= target_texts:
        return 0
    seen_ids = set(state["arxiv_seen_ids"])
    new_count = 0
    catalog_path = PAPERS_INDEX_DIR / "arxiv_catalog.jsonl"
    start = 0
    page_size = 100

    while start < max_scan and count_usable_paper_texts() < target_texts:
        params = {
            "search_query": ARXIV_CATEGORY_QUERY,
            "sortBy": "submittedDate",
            "sortOrder": "descending",
            "start": start,
            "max_results": min(page_size, max_scan - start),
        }
        response = sess.get(ARXIV_API_URL, params=params, timeout=60)
        response.raise_for_status()
        ns = {"atom": "http://www.w3.org/2005/Atom"}
        root = ET.fromstring(response.text)
        entries = root.findall("atom:entry", ns)
        if not entries:
            break

        for entry in entries:
            if count_usable_paper_texts() >= target_texts:
                break
            paper_id = (entry.findtext("atom:id", default="", namespaces=ns) or "").strip().split("/")[-1]
            if not paper_id:
                continue
            title = " ".join((entry.findtext("atom:title", default="", namespaces=ns) or "").split())
            summary = " ".join((entry.findtext("atom:summary", default="", namespaces=ns) or "").split())
            published = entry.findtext("atom:published", default="", namespaces=ns) or ""
            updated = entry.findtext("atom:updated", default="", namespaces=ns) or ""
            categories = [node.attrib.get("term", "") for node in entry.findall("atom:category", ns)]
            pdf_url = None
            for link in entry.findall("atom:link", ns):
                if link.attrib.get("title") == "pdf":
                    pdf_url = link.attrib.get("href")
                    break
            record = {
                "source": "arxiv",
                "id": paper_id,
                "title": title,
                "summary": summary,
                "published": published,
                "updated": updated,
                "categories": categories,
                "pdf_url": pdf_url,
                "fetched_at": iso_now(),
            }
            save_json(PAPERS_METADATA_DIR / f"arxiv_{safe_slug(paper_id)}.json", record)
            append_jsonl(catalog_path, record)
            if pdf_url and should_download_arxiv_full_text(title, summary, categories):
                try:
                    pdf_response = sess.get(pdf_url, timeout=120)
                    if save_pdf_and_text(f"arxiv_{safe_slug(paper_id)}", pdf_response):
                        new_count += 1
                except Exception:
                    continue
            seen_ids.add(paper_id)
            time.sleep(0.5)
        start += page_size

    state["arxiv_seen_ids"] = sorted(seen_ids)
    return new_count


def fetch_arxiv_papers_via_rss(sess: requests.Session, state: dict[str, Any], lookback_days: int) -> int:
    seen_ids = set(state["arxiv_seen_ids"])
    threshold = utc_now() - timedelta(days=lookback_days)
    response = sess.get(ARXIV_RSS_URL, timeout=60)
    response.raise_for_status()

    root = ET.fromstring(response.text)
    channel = root.find("channel")
    if channel is None:
        return 0

    new_count = 0
    catalog_path = PAPERS_INDEX_DIR / "arxiv_catalog.jsonl"
    for item in channel.findall("item"):
        guid = (item.findtext("guid") or "").strip()
        if not guid:
            continue
        paper_id = guid.rstrip("/").split("/")[-1]
        if paper_id in seen_ids:
            continue
        pub_date = item.findtext("pubDate") or ""
        published_dt = None
        if pub_date:
            try:
                published_dt = datetime.strptime(pub_date, "%a, %d %b %Y %H:%M:%S %Z").replace(tzinfo=timezone.utc)
            except ValueError:
                published_dt = None
        if published_dt and published_dt < threshold:
            continue

        title = " ".join((item.findtext("title") or "").split())
        summary = " ".join((item.findtext("description") or "").split())
        link = item.findtext("link") or ""
        pdf_url = None
        if link:
            paper_id = link.rstrip("/").split("/")[-1]
            pdf_url = f"https://arxiv.org/pdf/{paper_id}.pdf"

        record = {
            "source": "arxiv",
            "id": paper_id,
            "title": title,
            "summary": summary,
            "published": published_dt.isoformat() if published_dt else pub_date,
            "updated": published_dt.isoformat() if published_dt else pub_date,
            "categories": ["q-fin"],
            "pdf_url": pdf_url,
            "fetched_at": iso_now(),
            "via": "rss",
        }

        save_json(PAPERS_METADATA_DIR / f"arxiv_{safe_slug(paper_id)}.json", record)
        append_jsonl(catalog_path, record)
        if pdf_url and should_download_arxiv_full_text(title, summary, ["q-fin"]):
            pdf_response = sess.get(pdf_url, timeout=120)
            save_pdf_and_text(f"arxiv_{safe_slug(paper_id)}", pdf_response)
        seen_ids.add(paper_id)
        new_count += 1

    state["arxiv_seen_ids"] = sorted(seen_ids)
    return new_count


def extract_html_text(html_text: str) -> str:
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html_text, "html.parser")
    for tag in soup(["script", "style", "noscript"]):
        tag.decompose()
    text = soup.get_text("\n")
    return normalize_text_content(text)


def is_relevant_company_report(title: str, body_text: str) -> bool:
    title_text = normalize_keyword_text(title)
    body_lower = normalize_keyword_text(body_text)
    title_hits = sum(1 for hint in REPORT_SIGNAL_HINTS if hint in title_text)
    body_hits = sum(1 for hint in REPORT_SIGNAL_HINTS if hint in body_lower)
    if title_hits >= 1:
        return True
    return body_hits >= 2


def load_active_report_sources() -> list[ReportSource]:
    payload = load_json(SOURCE_CONFIG_PATH, {"reports": []})
    sources: list[ReportSource] = []
    for item in payload.get("reports", []):
        if not item.get("full_text_supported"):
            continue
        if item.get("current_status") != "working":
            continue
        source_id = str(item.get("id") or "").strip()
        provider = str(item.get("provider") or source_id).strip()
        mode = str(item.get("mode") or "").strip()
        url = str(item.get("url") or "").strip()
        if not source_id or not provider or not mode or not url:
            continue
        sources.append(ReportSource(provider=provider, source_id=source_id, mode=mode, url=url))
    return sources


def article_title_from_html(article_html: str, fallback: str) -> str:
    title_match = re.search(r"<title>(.*?)</title>", article_html, re.I | re.S)
    title = title_match.group(1) if title_match else fallback
    return " ".join(title.split())


def persist_company_report(
    state: dict[str, Any],
    *,
    publisher_slug: str,
    article_id: str,
    title: str,
    article_url: str,
    published: str,
    body_text: str,
) -> int:
    seen_ids = set(state.get("company_report_seen_ids") or [])
    if article_id in seen_ids:
        return 0
    record = {
        "source": "company_news",
        "publisher": publisher_slug,
        "id": article_id,
        "title": title,
        "article_url": article_url,
        "published": published,
        "fetched_at": iso_now(),
    }
    catalog_path = REPORT_COMPANY_NEWS_INDEX_DIR / "company_news_catalog.jsonl"
    save_json(REPORT_COMPANY_NEWS_METADATA_DIR / f"{publisher_slug}_{article_id}.json", record)
    append_jsonl(catalog_path, record)
    (REPORT_COMPANY_NEWS_RAW_DIR / f"{publisher_slug}_{article_id}.txt").write_text(body_text, encoding="utf-8")
    seen_ids.add(article_id)
    state["company_report_seen_ids"] = sorted(seen_ids)
    return 1


def fetch_rss_report_source(sess: requests.Session, state: dict[str, Any], source: ReportSource, lookback_days: int) -> int:
    cutoff = utc_now() - timedelta(days=lookback_days)
    response = sess.get(source.url, timeout=60)
    response.raise_for_status()

    root = ET.fromstring(response.text)
    ns = {"atom": "http://www.w3.org/2005/Atom"}
    new_count = 0
    for entry in root.findall("atom:entry", ns):
        title = " ".join((entry.findtext("atom:title", default="", namespaces=ns) or "").split())
        link_node = entry.find("atom:link", ns)
        article_url = link_node.attrib.get("href") if link_node is not None else ""
        updated = entry.findtext("atom:updated", default="", namespaces=ns) or ""
        article_id = safe_slug(article_url or title)
        if not article_url:
            continue
        published_dt = None
        if updated:
            try:
                published_dt = datetime.fromisoformat(updated.replace("Z", "+00:00"))
            except ValueError:
                published_dt = None
        if published_dt and published_dt < cutoff:
            continue

        article_response = sess.get(article_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=120)
        if not article_response.ok:
            continue
        body_text = extract_html_text(article_response.text)
        if not is_usable_full_text(body_text, min_length=COMPANY_REPORT_MIN_TEXT_LENGTH):
            continue
        if not is_relevant_company_report(title, body_text):
            continue
        new_count += persist_company_report(
            state,
            publisher_slug=safe_slug(source.provider.lower()),
            article_id=article_id,
            title=title,
            article_url=article_url,
            published=updated,
            body_text=body_text,
        )
    return new_count


def fetch_sitemap_report_source(sess: requests.Session, state: dict[str, Any], source: ReportSource, lookback_days: int) -> int:
    cutoff = utc_now() - timedelta(days=lookback_days)
    response = sess.get(source.url, headers={"User-Agent": "Mozilla/5.0"}, timeout=60)
    response.raise_for_status()

    root = ET.fromstring(response.text)
    ns = {"sm": "http://www.sitemaps.org/schemas/sitemap/0.9"}
    sitemap_urls = sorted([loc.text for loc in root.findall(".//sm:loc", ns) if loc.text], reverse=True)[:4]

    new_count = 0
    for sitemap_url in sitemap_urls:
        sm_response = sess.get(sitemap_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=60)
        if not sm_response.ok:
            continue
        sm_root = ET.fromstring(sm_response.text)
        for url_node in sm_root.findall(".//sm:url", ns):
            article_url = url_node.findtext("sm:loc", default="", namespaces=ns) or ""
            lastmod = url_node.findtext("sm:lastmod", default="", namespaces=ns) or ""
            article_id = safe_slug(article_url)
            if not article_url:
                continue
            published_dt = None
            if lastmod:
                try:
                    published_dt = datetime.fromisoformat(lastmod.replace("Z", "+00:00"))
                except ValueError:
                    published_dt = None
            if published_dt and published_dt < cutoff:
                continue

            article_response = sess.get(article_url, headers={"User-Agent": "Mozilla/5.0"}, timeout=120)
            if not article_response.ok:
                continue
            body_text = extract_html_text(article_response.text)
            if not is_usable_full_text(body_text, min_length=COMPANY_REPORT_MIN_TEXT_LENGTH):
                continue
            title = article_title_from_html(article_response.text, article_url)
            if not is_relevant_company_report(title, body_text):
                continue
            new_count += persist_company_report(
                state,
                publisher_slug=safe_slug(source.provider.lower()),
                article_id=article_id,
                title=title,
                article_url=article_url,
                published=lastmod,
                body_text=body_text,
            )
    return new_count


def fetch_company_reports(sess: requests.Session, state: dict[str, Any], lookback_days: int) -> int:
    total = 0
    for source in load_active_report_sources():
        if source.mode == "rss_html":
            total += fetch_rss_report_source(sess, state, source, lookback_days)
        elif source.mode == "sitemap_html":
            total += fetch_sitemap_report_source(sess, state, source, lookback_days)
    return total


def count_sec_raw_reports() -> int:
    if not REPORT_SEC_RAW_DIR.exists():
        return 0
    return len([path for path in REPORT_SEC_RAW_DIR.glob("*.txt") if path.stat().st_size >= COMPANY_REPORT_MIN_TEXT_LENGTH])


def load_sec_company_tickers(sess: requests.Session) -> dict[str, str]:
    response = sess.get(SEC_COMPANY_TICKERS_URL, headers={"User-Agent": build_user_agent()}, timeout=60)
    response.raise_for_status()
    payload = response.json()
    tickers: dict[str, str] = {}
    for item in payload.values():
        ticker = str(item.get("ticker") or "").upper()
        cik = str(item.get("cik_str") or "").zfill(10)
        if ticker and cik:
            tickers[ticker] = cik
    return tickers


def sec_primary_document_url(cik: str, accession: str, primary_document: str) -> str:
    return SEC_ARCHIVES_URL.format(
        cik_no_zero=str(int(cik)),
        accession_no_dash=accession.replace("-", ""),
        primary_document=primary_document,
    )


def fetch_sec_reports_to_target(
    sess: requests.Session,
    state: dict[str, Any],
    *,
    target_reports: int,
    max_per_ticker: int,
) -> int:
    if target_reports <= 0 or count_sec_raw_reports() >= target_reports:
        return 0
    seen_ids = set(state.setdefault("sec_seen_ids", []))
    ticker_to_cik = load_sec_company_tickers(sess)
    new_count = 0
    catalog_path = REPORT_SEC_INDEX_DIR / "sec_catalog.jsonl"

    for ticker in SEC_TICKERS:
        if count_sec_raw_reports() >= target_reports:
            break
        cik = ticker_to_cik.get(ticker.upper())
        if not cik:
            continue
        try:
            response = sess.get(
                SEC_SUBMISSIONS_URL.format(cik=cik),
                headers={"User-Agent": build_user_agent()},
                timeout=60,
            )
            response.raise_for_status()
            submission = response.json()
        except Exception:
            continue
        recent = (submission.get("filings") or {}).get("recent") or {}
        forms = recent.get("form") or []
        accessions = recent.get("accessionNumber") or []
        primary_docs = recent.get("primaryDocument") or []
        filing_dates = recent.get("filingDate") or []
        accepted = 0

        for form, accession, primary_doc, filing_date in zip(forms, accessions, primary_docs, filing_dates):
            if count_sec_raw_reports() >= target_reports or accepted >= max_per_ticker:
                break
            form = str(form or "").upper()
            accession = str(accession or "")
            primary_doc = str(primary_doc or "")
            if form not in SEC_FORMS or not accession or not primary_doc:
                continue
            article_id = safe_slug(f"{ticker}_{form}_{accession}")
            if article_id in seen_ids:
                continue
            url = sec_primary_document_url(cik, accession, primary_doc)
            try:
                doc_response = sess.get(
                    url,
                    headers={"User-Agent": build_user_agent(), "Accept": "text/html,application/xhtml+xml,text/plain,*/*"},
                    timeout=120,
                )
            except Exception:
                continue
            if not doc_response.ok or not doc_response.text:
                continue
            body_text = extract_html_text(doc_response.text)
            if not is_usable_full_text(body_text, min_length=COMPANY_REPORT_MIN_TEXT_LENGTH):
                continue
            record = {
                "source": "sec",
                "id": article_id,
                "ticker": ticker,
                "cik": cik,
                "form": form,
                "accession": accession,
                "primary_document": primary_doc,
                "filing_date": filing_date,
                "filing_url": url,
                "title": f"{ticker} {form} filing {filing_date}",
                "fetched_at": iso_now(),
            }
            save_json(REPORT_SEC_METADATA_DIR / f"{article_id}.json", record)
            append_jsonl(catalog_path, record)
            (REPORT_SEC_RAW_DIR / f"{article_id}.txt").write_text(body_text, encoding="utf-8")
            seen_ids.add(article_id)
            accepted += 1
            new_count += 1
            time.sleep(0.3)

    state["sec_seen_ids"] = sorted(seen_ids)
    return new_count


def run_once(lookback_days: int, target_paper_texts: int = 0, target_sec_reports: int = 0) -> SyncSummary:
    ensure_directories()
    sess = create_session()
    state = load_state()
    started = iso_now()

    source_errors: dict[str, str] = {}
    try:
        new_arxiv = fetch_arxiv_papers(sess, state, lookback_days=lookback_days)
    except Exception as exc:
        try:
            new_arxiv = fetch_arxiv_papers_via_rss(sess, state, lookback_days=lookback_days)
            source_errors["arxiv"] = f"api_failed_then_rss_used: {exc}"
        except Exception as rss_exc:
            new_arxiv = 0
            source_errors["arxiv"] = f"api_failed: {exc}; rss_failed: {rss_exc}"

    if target_paper_texts > 0:
        try:
            new_arxiv += fetch_arxiv_papers_to_target(
                sess,
                state,
                target_texts=target_paper_texts,
                max_scan=max(target_paper_texts * 5, 300),
            )
        except Exception as exc:
            source_errors["arxiv_target"] = str(exc)

    try:
        new_company_reports = fetch_company_reports(sess, state, lookback_days=lookback_days)
    except Exception as exc:
        new_company_reports = 0
        source_errors["company_reports"] = str(exc)

    if target_sec_reports > 0:
        try:
            new_company_reports += fetch_sec_reports_to_target(
                sess,
                state,
                target_reports=target_sec_reports,
                max_per_ticker=max(3, target_sec_reports // max(len(SEC_TICKERS), 1) + 2),
            )
        except Exception as exc:
            source_errors["sec_reports"] = str(exc)

    state["last_run_at"] = iso_now()
    save_json(STATE_PATH, state)

    extraction_summary = extract_research_families(limit_papers=max(60, target_paper_texts), limit_sec=max(120, target_sec_reports))

    summary = SyncSummary(
        started_at=started,
        finished_at=iso_now(),
        new_arxiv_papers=new_arxiv,
        new_company_reports=new_company_reports,
        accepted_alpha_families=int(extraction_summary.get("accepted", 0)),
        source_errors=source_errors,
    )
    save_json(SUMMARY_PATH, summary.__dict__)
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch USA research resources for raw-alpha discovery.")
    parser.add_argument("--lookback-days", type=int, default=7)
    parser.add_argument("--target-paper-texts", type=int, default=0, help="Backfill arXiv q-fin PDF texts until this local count is reached.")
    parser.add_argument("--target-sec-reports", type=int, default=0, help="Backfill SEC filing texts until this local count is reached.")
    parser.add_argument("--loop-seconds", type=int, default=0, help="Run continuously. Example: 86400 for daily checks.")
    args = parser.parse_args()

    while True:
        summary = run_once(
            lookback_days=args.lookback_days,
            target_paper_texts=args.target_paper_texts,
            target_sec_reports=args.target_sec_reports,
        )
        print(json.dumps(summary.__dict__, ensure_ascii=False, indent=2))
        if args.loop_seconds <= 0:
            break
        time.sleep(args.loop_seconds)


if __name__ == "__main__":
    main()
