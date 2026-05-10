#!/usr/bin/env python
"""Template and FASTEXPR-style expression validator.

This is a lightweight but real engineering step toward the generation_two
expression-compiler direction:
- parse template expressions before large-scale submission
- extract operators / identifiers / placeholders
- fail fast on malformed templates
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
import re
import sys
from typing import Iterable

import yaml


ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from script.compiler_knowledge import CompilerKnowledgeRule, append_rule, iter_replace_rules

TEMPLATES_DIR = ROOT_DIR / "alpha_generation" / "templates"

TOKEN_RE = re.compile(
    r"""
    (?P<SPACE>\s+)
    |(?P<PLACEHOLDER>\{\{[A-Za-z0-9_]+\}\})
    |(?P<NUMBER>\d+(?:\.\d+)?)
    |(?P<IDENT>[A-Za-z_][A-Za-z0-9_\.]*)
    |(?P<COMP>>=|<=|==|!=|>|<|=)
    |(?P<OP>[+\-*/(),])
    |(?P<MISMATCH>.)
    """,
    re.VERBOSE,
)

REQUIRED_TEMPLATE_KEYS = {"name", "type", "category", "settings", "expression"}
ALLOWED_SIMULATION_TYPES = {"REGULAR"}
KNOWN_GROUP_IDENTIFIERS = {"industry", "subindustry", "sector", "market"}
CROSS_SECTIONAL_OPERATORS = {
    "rank",
    "zscore",
    "group_rank",
    "group_zscore",
    "group_neutralize",
    "vector_neut",
    "regression_neut",
}


@dataclass
class Token:
    kind: str
    value: str
    start: int
    end: int


@dataclass
class ASTNode:
    kind: str
    value: str | None = None
    children: list["ASTNode"] = field(default_factory=list)


@dataclass
class TemplateValidationResult:
    path: Path
    valid: bool
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    operators: list[str] = field(default_factory=list)
    identifiers: list[str] = field(default_factory=list)
    placeholders: list[str] = field(default_factory=list)
    fixed_expression: str | None = None


@dataclass
class ExpressionCompatibilityProfile:
    operators: list[str] = field(default_factory=list)
    placeholder_paths: dict[str, list[list[str]]] = field(default_factory=dict)
    uses_vector_operator: bool = False
    uses_ts_operator: bool = False
    uses_ts_backfill: bool = False
    uses_cross_sectional_operator: bool = False
    field_used_as_group_key: bool = False
    placeholder_inside_ts_operator: bool = False
    placeholder_inside_cross_sectional_operator: bool = False
    cross_sectional_result_inside_ts_operator: bool = False
    parse_error: str | None = None


@dataclass
class ParsedExpression:
    ast: ASTNode
    parser: "ExpressionParser"


class ParseError(ValueError):
    pass


def _is_ts_operator(name: str) -> bool:
    return name.startswith("ts_")


def _is_cross_sectional_operator(name: str) -> bool:
    return name in CROSS_SECTIONAL_OPERATORS


def _collect_placeholder_paths(
    node: ASTNode,
    stack: list[str] | None = None,
    placeholder_paths: dict[str, list[list[str]]] | None = None,
) -> dict[str, list[list[str]]]:
    stack = list(stack or [])
    if placeholder_paths is None:
        placeholder_paths = {}
    if node.kind == "call":
        operator = str(node.value or "").lower()
        next_stack = stack + [operator]
        for idx, child in enumerate(node.children):
            if child.kind == "placeholder":
                child_stack = list(next_stack)
                if idx > 0 and operator.startswith("group_"):
                    child_stack.append("__group_key__")
                placeholder_paths.setdefault(str(child.value), []).append(child_stack)
            else:
                _collect_placeholder_paths(child, next_stack, placeholder_paths)
        return placeholder_paths
    if node.kind == "placeholder":
        placeholder_paths.setdefault(str(node.value), []).append(list(stack))
        return placeholder_paths
    for child in node.children:
        _collect_placeholder_paths(child, stack, placeholder_paths)
    return placeholder_paths


def parse_expression(expression: str) -> ParsedExpression:
    parser = ExpressionParser(tokenize(expression))
    ast = parser.parse()
    return ParsedExpression(ast=ast, parser=parser)


@lru_cache(maxsize=2048)
def analyze_expression_compatibility(expression: str) -> ExpressionCompatibilityProfile:
    expression = str(expression or "").strip()
    if not expression:
        return ExpressionCompatibilityProfile(parse_error="empty expression")
    try:
        parsed = parse_expression(expression)
    except ParseError as exc:
        return ExpressionCompatibilityProfile(parse_error=str(exc))

    operators = sorted(set(str(item).lower() for item in parsed.parser.operators))
    placeholder_paths = _collect_placeholder_paths(parsed.ast)
    uses_vector_operator = any(operator.startswith("vec_") for operator in operators)
    uses_ts_operator = any(_is_ts_operator(operator) for operator in operators)
    uses_ts_backfill = "ts_backfill" in operators
    uses_cross_sectional_operator = any(_is_cross_sectional_operator(operator) for operator in operators)

    placeholder_inside_ts_operator = False
    placeholder_inside_cross_sectional_operator = False
    field_used_as_group_key = False
    cross_sectional_result_inside_ts_operator = False
    for paths in placeholder_paths.values():
        for raw_path in paths:
            path = [item for item in raw_path if item != "__group_key__"]
            if "__group_key__" in raw_path:
                field_used_as_group_key = True
            if any(_is_ts_operator(item) for item in path):
                placeholder_inside_ts_operator = True
            if any(_is_cross_sectional_operator(item) for item in path):
                placeholder_inside_cross_sectional_operator = True
            for index, operator in enumerate(path):
                if _is_ts_operator(operator) and any(_is_cross_sectional_operator(item) for item in path[index + 1 :]):
                    cross_sectional_result_inside_ts_operator = True
                    break

    return ExpressionCompatibilityProfile(
        operators=operators,
        placeholder_paths=placeholder_paths,
        uses_vector_operator=uses_vector_operator,
        uses_ts_operator=uses_ts_operator,
        uses_ts_backfill=uses_ts_backfill,
        uses_cross_sectional_operator=uses_cross_sectional_operator,
        field_used_as_group_key=field_used_as_group_key,
        placeholder_inside_ts_operator=placeholder_inside_ts_operator,
        placeholder_inside_cross_sectional_operator=placeholder_inside_cross_sectional_operator,
        cross_sectional_result_inside_ts_operator=cross_sectional_result_inside_ts_operator,
    )


def tokenize(expression: str) -> list[Token]:
    tokens: list[Token] = []
    for match in TOKEN_RE.finditer(expression):
        kind = match.lastgroup or "MISMATCH"
        if kind == "SPACE":
            continue
        value = match.group()
        if kind == "MISMATCH":
            raise ParseError(f"Unexpected token {value!r} at position {match.start()}")
        tokens.append(Token(kind=kind, value=value, start=match.start(), end=match.end()))
    return tokens


class ExpressionParser:
    def __init__(self, tokens: list[Token]):
        self.tokens = tokens
        self.index = 0
        self.operators: list[str] = []
        self.identifiers: list[str] = []
        self.placeholders: list[str] = []

    def parse(self) -> ASTNode:
        if not self.tokens:
            raise ParseError("Empty expression")
        node = self.parse_expression()
        if self.current() is not None:
            token = self.current()
            raise ParseError(f"Unexpected trailing token {token.value!r} at position {token.start}")
        return node

    def current(self) -> Token | None:
        if self.index >= len(self.tokens):
            return None
        return self.tokens[self.index]

    def consume(self, expected_value: str | None = None, expected_kind: str | None = None) -> Token:
        token = self.current()
        if token is None:
            raise ParseError("Unexpected end of expression")
        if expected_value is not None and token.value != expected_value:
            raise ParseError(f"Expected {expected_value!r}, got {token.value!r}")
        if expected_kind is not None and token.kind != expected_kind:
            raise ParseError(f"Expected token kind {expected_kind}, got {token.kind}")
        self.index += 1
        return token

    def parse_expression(self) -> ASTNode:
        return self.parse_comparison()

    def parse_comparison(self) -> ASTNode:
        node = self.parse_additive()
        while self.current() is not None and self.current().kind == "COMP":
            op = self.consume().value
            right = self.parse_additive()
            node = ASTNode(kind="binary_op", value=op, children=[node, right])
        return node

    def parse_additive(self) -> ASTNode:
        node = self.parse_multiplicative()
        while self.current() is not None and self.current().value in {"+", "-"}:
            op = self.consume().value
            right = self.parse_multiplicative()
            node = ASTNode(kind="binary_op", value=op, children=[node, right])
        return node

    def parse_multiplicative(self) -> ASTNode:
        node = self.parse_unary()
        while self.current() is not None and self.current().value in {"*", "/"}:
            op = self.consume().value
            right = self.parse_unary()
            node = ASTNode(kind="binary_op", value=op, children=[node, right])
        return node

    def parse_unary(self) -> ASTNode:
        token = self.current()
        if token is not None and token.value in {"+", "-"}:
            op = self.consume().value
            child = self.parse_unary()
            return ASTNode(kind="unary_op", value=op, children=[child])
        return self.parse_primary()

    def parse_primary(self) -> ASTNode:
        token = self.current()
        if token is None:
            raise ParseError("Unexpected end of expression")

        if token.kind == "NUMBER":
            self.consume()
            return ASTNode(kind="number", value=token.value)

        if token.kind == "PLACEHOLDER":
            self.consume()
            self.placeholders.append(token.value)
            return ASTNode(kind="placeholder", value=token.value)

        if token.kind == "IDENT":
            ident = self.consume().value
            self.identifiers.append(ident)
            if self.current() is not None and self.current().value == "(":
                self.operators.append(ident)
                self.consume("(")
                args: list[ASTNode] = []
                if self.current() is not None and self.current().value != ")":
                    while True:
                        args.append(self.parse_expression())
                        if self.current() is not None and self.current().value == ",":
                            self.consume(",")
                            continue
                        break
                self.consume(")")
                return ASTNode(kind="call", value=ident, children=args)
            return ASTNode(kind="identifier", value=ident)

        if token.value == "(":
            self.consume("(")
            node = self.parse_expression()
            self.consume(")")
            return ASTNode(kind="group", children=[node])

        raise ParseError(f"Unexpected token {token.value!r} at position {token.start}")


def load_template(path: Path) -> dict:
    payload = yaml.safe_load(path.read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError(f"Template must be a YAML mapping: {path}")
    return payload


def validate_template_payload(path: Path, template: dict) -> TemplateValidationResult:
    errors: list[str] = []
    warnings: list[str] = []

    missing = sorted(REQUIRED_TEMPLATE_KEYS - set(template.keys()))
    if missing:
        errors.append(f"missing required keys: {', '.join(missing)}")

    template_type = str(template.get("type") or "").upper()
    if template_type and template_type not in ALLOWED_SIMULATION_TYPES:
        errors.append(f"unsupported template type: {template_type}")

    settings = template.get("settings")
    if settings is not None and not isinstance(settings, dict):
        errors.append("settings must be a mapping")

    expression = str(template.get("expression") or "").strip()
    if not expression:
        errors.append("expression is empty")

    operators: list[str] = []
    identifiers: list[str] = []
    placeholders: list[str] = []
    compatibility_profile: ExpressionCompatibilityProfile | None = None

    if expression:
        try:
            parsed = parse_expression(expression)
            operators = sorted(set(parsed.parser.operators))
            identifiers = sorted(set(parsed.parser.identifiers))
            placeholders = sorted(set(parsed.parser.placeholders))
            compatibility_profile = analyze_expression_compatibility(expression)
        except ParseError as exc:
            errors.append(f"expression parse error: {exc}")

    field_slots = sorted(set(re.findall(r"\{\{(FIELD(?:_[A-Z0-9]+)?)\}\}", expression)))
    name_text = str(template.get("name") or "")
    for slot in field_slots:
        if f"{{{{{slot}_SLUG}}}}" not in name_text:
            warnings.append(f"expression uses {{{{{slot}}}}} but template name does not include {{{{{slot}_SLUG}}}}")

    if expression and not field_slots:
        warnings.append("expression does not contain any field placeholder")

    if settings and str(settings.get("language") or "").upper() not in {"", "FASTEXPR"}:
        warnings.append(f"non-FASTEXPR language: {settings.get('language')}")

    if compatibility_profile and compatibility_profile.cross_sectional_result_inside_ts_operator:
        errors.append("cross_sectional_result_inside_ts_operator")

    for ident in identifiers:
        if ident in KNOWN_GROUP_IDENTIFIERS:
            continue
        if ident.startswith("{{") and ident.endswith("}}"):
            continue

    field_selection = template.get("field_selection")
    if field_selection is not None and not isinstance(field_selection, dict):
        errors.append("field_selection must be a mapping when present")

    return TemplateValidationResult(
        path=path,
        valid=not errors,
        errors=errors,
        warnings=warnings,
        operators=operators,
        identifiers=identifiers,
        placeholders=placeholders,
    )


def apply_known_rewrite_rules(expression: str) -> tuple[str, list[str]]:
    fixed = expression
    applied: list[str] = []
    for rule in iter_replace_rules():
        if rule.match_text in fixed:
            fixed = fixed.replace(rule.match_text, rule.replacement or "")
            applied.append(rule.description or f"replace {rule.match_text}")
    return fixed, applied


def auto_fix_expression(expression: str) -> tuple[str, list[str]]:
    fixed = expression.strip()
    fixes: list[str] = []

    rewritten, rule_fixes = apply_known_rewrite_rules(fixed)
    if rewritten != fixed:
        fixed = rewritten
        fixes.extend(rule_fixes)

    region_prefix_pattern = re.compile(r"\b(?:USA|EUR|CHN|ASI|GLB|IND)\.([A-Za-z_][A-Za-z0-9_]*)\b")
    new_fixed = region_prefix_pattern.sub(r"\1", fixed)
    if new_fixed != fixed:
        fixed = new_fixed
        fixes.append("removed_region_prefix")

    new_fixed = re.sub(r"\)\s+(\d+)\b", r"), \1", fixed)
    if new_fixed != fixed:
        fixed = new_fixed
        fixes.append("inserted_missing_comma_before_number")

    new_fixed = re.sub(r"\)\s+([A-Za-z_][A-Za-z0-9_]*)\b", r"), \1", fixed)
    if new_fixed != fixed:
        fixed = new_fixed
        fixes.append("inserted_missing_comma_before_identifier")

    new_fixed = re.sub(r"\b([A-Za-z_]+)\1\b", r"\1", fixed)
    if new_fixed != fixed:
        fixed = new_fixed
        fixes.append("collapsed_duplicate_operator_token")

    open_count = fixed.count("(")
    close_count = fixed.count(")")
    if open_count > close_count:
        fixed = fixed + (")" * (open_count - close_count))
        fixes.append("balanced_missing_closing_parens")
    elif close_count > open_count:
        extra = close_count - open_count
        while extra > 0 and fixed.endswith(")"):
            fixed = fixed[:-1]
            extra -= 1
        fixes.append("trimmed_extra_trailing_closing_parens")

    return fixed, fixes


def validate_or_fix_template_file(path: Path, apply_fix: bool = False) -> TemplateValidationResult:
    try:
        template = load_template(path)
    except Exception as exc:
        return TemplateValidationResult(path=path, valid=False, errors=[str(exc)])

    result = validate_template_payload(path, template)
    if result.valid or not apply_fix:
        return result

    expression = str(template.get("expression") or "")
    fixed_expression, fixes = auto_fix_expression(expression)
    if fixed_expression == expression:
        return result

    fixed_template = dict(template)
    fixed_template["expression"] = fixed_expression
    fixed_result = validate_template_payload(path, fixed_template)
    fixed_result.fixed_expression = fixed_expression
    fixed_result.warnings = list(fixed_result.warnings) + [f"auto_fix_applied: {', '.join(fixes)}"]
    if fixed_result.valid:
        for fix in fixes:
            append_rule(
                CompilerKnowledgeRule(
                    rule_type="replace_text",
                    match_text=expression,
                    replacement=fixed_expression,
                    description=fix,
                    created_from=str(path),
                )
            )
    return fixed_result


def validate_template_file(path: Path) -> TemplateValidationResult:
    return validate_or_fix_template_file(path, apply_fix=False)


def iter_template_paths(inputs: Iterable[str]) -> list[Path]:
    paths: list[Path] = []
    for raw in inputs:
        path = Path(raw)
        if not path.exists():
            candidate = TEMPLATES_DIR / raw
            if candidate.exists():
                path = candidate
        if path.is_dir():
            paths.extend(sorted(path.glob("*.yaml")))
            paths.extend(sorted(path.glob("*.yml")))
        else:
            paths.append(path)
    seen: set[Path] = set()
    unique_paths: list[Path] = []
    for path in paths:
        resolved = path.resolve()
        if resolved not in seen:
            seen.add(resolved)
            unique_paths.append(resolved)
    return unique_paths


def print_result(result: TemplateValidationResult, verbose: bool) -> None:
    status = "OK" if result.valid else "ERROR"
    print(f"[{status}] {result.path}")
    for error in result.errors:
        print(f"  error: {error}")
    for warning in result.warnings:
        print(f"  warning: {warning}")
    if verbose:
        print(f"  operators: {', '.join(result.operators) or '<none>'}")
        print(f"  identifiers: {', '.join(result.identifiers) or '<none>'}")
        print(f"  placeholders: {', '.join(result.placeholders) or '<none>'}")
        if result.fixed_expression:
            print(f"  fixed_expression: {result.fixed_expression}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate template files and their FASTEXPR-style expressions.")
    parser.add_argument(
        "paths",
        nargs="*",
        help="Template file(s) or directories. Default: validate alpha_generation/templates",
    )
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--fix", action="store_true", help="Attempt safe autofix on invalid expressions")
    args = parser.parse_args()

    raw_paths = args.paths or [str(TEMPLATES_DIR)]
    template_paths = iter_template_paths(raw_paths)
    if not template_paths:
        print("No template files found.")
        raise SystemExit(1)

    results = [validate_or_fix_template_file(path, apply_fix=args.fix) for path in template_paths]
    invalid_count = 0
    for result in results:
        print_result(result, verbose=args.verbose)
        if not result.valid:
            invalid_count += 1

    print("=" * 72)
    print(f"validated={len(results)} invalid={invalid_count}")
    if invalid_count:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
