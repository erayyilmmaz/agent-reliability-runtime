"""Deterministic, side-effect-free evaluation rules for completed run output."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import ValidationError

from agent_runtime.evaluation.safety import (
    EvaluationConfigurationError,
    bounded_value,
    validate_rules,
)


@dataclass(frozen=True)
class EvaluationOutcome:
    passed: bool
    rule_results: list[dict[str, Any]]

    def as_persisted_result(self) -> dict[str, Any]:
        return {"passed": self.passed, "rules": self.rule_results}


def evaluate_rules(
    *, rules: list[dict[str, Any]], result_payload: dict[str, Any], latency_ms: int | None
) -> EvaluationOutcome:
    """Apply configured checks without retaining a provider response in the result."""

    validate_rules(rules)
    bounded_value(result_payload)

    results = [
        _evaluate_rule(rule, result_payload=result_payload, latency_ms=latency_ms) for rule in rules
    ]
    return EvaluationOutcome(passed=all(item["passed"] for item in results), rule_results=results)


def _evaluate_rule(
    rule: dict[str, Any], *, result_payload: dict[str, Any], latency_ms: int | None
) -> dict[str, Any]:
    kind = rule.get("type")
    if not isinstance(kind, str):
        raise EvaluationConfigurationError("every evaluation rule requires a string type")
    path = rule.get("path", "")
    if not isinstance(path, str):
        raise EvaluationConfigurationError("evaluation rule path must be a string")

    if kind == "non_empty":
        value, found = _value_at_path(result_payload, path)
        passed = found and _is_non_empty(value)
        return _rule_result(
            kind, path, passed, "value is non-empty" if passed else "value is empty"
        )

    if kind == "json_schema":
        schema = rule.get("schema")
        if not isinstance(schema, dict):
            raise EvaluationConfigurationError("json_schema rule requires an object schema")
        value, found = _value_at_path(result_payload, path)
        if not found:
            return _rule_result(kind, path, False, "path was not found")
        try:
            Draft202012Validator(schema).validate(value)
        except ValidationError:
            return _rule_result(kind, path, False, "schema validation failed")
        return _rule_result(kind, path, True, "schema validation passed")

    if kind == "latency_budget":
        budget = rule.get("max_ms")
        if not isinstance(budget, int) or isinstance(budget, bool) or budget < 0:
            raise EvaluationConfigurationError(
                "latency_budget rule requires non-negative integer max_ms"
            )
        passed = latency_ms is not None and latency_ms <= budget
        message = "latency is within budget" if passed else "latency budget exceeded or unavailable"
        return _rule_result(kind, "", passed, message)

    if kind == "rule":
        operator = rule.get("operator")
        if operator not in {"exists", "equals", "not_equals", "contains"}:
            raise EvaluationConfigurationError(
                "rule evaluator operator must be exists, equals, not_equals, or contains"
            )
        value, found = _value_at_path(result_payload, path)
        expected = rule.get("value")
        if operator == "exists":
            passed = found
        elif operator == "equals":
            passed = found and value == expected
        elif operator == "not_equals":
            passed = found and value != expected
        else:
            passed = found and (
                (isinstance(value, list) and expected in value)
                or (
                    isinstance(value, (str, dict))
                    and isinstance(expected, str)
                    and expected in value
                )
            )
        return _rule_result(
            kind, path, passed, f"{operator} check {'passed' if passed else 'failed'}"
        )

    raise EvaluationConfigurationError(f"unsupported evaluation rule type: {kind}")


def _value_at_path(payload: Any, path: str) -> tuple[Any, bool]:
    value = payload
    if not path:
        return value, True
    for part in path.split("."):
        if isinstance(value, dict) and part in value:
            value = value[part]
        elif (
            isinstance(value, list)
            and len(part) <= 10
            and part.isdigit()
            and int(part) < len(value)
        ):
            value = value[int(part)]
        else:
            return None, False
    return value, True


def _is_non_empty(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        return bool(value.strip())
    if isinstance(value, (list, dict, tuple, set)):
        return bool(value)
    return True


def _rule_result(kind: str, path: str, passed: bool, message: str) -> dict[str, Any]:
    return {"type": kind, "path": path, "passed": passed, "message": message}
