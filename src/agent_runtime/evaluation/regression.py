"""Versioned provider/model evaluation regression runner."""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from agent_runtime.application.execution import ExecutionResult
from agent_runtime.evaluation.engine import evaluate_rules

REPORT_SCHEMA_VERSION = "evaluation-regression-report.v1"


class RegressionExecutor(Protocol):
    async def execute(
        self,
        *,
        provider: str,
        input_payload: dict[str, Any],
        policy_snapshot: dict[str, Any],
    ) -> ExecutionResult: ...


@dataclass(frozen=True)
class RegressionCase:
    case_id: str
    input_payload: dict[str, Any]
    rules: list[dict[str, Any]]


@dataclass(frozen=True)
class RegressionDataset:
    dataset_id: str
    version: str
    cases: tuple[RegressionCase, ...]

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> RegressionDataset:
        dataset_id = value.get("dataset_id")
        version = value.get("version")
        raw_cases = value.get("cases")
        if not isinstance(dataset_id, str) or not dataset_id.strip():
            raise ValueError("dataset_id must be a non-empty string")
        if not isinstance(version, str) or not version.strip():
            raise ValueError("dataset.version must be a non-empty string")
        if not isinstance(raw_cases, list) or not raw_cases:
            raise ValueError("dataset.cases must be a non-empty list")

        cases: list[RegressionCase] = []
        case_ids: set[str] = set()
        for raw_case in raw_cases:
            if not isinstance(raw_case, Mapping):
                raise ValueError("every dataset case must be an object")
            case_id = raw_case.get("case_id")
            input_payload = raw_case.get("input")
            rules = raw_case.get("rules")
            if not isinstance(case_id, str) or not case_id.strip():
                raise ValueError("every dataset case requires a non-empty case_id")
            if case_id in case_ids:
                raise ValueError("dataset case_id values must be unique")
            if not isinstance(input_payload, dict) or not input_payload:
                raise ValueError("every dataset case requires a non-empty input object")
            if (
                not isinstance(rules, list)
                or not rules
                or not all(isinstance(rule, dict) for rule in rules)
            ):
                raise ValueError("every dataset case requires a non-empty rules list")
            case_ids.add(case_id)
            cases.append(
                RegressionCase(
                    case_id=case_id,
                    input_payload=dict(input_payload),
                    rules=[dict(rule) for rule in rules],
                )
            )
        return cls(dataset_id=dataset_id, version=version, cases=tuple(cases))


@dataclass(frozen=True)
class ProviderModelTarget:
    provider: str
    model: str | None = None
    estimated_cost_microusd: int = 0

    def __post_init__(self) -> None:
        if not self.provider.strip():
            raise ValueError("target.provider must be a non-empty string")
        if self.model is not None and not self.model.strip():
            raise ValueError("target.model must be non-empty when provided")
        if self.estimated_cost_microusd < 0:
            raise ValueError("target.estimated_cost_microusd must be non-negative")

    def as_dict(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "estimated_cost_microusd": self.estimated_cost_microusd,
        }


@dataclass(frozen=True)
class RegressionGates:
    max_quality_regression_points: float = 0.0
    max_latency_regression_percent: float | None = None
    max_cost_regression_percent: float | None = None

    def __post_init__(self) -> None:
        values = (
            self.max_quality_regression_points,
            self.max_latency_regression_percent,
            self.max_cost_regression_percent,
        )
        if any(value is not None and value < 0 for value in values):
            raise ValueError("regression gates must be non-negative")


class EvaluationRegressionRunner:
    """Runs one versioned dataset against a baseline and candidate target."""

    def __init__(self, executor: RegressionExecutor) -> None:
        self._executor = executor

    async def run(
        self,
        *,
        dataset: RegressionDataset,
        baseline: ProviderModelTarget,
        candidate: ProviderModelTarget,
        gates: RegressionGates | None = None,
    ) -> dict[str, Any]:
        gates = gates or RegressionGates()
        baseline_result = await self._run_target(dataset, baseline)
        candidate_result = await self._run_target(dataset, candidate)
        comparison = _comparison(baseline_result, candidate_result, gates)
        return {
            "schema_version": REPORT_SCHEMA_VERSION,
            "dataset": {
                "dataset_id": dataset.dataset_id,
                "version": dataset.version,
                "case_count": len(dataset.cases),
            },
            "baseline": baseline_result,
            "candidate": candidate_result,
            "comparison": comparison,
            "passed": comparison["passed"],
        }

    async def _run_target(
        self, dataset: RegressionDataset, target: ProviderModelTarget
    ) -> dict[str, Any]:
        case_results: list[dict[str, Any]] = []
        for case in dataset.cases:
            started_at = time.perf_counter()
            try:
                result = await self._executor.execute(
                    provider=target.provider,
                    input_payload=case.input_payload,
                    policy_snapshot=_target_policy(target),
                )
                latency_ms = round((time.perf_counter() - started_at) * 1000)
                outcome = evaluate_rules(
                    rules=case.rules,
                    result_payload=result.result_payload,
                    latency_ms=latency_ms,
                )
                case_results.append(
                    {
                        "case_id": case.case_id,
                        "passed": outcome.passed,
                        "latency_ms": latency_ms,
                        "estimated_cost_microusd": target.estimated_cost_microusd,
                        "rules": outcome.rule_results,
                    }
                )
            except Exception as exc:
                latency_ms = round((time.perf_counter() - started_at) * 1000)
                case_results.append(
                    {
                        "case_id": case.case_id,
                        "passed": False,
                        "latency_ms": latency_ms,
                        "estimated_cost_microusd": target.estimated_cost_microusd,
                        "error_type": type(exc).__name__,
                    }
                )
        passed_count = sum(case["passed"] for case in case_results)
        return {
            "target": target.as_dict(),
            "cases": case_results,
            "summary": {
                "passed_cases": passed_count,
                "failed_cases": len(case_results) - passed_count,
                "pass_rate": round(passed_count / len(case_results), 6),
                "average_latency_ms": round(
                    sum(case["latency_ms"] for case in case_results) / len(case_results), 3
                ),
                "estimated_cost_microusd": sum(
                    case["estimated_cost_microusd"] for case in case_results
                ),
            },
        }


def _target_policy(target: ProviderModelTarget) -> dict[str, Any]:
    policy: dict[str, Any] = {"provider_order": [target.provider]}
    if target.model is not None:
        policy["model"] = target.model
    return policy


def _comparison(
    baseline: Mapping[str, Any], candidate: Mapping[str, Any], gates: RegressionGates
) -> dict[str, Any]:
    baseline_summary = _summary(baseline)
    candidate_summary = _summary(candidate)
    quality_delta_points = round(
        (candidate_summary["pass_rate"] - baseline_summary["pass_rate"]) * 100, 3
    )
    latency = _metric_comparison(
        baseline_summary["average_latency_ms"],
        candidate_summary["average_latency_ms"],
        gates.max_latency_regression_percent,
    )
    cost = _metric_comparison(
        baseline_summary["estimated_cost_microusd"],
        candidate_summary["estimated_cost_microusd"],
        gates.max_cost_regression_percent,
    )
    quality_regressed = quality_delta_points < -gates.max_quality_regression_points
    return {
        "quality": {
            "baseline_pass_rate": baseline_summary["pass_rate"],
            "candidate_pass_rate": candidate_summary["pass_rate"],
            "delta_points": quality_delta_points,
            "max_regression_points": gates.max_quality_regression_points,
            "regressed": quality_regressed,
        },
        "latency": latency,
        "cost": cost,
        "passed": not quality_regressed and not latency["regressed"] and not cost["regressed"],
    }


def _summary(target_result: Mapping[str, Any]) -> Mapping[str, float | int]:
    summary = target_result.get("summary")
    if not isinstance(summary, Mapping):
        raise RuntimeError("regression target result is missing its summary")
    return summary


def _metric_comparison(
    baseline: float | int, candidate: float | int, allowed_percent: float | None
) -> dict[str, Any]:
    absolute_delta = round(candidate - baseline, 3)
    percent_delta: float | None
    if baseline == 0:
        percent_delta = 0.0 if candidate == 0 else None
    else:
        percent_delta = round((candidate - baseline) / baseline * 100, 3)
    if allowed_percent is None:
        regressed = False
    elif percent_delta is None:
        regressed = candidate > baseline
    else:
        regressed = percent_delta > allowed_percent
    return {
        "baseline": baseline,
        "candidate": candidate,
        "absolute_delta": absolute_delta,
        "percent_delta": percent_delta,
        "max_regression_percent": allowed_percent,
        "regressed": regressed,
    }
