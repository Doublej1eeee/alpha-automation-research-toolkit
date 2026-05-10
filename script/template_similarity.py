#!/usr/bin/env python
"""Template similarity helpers."""

from __future__ import annotations

import hashlib
import re
from difflib import SequenceMatcher


def normalize_template_expression(expression: str) -> str:
    normalized = expression.strip()
    normalized = re.sub(r"\{\{FIELD(?:_[A-Z0-9]+)?_SLUG\}\}", "{{FIELD_SLOT}}", normalized)
    normalized = re.sub(r"\{\{FIELD(?:_[A-Z0-9]+)?\}\}", "{{FIELD_SLOT}}", normalized)
    normalized = re.sub(r"\s+", " ", normalized)
    return normalized.lower()


def template_hash(expression: str) -> str:
    normalized = normalize_template_expression(expression)
    return hashlib.md5(normalized.encode("utf-8")).hexdigest()


def template_similarity(left: str, right: str) -> float:
    return SequenceMatcher(
        None,
        normalize_template_expression(left),
        normalize_template_expression(right),
    ).ratio()
