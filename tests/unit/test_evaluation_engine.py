from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from agent_runtime.evaluation import EvaluationConfigurationError, evaluate_rules

REGRESSION_FIXTURE_PATH = Path(__file__).parents[1] / "fixtures" / "evaluation_regressions.json"
REGRESSION_FIXTURES = cast(list[dict[str, Any]], json.loads(REGRESSION_FIXTURE_PATH.read_text()))


@pytest.mark.parametrize("fixture", REGRESSION_FIXTURES, ids=lambda fixture: fixture["name"])
def test_regression_fixtures(fixture: dict[str, Any]) -> None:
    outcome = evaluate_rules(
        rules=fixture["rules"],
        result_payload=fixture["result_payload"],
        latency_ms=fixture["latency_ms"],
    )

    assert outcome.passed is fixture["passed"]


def test_all_deterministic_evaluators_pass() -> None:
    outcome = evaluate_rules(
        rules=[
            {"type": "non_empty", "path": "answer"},
            {
                "type": "json_schema",
                "path": "metadata",
                "schema": {
                    "type": "object",
                    "required": ["source"],
                    "properties": {"source": {"type": "string"}},
                },
            },
            {"type": "latency_budget", "max_ms": 100},
            {"type": "rule", "path": "answer", "operator": "contains", "value": "ready"},
        ],
        result_payload={"answer": "ready for review", "metadata": {"source": "fixture"}},
        latency_ms=42,
    )

    assert outcome.passed is True
    assert [result["type"] for result in outcome.rule_results] == [
        "non_empty",
        "json_schema",
        "latency_budget",
        "rule",
    ]


def test_failed_rule_is_a_quality_failure_not_an_engine_error() -> None:
    outcome = evaluate_rules(
        rules=[{"type": "non_empty", "path": "answer"}],
        result_payload={"answer": "  "},
        latency_ms=1,
    )

    assert outcome.passed is False
    assert outcome.rule_results[0]["message"] == "value is empty"


@pytest.mark.parametrize(
    "rule",
    [
        {"type": "unknown"},
        {"type": "latency_budget", "max_ms": -1},
        {"type": "json_schema", "schema": "not-an-object"},
        {"type": "rule", "path": "answer", "operator": "matches", "value": "["},
    ],
)
def test_invalid_evaluator_configuration_raises(rule: dict[str, object]) -> None:
    with pytest.raises(EvaluationConfigurationError):
        evaluate_rules(rules=[rule], result_payload={"answer": "ok"}, latency_ms=1)
