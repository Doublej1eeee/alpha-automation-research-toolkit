#!/usr/bin/env python
"""Extract original-alpha families from fetched USA research resources."""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
from typing import Any


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from script.raw_alpha_builder import build_template_from_archetype, parse_idea  # noqa: E402
from script.template_validator import validate_template_payload  # noqa: E402


PAPERS_METADATA_DIR = ROOT_DIR / "memory" / "learning_sources" / "quant_papers" / "usa_equity" / "metadata"
PAPERS_TEXT_DIR = ROOT_DIR / "memory" / "learning_sources" / "quant_papers" / "usa_equity" / "texts"
COMPANY_NEWS_METADATA_DIR = ROOT_DIR / "memory" / "learning_sources" / "research_reports" / "usa_equity" / "company_news" / "metadata"
COMPANY_NEWS_RAW_DIR = ROOT_DIR / "memory" / "learning_sources" / "research_reports" / "usa_equity" / "company_news" / "raw"
SEC_METADATA_DIR = ROOT_DIR / "memory" / "learning_sources" / "research_reports" / "usa_equity" / "sec_filings" / "metadata"
SEC_RAW_DIR = ROOT_DIR / "memory" / "learning_sources" / "research_reports" / "usa_equity" / "sec_filings" / "raw"

TEMP_OUTPUT_DIR = ROOT_DIR / "temp" / "research_alpha_extraction"
CATALOG_PATH = ROOT_DIR / "result_store" / "index" / "research_alpha_family_catalog.jsonl"
PROCESSED_SOURCES_PATH = ROOT_DIR / "memory" / "processed_research_sources.jsonl"

RAW_ALPHA_HUMAN = ROOT_DIR / "alpha_generation" / "raw_alpha_human.md"
RAW_ALPHA_AI = ROOT_DIR / "alpha_generation" / "raw_alpha_ai.md"
TEMPLATE_DIR = ROOT_DIR / "alpha_generation" / "templates"

TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")

DOMAIN_RULES = {
    "analyst": {"analyst", "forecast", "estimate", "guidance", "target", "revision"},
    "sentiment": {"news", "media", "language", "textual", "disclosure", "tone", "attention"},
    "fundamental": {"earnings", "cash", "accrual", "balance", "liquidity", "margin", "revenue", "profitability"},
    "event": {"8", "k", "announcement", "filing", "merger", "repurchase", "dividend", "guidance"},
    "risk": {"risk", "volatility", "uncertainty", "credit", "default", "drawdown", "fraud"},
}

MECHANISM_RULES = {
    "drift": {"drift", "underreaction", "post", "earnings", "announcement", "delayed", "gradual"},
    "revision": {"revision", "forecast", "guidance", "target", "estimate"},
    "quality": {"quality", "profitability", "accrual", "stability", "balance", "cash"},
    "attention": {"attention", "news", "textual", "language", "coverage", "information"},
    "stress": {"liquidity", "default", "credit", "stress", "distress", "uncertainty"},
    "governance": {"governance", "proxy", "compensation", "ownership", "board"},
}

REPORT_TITLE_HINTS = {
    "earnings": ("fundamental", ["drift", "revision"]),
    "results": ("fundamental", ["drift", "revision"]),
    "guidance": ("analyst", ["revision"]),
    "outlook": ("analyst", ["revision"]),
    "ceo": ("risk", ["governance"]),
    "chief executive officer": ("risk", ["governance"]),
    "cfo": ("risk", ["governance"]),
    "chief financial officer": ("risk", ["governance"]),
    "board": ("risk", ["governance"]),
    "chairman": ("risk", ["governance"]),
    "dividend": ("fundamental", ["quality"]),
    "buyback": ("fundamental", ["quality"]),
    "repurchase": ("fundamental", ["quality"]),
}

ARCHETYPE_BY_DOMAIN = {
    "analyst": "analyst_revision_momentum",
    "sentiment": "sentiment_momentum",
    "fundamental": "fundamental_level_momentum",
    "event": "sentiment_momentum",
    "risk": "risk_instability",
}

POSITIVE_FINANCE_HINTS = {
    "market", "markets", "stock", "stocks", "equity", "equities", "earnings",
    "analyst", "revision", "return", "returns", "trading", "investment",
    "investor", "asset", "assets", "portfolio", "credit", "liquidity",
    "fraud", "guidance", "valuation", "announcement", "disclosure",
}

NEGATIVE_TOPIC_HINTS = {
    "urban", "park", "palestinian", "health", "disease", "campus",
    "education", "ecosystem", "ecology", "tourism", "biological",
}

PAPER_TITLE_EXCLUDE_HINTS = {
    "survey",
    "comprehensive survey",
    "urban parks",
    "cybersecurity",
    "political turnover",
    "ecosystem service",
    "manufacturing program",
    "american manufacturing",
    "oligopoly",
    "existential risk",
}

REPORT_TITLE_INCLUDE_HINTS = {
    "earnings",
    "results",
    "guidance",
    "outlook",
    "dividend",
    "repurchase",
    "buyback",
    "quarter",
    "annual",
    "conference call",
}

REPORT_TITLE_EXCLUDE_HINTS = {
    "partner",
    "partnership",
    "manufacturing program",
    "community",
    "education",
    "award",
    "store",
    "launch",
    "product",
    "device",
}


@dataclass
class ExtractedFamily:
    family_key: str
    source_type: str
    source_id: str
    title: str
    domain: str
    mechanisms: list[str]
    field_plan: dict[str, Any]
    parsed_idea_text: str
    template: dict[str, Any]
    validation_valid: bool
    validation_errors: list[str]
    duplicate_of_existing_family: bool
    rationale: list[str]


def tokenize(text: str) -> set[str]:
    return {token.lower() for token in TOKEN_RE.findall(text or "") if token}


def normalize_keyword_text(text: str) -> str:
    text = str(text or "").lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    return " ".join(text.split())


def load_json(path: Path, default: Any) -> Any:
    if not path.exists():
        return default
    return json.loads(path.read_text(encoding="utf-8"))


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")


def append_jsonl(path: Path, row: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def processed_source_keys() -> set[str]:
    keys: set[str] = set()
    if not PROCESSED_SOURCES_PATH.exists():
        return keys
    with PROCESSED_SOURCES_PATH.open("r", encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except Exception:
                continue
            source_type = str(row.get("source_type") or "").strip()
            source_id = str(row.get("source_id") or "").strip()
            title = normalize_keyword_text(row.get("title") or "")
            if source_type and source_id:
                keys.add(f"{source_type}:{source_id}")
            if source_type and title:
                keys.add(f"{source_type}:title:{title}")
    return keys


def source_processed_key(source_type: str, payload: dict[str, Any]) -> str:
    source_id = str(payload.get("id") or payload.get("accession") or payload.get("title") or "").strip()
    return f"{source_type}:{source_id}"


def source_title_processed_key(source_type: str, payload: dict[str, Any]) -> str:
    return f"{source_type}:title:{normalize_keyword_text(payload.get('title') or payload.get('entity_name') or '')}"


def iter_metadata_files(path: Path) -> list[Path]:
    if not path.exists():
        return []
    return sorted([item for item in path.iterdir() if item.is_file() and item.suffix == ".json"])


def choose_domain(tokens: set[str]) -> tuple[str, list[str]]:
    scores: list[tuple[str, int]] = []
    rationale: list[str] = []
    for domain, keywords in DOMAIN_RULES.items():
        overlap = tokens & keywords
        scores.append((domain, len(overlap)))
        if overlap:
            rationale.append(f"{domain}: {', '.join(sorted(overlap))}")
    scores.sort(key=lambda item: (-item[1], item[0]))
    best_domain, best_score = scores[0]
    if best_score <= 0:
        return "fundamental", rationale + ["fallback_domain=fundamental"]
    return best_domain, rationale


def choose_mechanisms(tokens: set[str]) -> list[str]:
    mechanisms: list[str] = []
    for name, keywords in MECHANISM_RULES.items():
        if tokens & keywords:
            mechanisms.append(name)
    return mechanisms or ["level"]


def build_family_key(domain: str, mechanisms: list[str], title: str) -> str:
    title_tokens = sorted(tokenize(title) & {"earnings", "guidance", "attention", "accrual", "liquidity", "credit", "analyst", "news", "fraud"})
    pieces = [domain] + sorted(mechanisms) + title_tokens[:3]
    return "__".join(pieces)


def is_finance_relevant_paper(payload: dict[str, Any]) -> bool:
    categories = [str(item).lower() for item in (payload.get("categories") or [])]
    if any(category.startswith("q-fin") for category in categories):
        return True
    title = str(payload.get("title") or "").lower()
    summary = str(payload.get("summary") or "").lower()
    primary_topic = str(payload.get("primary_topic") or "").lower()
    full_text = str(payload.get("full_text_excerpt") or "").lower()
    text = f"{title} {summary} {primary_topic} {full_text}"
    positive = sum(1 for hint in POSITIVE_FINANCE_HINTS if hint in text)
    negative = sum(1 for hint in NEGATIVE_TOPIC_HINTS if hint in text)
    title_negative = any(hint in title for hint in PAPER_TITLE_EXCLUDE_HINTS)
    if title_negative and positive < 4:
        return False
    return positive >= 2 and positive > negative


def is_relevant_company_report(payload: dict[str, Any]) -> bool:
    title = normalize_keyword_text(payload.get("title") or "")
    text = " ".join(
        [
            title,
            normalize_keyword_text(payload.get("publisher") or ""),
            normalize_keyword_text(payload.get("full_text_excerpt") or ""),
        ]
    )
    include_hits = sum(1 for hint in REPORT_TITLE_INCLUDE_HINTS if hint in text)
    exclude_hits = sum(1 for hint in REPORT_TITLE_EXCLUDE_HINTS if hint in text)
    return include_hits >= 1 and include_hits > exclude_hits


def load_paper_full_text(payload: dict[str, Any]) -> str:
    paper_id = str(payload.get("id") or "").strip()
    if not paper_id:
        return ""
    text_path = PAPERS_TEXT_DIR / f"arxiv_{safe_slug(paper_id)}.txt"
    if not text_path.exists():
        return ""
    return text_path.read_text(encoding="utf-8", errors="ignore")


def compact_excerpt(text: str, max_chars: int = 3000) -> str:
    text = text.replace("\x00", " ")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    text = text.strip()
    if len(text) <= max_chars:
        return text
    return text[:max_chars]


def extract_existing_family_keys() -> set[str]:
    keys: set[str] = set()

    for path in [RAW_ALPHA_HUMAN, RAW_ALPHA_AI]:
        if path.exists():
            text = path.read_text(encoding="utf-8", errors="ignore").lower()
            for line in text.splitlines():
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if line.startswith("alpha:"):
                    family_match = re.search(r"family=([a-z0-9_\\-]+)", line)
                    if family_match:
                        keys.add(family_match.group(1))
                        continue
                tokens = tokenize(line)
                if not tokens:
                    continue
                domain, _ = choose_domain(tokens)
                mechanisms = choose_mechanisms(tokens)
                keys.add(build_family_key(domain, mechanisms, line))

    if TEMPLATE_DIR.exists():
        for template_path in TEMPLATE_DIR.glob("*.yaml"):
            name = template_path.stem.lower()
            tokens = tokenize(name)
            domain, _ = choose_domain(tokens)
            mechanisms = choose_mechanisms(tokens)
            keys.add(build_family_key(domain, mechanisms, name))

    if CATALOG_PATH.exists():
        with CATALOG_PATH.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    payload = json.loads(line)
                except Exception:
                    continue
                key = str(payload.get("family_key") or "").strip()
                if key:
                    keys.add(key)
    return keys


def build_idea_text(title: str, domain: str, mechanisms: list[str]) -> str:
    mechanism_text = " ".join(mechanisms)
    if domain == "analyst":
        return f"{title}. analyst estimate revision drift {mechanism_text}"
    if domain == "sentiment":
        return f"{title}. news sentiment underreaction attention {mechanism_text}"
    if domain == "risk":
        return f"{title}. credit stress instability risk {mechanism_text}"
    return f"{title}. fundamental quality earnings persistence {mechanism_text}"


def build_field_plan(domain: str, mechanisms: list[str], source_type: str, payload: dict[str, Any]) -> dict[str, Any]:
    query_terms = [domain, *mechanisms]
    dataset_hints: list[str]
    category_hints: list[str]

    if domain == "analyst":
        dataset_hints = ["analyst"]
        category_hints = ["analyst"]
        query_terms.extend(["estimate", "forecast", "target", "eps"])
    elif domain == "sentiment":
        dataset_hints = ["sentiment", "news"]
        category_hints = ["sentiment", "news"]
        query_terms.extend(["news", "tone", "attention", "coverage"])
    elif domain == "risk":
        dataset_hints = ["model", "fundamental"]
        category_hints = ["model", "fundamental"]
        query_terms.extend(["risk", "credit", "liquidity", "default", "distress"])
    else:
        dataset_hints = ["fundamental"]
        category_hints = ["fundamental"]
        query_terms.extend(["earnings", "cash", "margin", "accrual", "profitability"])

    if source_type == "sec":
        form = str(payload.get("form") or "").upper()
        if form == "8-K":
            dataset_hints = ["news", "analyst"]
            category_hints = ["news", "analyst"]
            query_terms.extend(["guidance", "announcement", "surprise"])
        elif form in {"10-Q", "10-K"}:
            dataset_hints = ["fundamental"]
            category_hints = ["fundamental"]
            query_terms.extend(["revenue", "margin", "balance", "cashflow"])
        elif form == "DEF 14A":
            dataset_hints = ["model", "fundamental"]
            category_hints = ["model", "fundamental"]
            query_terms.extend(["governance", "ownership", "compensation"])

    seen: set[str] = set()
    ordered_terms: list[str] = []
    for term in query_terms:
        if term in seen:
            continue
        seen.add(term)
        ordered_terms.append(term)
    return {
        "dataset_hints": dataset_hints,
        "category_hints": category_hints,
        "search_terms": ordered_terms,
    }


def safe_slug(value: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", value)[:180] or "untitled"


def build_family_from_source(source_type: str, payload: dict[str, Any], existing_keys: set[str]) -> ExtractedFamily | None:
    title = str(payload.get("title") or payload.get("entity_name") or payload.get("form") or "").strip()
    if not title:
        return None
    if source_type == "paper":
        full_text = load_paper_full_text(payload)
        if full_text:
            payload = dict(payload)
            payload["full_text_excerpt"] = compact_excerpt(full_text)
    if source_type == "paper" and not is_finance_relevant_paper(payload):
        return None
    if source_type == "report" and not is_relevant_company_report(payload):
        return None

    text = " ".join(
        [
            title,
            str(payload.get("summary") or ""),
            str(payload.get("full_text_excerpt") or ""),
            str(payload.get("form") or ""),
            str(payload.get("sic_description") or ""),
            str(payload.get("primary_topic") or ""),
        ]
    )
    tokens = tokenize(text)

    if source_type == "report":
        lowered = text.lower()
        domain = None
        mechanisms: list[str] | None = None
        rationale = []
        for hint, rule in REPORT_TITLE_HINTS.items():
            if hint in lowered:
                domain, mechanisms = rule
                rationale = [f"report_hint={hint}"]
                break
        if domain is None:
            domain, rationale = choose_domain(tokens)
            mechanisms = choose_mechanisms(tokens)
    elif source_type == "sec":
        form = str(payload.get("form") or "").upper()
        if form == "8-K":
            domain = "event"
            rationale = [f"sec_form={form}", "event_driven_disclosure"]
        elif form in {"10-Q", "10-K"}:
            domain = "fundamental"
            rationale = [f"sec_form={form}", "periodic_fundamental_disclosure"]
        elif form == "DEF 14A":
            domain = "risk"
            rationale = [f"sec_form={form}", "governance_proxy_signal"]
        else:
            domain, rationale = choose_domain(tokens)
        mechanisms = choose_mechanisms(tokens)
    else:
        domain, rationale = choose_domain(tokens)
        mechanisms = choose_mechanisms(tokens)
    family_key = build_family_key(domain, mechanisms, title)
    duplicate = family_key in existing_keys

    archetype = ARCHETYPE_BY_DOMAIN.get(domain, "fundamental_level_momentum")
    field_plan = build_field_plan(domain, mechanisms, source_type, payload)
    idea_text = build_idea_text(title, domain, mechanisms)

    parsed = parse_idea(idea_text)
    parsed = parsed.__class__(
        raw_text=parsed.raw_text,
        tokens=parsed.tokens,
        inferred_tags=sorted(set(parsed.inferred_tags + [domain] + mechanisms)),
        inferred_category=parsed.inferred_category,
        inferred_archetype=archetype,
        rationale=parsed.rationale + rationale,
    )

    template = build_template_from_archetype(parsed)
    template["name"] = f"research_{source_type}_{safe_slug(family_key)}"
    template["description"] = title
    template["tags"] = sorted(set(template.get("tags") or []) | {source_type, domain, *mechanisms})
    if field_plan["dataset_hints"]:
        template["field_selection"]["dataset_hint"] = field_plan["dataset_hints"][0]

    validation = validate_template_payload(Path("<research>"), template)
    return ExtractedFamily(
        family_key=family_key,
        source_type=source_type,
        source_id=str(payload.get("id") or payload.get("accession") or title),
        title=title,
        domain=domain,
        mechanisms=mechanisms,
        field_plan=field_plan,
        parsed_idea_text=idea_text,
        template=template,
        validation_valid=validation.valid,
        validation_errors=validation.errors,
        duplicate_of_existing_family=duplicate,
        rationale=parsed.rationale,
    )


def load_company_news_payloads(limit: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    processed = processed_source_keys()
    for path in sorted(iter_metadata_files(COMPANY_NEWS_METADATA_DIR), key=lambda item: item.stat().st_mtime, reverse=True)[:limit]:
        payload = load_json(path, {})
        if source_processed_key("report", payload) in processed or source_title_processed_key("report", payload) in processed:
            continue
        publisher = str(payload.get("publisher") or "unknown")
        source_id = str(payload.get("id") or "")
        raw_path = COMPANY_NEWS_RAW_DIR / f"{publisher}_{source_id}.txt"
        if not raw_path.exists():
            continue
        payload["full_text_excerpt"] = compact_excerpt(raw_path.read_text(encoding="utf-8", errors="ignore"))
        rows.append(payload)
    return rows


def load_sec_payloads(limit: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    processed = processed_source_keys()
    for path in sorted(iter_metadata_files(SEC_METADATA_DIR), key=lambda item: item.stat().st_mtime, reverse=True)[:limit]:
        payload = load_json(path, {})
        if source_processed_key("sec", payload) in processed or source_title_processed_key("sec", payload) in processed:
            continue
        source_id = str(payload.get("id") or "")
        raw_path = SEC_RAW_DIR / f"{source_id}.txt"
        if not raw_path.exists():
            continue
        payload["full_text_excerpt"] = compact_excerpt(raw_path.read_text(encoding="utf-8", errors="ignore"))
        rows.append(payload)
    return rows


def load_paper_payloads(limit: int) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    processed = processed_source_keys()
    for path in sorted(iter_metadata_files(PAPERS_METADATA_DIR), key=lambda item: item.stat().st_mtime, reverse=True)[:limit]:
        payload = load_json(path, {})
        if source_processed_key("paper", payload) in processed or source_title_processed_key("paper", payload) in processed:
            continue
        rows.append(payload)
    return rows


def write_catalog(records: list[ExtractedFamily]) -> None:
    CATALOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    existing_rows: dict[str, dict[str, Any]] = {}
    if CATALOG_PATH.exists():
        try:
            with CATALOG_PATH.open("r", encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    line = line.strip()
                    if not line:
                        continue
                    try:
                        row = json.loads(line)
                    except Exception:
                        continue
                    key = str(row.get("family_key") or "")
                    source_id = str(row.get("source_id") or "")
                    existing_rows[f"{key}:{source_id}"] = row
        except PermissionError:
            existing_rows = {}
    for record in records:
        row = asdict(record)
        existing_rows[f"{record.family_key}:{record.source_id}"] = row
    lines = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in existing_rows.values())
    try:
        CATALOG_PATH.write_text(lines, encoding="utf-8")
    except PermissionError:
        fallback = CATALOG_PATH.with_name(f"{CATALOG_PATH.stem}_{safe_slug(str(Path.cwd().stat().st_mtime_ns))}{CATALOG_PATH.suffix}")
        try:
            fallback.write_text(lines, encoding="utf-8")
        except PermissionError:
            return


def safe_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        path.write_text(text, encoding="utf-8")
        return
    except PermissionError:
        fallback = ROOT_DIR / "result_store" / "index" / f"{path.stem}_{safe_slug(str(Path.cwd().stat().st_mtime_ns))}{path.suffix}"
        try:
            fallback.parent.mkdir(parents=True, exist_ok=True)
            fallback.write_text(text, encoding="utf-8")
        except PermissionError:
            return


def source_paths_for_payload(source_type: str, payload: dict[str, Any]) -> tuple[Path | None, Path | None]:
    if source_type == "paper":
        source_id = str(payload.get("id") or "").strip()
        if not source_id:
            return None, None
        return (
            PAPERS_METADATA_DIR / f"arxiv_{safe_slug(source_id)}.json",
            PAPERS_TEXT_DIR / f"arxiv_{safe_slug(source_id)}.txt",
        )
    if source_type == "report":
        publisher = str(payload.get("publisher") or "unknown")
        source_id = str(payload.get("id") or "").strip()
        if not source_id:
            return None, None
        return (
            COMPANY_NEWS_METADATA_DIR / f"{publisher}_{source_id}.json",
            COMPANY_NEWS_RAW_DIR / f"{publisher}_{source_id}.txt",
        )
    if source_type == "sec":
        source_id = str(payload.get("id") or "").strip()
        if not source_id:
            return None, None
        return (
            SEC_METADATA_DIR / f"{source_id}.json",
            SEC_RAW_DIR / f"{source_id}.txt",
        )
    return None, None


def mark_source_processed(
    source_type: str,
    payload: dict[str, Any],
    produced_family_keys: list[str],
    *,
    outcome: str = "accepted",
    delete_text: bool = True,
) -> None:
    metadata_path, text_path = source_paths_for_payload(source_type, payload)
    row = {
        "source_type": source_type,
        "source_id": str(payload.get("id") or payload.get("accession") or payload.get("title") or "").strip(),
        "title": str(payload.get("title") or payload.get("entity_name") or "").strip(),
        "metadata_path": str(metadata_path.relative_to(ROOT_DIR)).replace("\\", "/") if metadata_path and metadata_path.exists() else None,
        "text_path": str(text_path.relative_to(ROOT_DIR)).replace("\\", "/") if text_path and text_path.exists() else None,
        "processed_at": utc_now_iso(),
        "produced_family_keys": produced_family_keys,
        "outcome": outcome,
    }
    append_jsonl(PROCESSED_SOURCES_PATH, row)
    if delete_text and text_path and text_path.exists():
        try:
            text_path.unlink()
        except PermissionError:
            pass


def run(limit_papers: int, limit_sec: int) -> dict[str, Any]:
    existing_keys = extract_existing_family_keys()
    accepted: list[ExtractedFamily] = []
    skipped_duplicates: list[ExtractedFamily] = []
    rejected_invalid: list[ExtractedFamily] = []
    processed_count = 0

    def handle_payload(source_type: str, payload: dict[str, Any]) -> None:
        nonlocal processed_count
        family = build_family_from_source(source_type, payload, existing_keys)
        if not family:
            mark_source_processed(source_type, payload, [], outcome="irrelevant", delete_text=True)
            processed_count += 1
            return
        if family.duplicate_of_existing_family:
            skipped_duplicates.append(family)
            mark_source_processed(source_type, payload, [family.family_key], outcome="duplicate", delete_text=True)
            processed_count += 1
            return
        if not family.validation_valid:
            rejected_invalid.append(family)
            mark_source_processed(source_type, payload, [family.family_key], outcome="invalid", delete_text=True)
            processed_count += 1
            return
        accepted.append(family)
        existing_keys.add(family.family_key)
        mark_source_processed(source_type, payload, [family.family_key], outcome="accepted", delete_text=True)
        processed_count += 1

    for payload in load_paper_payloads(limit_papers):
        handle_payload("paper", payload)

    for payload in load_company_news_payloads(limit_sec):
        handle_payload("report", payload)

    for payload in load_sec_payloads(limit_sec):
        handle_payload("sec", payload)

    TEMP_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    output_path = TEMP_OUTPUT_DIR / "latest_extracted_families.json"
    output_payload = {
        "accepted": [asdict(item) for item in accepted],
        "skipped_duplicates": [asdict(item) for item in skipped_duplicates[:50]],
        "rejected_invalid": [asdict(item) for item in rejected_invalid[:50]],
    }
    safe_write_text(output_path, json.dumps(output_payload, ensure_ascii=False, indent=2))
    write_catalog(accepted)
    return {
        "accepted": len(accepted),
        "skipped_duplicates": len(skipped_duplicates),
        "rejected_invalid": len(rejected_invalid),
        "processed_sources_deleted": processed_count,
        "output": str(output_path.relative_to(ROOT_DIR)),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Extract raw-alpha families from fetched research resources.")
    parser.add_argument("--limit-papers", type=int, default=40)
    parser.add_argument("--limit-sec", type=int, default=80)
    args = parser.parse_args()
    summary = run(limit_papers=args.limit_papers, limit_sec=args.limit_sec)
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
