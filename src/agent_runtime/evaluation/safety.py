"""Deliberately restricted rules and JSON Schema subset with bounded traversal."""

from __future__ import annotations

import math
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError


class EvaluationConfigurationError(ValueError):
    pass


def bounded_value(value: Any, *, max_nodes: int = 10_000, max_depth: int = 16) -> None:
    pending = [(value, 0)]
    nodes = 0
    size = 0
    while pending:
        item, depth = pending.pop()
        nodes += 1
        if nodes > max_nodes or depth > max_depth:
            raise EvaluationConfigurationError("evaluation value complexity limit exceeded")
        if isinstance(item, dict):
            if len(item) > max_nodes:
                raise EvaluationConfigurationError("evaluation object limit exceeded")
            pending.extend((v, depth + 1) for v in item.values())
            try:
                size += sum(len(str(k).encode("utf-8")) for k in item)
            except UnicodeError:
                raise EvaluationConfigurationError("invalid evaluation text") from None
        elif isinstance(item, list):
            if len(item) > max_nodes:
                raise EvaluationConfigurationError("evaluation array limit exceeded")
            pending.extend((v, depth + 1) for v in item)
        elif isinstance(item, str):
            try:
                size += len(item.encode("utf-8"))
            except UnicodeError:
                raise EvaluationConfigurationError("invalid evaluation text") from None
        elif isinstance(item, float) and not math.isfinite(item):
            raise EvaluationConfigurationError("evaluation values must be finite")
        if size > 65_536 or len(pending) > max_nodes:
            raise EvaluationConfigurationError("evaluation value size limit exceeded")


def validate_schema(schema: Any) -> None:
    allowed = {
        "type",
        "properties",
        "required",
        "additionalProperties",
        "items",
        "enum",
        "const",
        "minimum",
        "maximum",
        "minLength",
        "maxLength",
        "minItems",
        "maxItems",
    }
    pending = [(schema, 0)]
    count = 0
    while pending:
        node, depth = pending.pop()
        count += 1
        if not isinstance(node, dict) or count > 128 or depth > 6 or set(node) - allowed:
            raise EvaluationConfigurationError("unsupported or overly complex JSON Schema")
        properties = node.get("properties", {})
        if not isinstance(properties, dict) or len(properties) > 32:
            raise EvaluationConfigurationError("schema properties limit exceeded")
        if any(len(key) > 128 for key in properties):
            raise EvaluationConfigurationError("schema property name limit exceeded")
        pending.extend((child, depth + 1) for child in properties.values())
        if "items" in node:
            pending.append((node["items"], depth + 1))
        if "additionalProperties" in node and not isinstance(node["additionalProperties"], bool):
            raise EvaluationConfigurationError("additionalProperties must be a boolean")
        if "enum" in node and (not isinstance(node["enum"], list) or len(node["enum"]) > 32):
            raise EvaluationConfigurationError("schema enum limit exceeded")
    try:
        Draft202012Validator.check_schema(schema)
    except SchemaError:
        raise EvaluationConfigurationError("invalid JSON Schema") from None


def validate_rules(rules: list[dict[str, Any]]) -> None:
    bounded_value(rules, max_nodes=2048, max_depth=16)
    if not 1 <= len(rules) <= 16:
        raise EvaluationConfigurationError("evaluation requires 1-16 rules")
    for rule in rules:
        kind = rule.get("type")
        allowed = {
            "non_empty": {"type", "path"},
            "json_schema": {"type", "path", "schema"},
            "latency_budget": {"type", "max_ms"},
            "rule": {"type", "path", "operator", "value"},
        }
        if not isinstance(kind, str) or kind not in allowed or set(rule) - allowed[kind]:
            raise EvaluationConfigurationError("unsupported evaluation rule or field")
        path = rule.get("path", "")
        if not isinstance(path, str) or len(path) > 256 or len(path.split(".")) > 16:
            raise EvaluationConfigurationError("invalid evaluation path")
        if kind == "json_schema":
            validate_schema(rule.get("schema"))
        elif kind == "rule":
            operator = rule.get("operator")
            if not isinstance(operator, str) or operator not in {
                "exists",
                "equals",
                "not_equals",
                "contains",
            }:
                raise EvaluationConfigurationError(
                    "unsupported operator; regex matches is disabled"
                )
        elif kind == "latency_budget":
            budget = rule.get("max_ms")
            if type(budget) is not int or not 0 <= budget <= 86_400_000:
                raise EvaluationConfigurationError("invalid latency budget")
