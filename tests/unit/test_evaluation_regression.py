from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest

from agent_runtime.application.execution import ExecutionResult
from agent_runtime.evaluation import (
    EvaluationRegressionRunner,
    ProviderModelTarget,
    RegressionDataset,
    RegressionGates,
    cli,
)


class FakeRegressionExecutor:
    def __init__(self, results: dict[str, dict[str, Any]]) -> None:
        self._results = results
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def execute(
        self,
        *,
        provider: str,
        input_payload: dict[str, Any],
        policy_snapshot: dict[str, Any],
    ) -> ExecutionResult:
        self.calls.append((provider, policy_snapshot))
        return ExecutionResult(provider=provider, result_payload=self._results[provider])


def _dataset() -> RegressionDataset:
    return RegressionDataset.from_mapping(
        {
            "dataset_id": "regression-fixture",
            "version": "1.0.0",
            "cases": [
                {
                    "case_id": "answer-is-ready",
                    "input": {"prompt": "go"},
                    "rules": [
                        {"type": "rule", "path": "answer", "operator": "equals", "value": "ready"}
                    ],
                }
            ],
        }
    )


@pytest.mark.asyncio
async def test_report_separates_quality_latency_and_cost_regressions() -> None:
    runner = EvaluationRegressionRunner(
        FakeRegressionExecutor({"baseline": {"answer": "ready"}, "candidate": {"answer": "wrong"}})
    )

    report = await runner.run(
        dataset=_dataset(),
        baseline=ProviderModelTarget("baseline", model="base-v1", estimated_cost_microusd=10),
        candidate=ProviderModelTarget(
            "candidate", model="candidate-v2", estimated_cost_microusd=25
        ),
        gates=RegressionGates(max_cost_regression_percent=50),
    )

    assert report["schema_version"] == "evaluation-regression-report.v1"
    assert report["baseline"]["summary"]["pass_rate"] == 1.0
    assert report["candidate"]["summary"]["pass_rate"] == 0.0
    assert report["comparison"]["quality"]["regressed"] is True
    assert report["comparison"]["cost"]["absolute_delta"] == 15
    assert report["comparison"]["cost"]["regressed"] is True
    assert report["passed"] is False


@pytest.mark.asyncio
async def test_report_keeps_latency_and_cost_visible_without_optional_gates() -> None:
    runner = EvaluationRegressionRunner(
        FakeRegressionExecutor({"baseline": {"answer": "ready"}, "candidate": {"answer": "ready"}})
    )

    report = await runner.run(
        dataset=_dataset(),
        baseline=ProviderModelTarget("baseline", estimated_cost_microusd=0),
        candidate=ProviderModelTarget("candidate", estimated_cost_microusd=50),
    )

    assert report["comparison"]["quality"]["regressed"] is False
    assert report["comparison"]["latency"]["baseline"] >= 0
    assert report["comparison"]["cost"]["percent_delta"] is None
    assert report["comparison"]["cost"]["regressed"] is False
    assert report["passed"] is True


@pytest.mark.parametrize(
    "dataset",
    [
        {"dataset_id": "x", "version": "1", "cases": []},
        {"dataset_id": "x", "version": "1", "cases": [{"case_id": "a", "input": {}, "rules": []}]},
        {
            "dataset_id": "x",
            "version": "1",
            "cases": [
                {"case_id": "a", "input": {"x": 1}, "rules": [{"type": "non_empty"}]},
                {"case_id": "a", "input": {"x": 2}, "rules": [{"type": "non_empty"}]},
            ],
        },
    ],
)
def test_dataset_contract_rejects_invalid_cases(dataset: dict[str, Any]) -> None:
    with pytest.raises(ValueError):
        RegressionDataset.from_mapping(dataset)


def test_cli_writes_machine_readable_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    dataset_path = tmp_path / "dataset.json"
    output_path = tmp_path / "report.json"
    dataset_path.write_text(
        json.dumps(
            {
                "dataset_id": "cli-fixture",
                "version": "1.0.0",
                "cases": [
                    {
                        "case_id": "preserves-prompt",
                        "input": {"prompt": "ready"},
                        "rules": [
                            {
                                "type": "rule",
                                "path": "accepted_input.prompt",
                                "operator": "equals",
                                "value": "ready",
                            }
                        ],
                    }
                ],
            }
        )
    )
    monkeypatch.setattr(
        sys,
        "argv",
        [
            "agent-runtime-regression",
            "--dataset",
            str(dataset_path),
            "--baseline-provider",
            "deterministic",
            "--candidate-provider",
            "deterministic",
            "--output",
            str(output_path),
        ],
    )

    cli.main()

    assert json.loads(output_path.read_text())["passed"] is True
