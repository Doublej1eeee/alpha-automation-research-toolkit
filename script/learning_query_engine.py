#!/usr/bin/env python
"""Search learning sources and derive query hints for field selection.

This follows the more robust direction implied by the reference project:
- learning/forum/tutorial content is an auxiliary semantic layer
- data fields are still selected from real field metadata
- documentation can expand or refine search terms
"""

from __future__ import annotations

import argparse
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
import re


ROOT_DIR = Path(__file__).resolve().parents[1]
LEARNING_ROOT = ROOT_DIR / "memory" / "learning_sources"
TEXT_EXTENSIONS = {".md", ".txt"}
TOKEN_RE = re.compile(r"[A-Za-z0-9_]+")
STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "can", "data", "field",
    "fields", "for", "from", "group", "have", "how", "if", "in", "into", "is",
    "it", "more", "of", "on", "or", "that", "the", "their", "this", "to", "use",
    "using", "with", "you", "your",
}
PHRASE_HINTS = {
    "earnings per share": ["eps"],
    "eps": ["earnings per share"],
    "implied volatility": ["iv"],
    "iv": ["implied volatility"],
    "subindustry": ["industry", "sector"],
    "industry": ["subindustry", "sector"],
    "sentiment": ["news", "tone"],
    "analyst": ["estimate", "revision", "forecast"],
    "fundamental": ["valuation", "quality", "cashflow", "sales", "earnings"],
}


@dataclass
class LearningSnippet:
    path: Path
    score: int
    text: str


def tokenize(text: str) -> list[str]:
    return [
        token.lower()
        for token in TOKEN_RE.findall(text)
        if token and token.lower() not in STOPWORDS
    ]


def iter_learning_files(root: Path = LEARNING_ROOT) -> list[Path]:
    return [
        path
        for path in root.rglob("*")
        if path.is_file() and path.suffix.lower() in TEXT_EXTENSIONS
    ]


def split_snippets(text: str) -> list[str]:
    blocks = re.split(r"(?:\n\s*\n)+", text)
    snippets = []
    for block in blocks:
        cleaned = " ".join(block.split())
        if cleaned:
            snippets.append(cleaned)
    return snippets


def search_learning_sources(query: str, max_hits: int = 8) -> list[LearningSnippet]:
    query_tokens = set(tokenize(query))
    if not query_tokens:
        return []

    hits: list[LearningSnippet] = []
    for path in iter_learning_files():
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except Exception:
            continue
        for snippet in split_snippets(text):
            snippet_tokens = set(tokenize(snippet))
            overlap = query_tokens & snippet_tokens
            if not overlap:
                continue
            score = len(overlap)
            if any(phrase in snippet.lower() for phrase in query_tokens):
                score += 1
            hits.append(LearningSnippet(path=path, score=score, text=snippet))

    hits.sort(key=lambda item: (-item.score, len(item.text), str(item.path)))
    return hits[:max_hits]


def derive_query_hints(query: str, max_hints: int = 8) -> list[str]:
    query_tokens = tokenize(query)
    query_text_lower = query.lower()
    hints = []

    for phrase, additions in PHRASE_HINTS.items():
        if phrase in query_text_lower:
            hints.extend(additions)

    snippets = search_learning_sources(query, max_hits=10)
    counter: Counter[str] = Counter()
    query_token_set = set(query_tokens)
    for snippet in snippets:
        for token in tokenize(snippet.text):
            if token in query_token_set:
                continue
            if len(token) <= 2:
                continue
            counter[token] += snippet.score

    hints.extend(
        token
        for token, _ in counter.most_common(max_hints * 2)
        if token not in hints
    )

    unique_hints = []
    seen = set()
    for hint in hints:
        if hint in seen:
            continue
        seen.add(hint)
        unique_hints.append(hint)
        if len(unique_hints) >= max_hints:
            break
    return unique_hints


def build_enriched_query(query: str, max_hints: int = 6) -> str:
    hints = derive_query_hints(query, max_hints=max_hints)
    if not hints:
        return query
    return f"{query} {' '.join(hints)}".strip()


def main() -> None:
    parser = argparse.ArgumentParser(description="Search learning sources and derive query hints.")
    parser.add_argument("query")
    parser.add_argument("--max-hits", type=int, default=8)
    parser.add_argument("--max-hints", type=int, default=8)
    args = parser.parse_args()

    hits = search_learning_sources(args.query, max_hits=args.max_hits)
    hints = derive_query_hints(args.query, max_hints=args.max_hints)

    print("Hints:", ", ".join(hints) if hints else "<none>")
    print("=" * 72)
    for idx, hit in enumerate(hits, start=1):
        try:
            rel = hit.path.relative_to(ROOT_DIR)
        except Exception:
            rel = hit.path
        print(f"{idx:02d}. score={hit.score} | {rel}")
        snippet = hit.text[:400]
        try:
            print(snippet)
        except UnicodeEncodeError:
            safe = snippet.encode("ascii", errors="ignore").decode("ascii")
            print(safe)
        print()


if __name__ == "__main__":
    main()
