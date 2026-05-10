#!/usr/bin/env python
"""File-based compiler/autofix knowledge store.

This keeps the project lightweight while still absorbing a useful idea from the
reference project: expression errors and successful repairs should become
reusable knowledge, not disappear after one run.
"""

from __future__ import annotations

from dataclasses import dataclass, asdict
import json
from pathlib import Path
from typing import Iterable


ROOT_DIR = Path(__file__).resolve().parents[1]
KNOWLEDGE_PATH = ROOT_DIR / "result_store" / "index" / "compiler_knowledge.jsonl"


@dataclass
class CompilerKnowledgeRule:
    rule_type: str
    match_text: str
    replacement: str | None = None
    description: str | None = None
    created_from: str | None = None


def ensure_parent() -> None:
    KNOWLEDGE_PATH.parent.mkdir(parents=True, exist_ok=True)


def append_rule(rule: CompilerKnowledgeRule) -> None:
    ensure_parent()
    with KNOWLEDGE_PATH.open("a", encoding="utf-8") as file:
        file.write(json.dumps(asdict(rule), ensure_ascii=False) + "\n")


def load_rules() -> list[CompilerKnowledgeRule]:
    if not KNOWLEDGE_PATH.exists():
        return []
    rules: list[CompilerKnowledgeRule] = []
    with KNOWLEDGE_PATH.open("r", encoding="utf-8") as file:
        for line in file:
            line = line.strip()
            if not line:
                continue
            try:
                payload = json.loads(line)
            except Exception:
                continue
            try:
                rules.append(CompilerKnowledgeRule(**payload))
            except Exception:
                continue
    return rules


def iter_replace_rules() -> Iterable[CompilerKnowledgeRule]:
    for rule in load_rules():
        if rule.rule_type == "replace_text" and rule.match_text and rule.replacement is not None:
            yield rule
