"""CLI trigger for versioned evaluation regression datasets."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path
from typing import Any

from agent_runtime.evaluation.regression import (
    EvaluationRegressionRunner,
    ProviderModelTarget,
    RegressionDataset,
    RegressionGates,
)
from agent_runtime.providers.deterministic import DeterministicProvider
from agent_runtime.providers.openai_responses import OpenAIResponsesProvider
from agent_runtime.providers.registry import ProviderRegistry
from agent_runtime.settings import Settings, get_settings


def main() -> None:
    args = _parser().parse_args()
    try:
        dataset = RegressionDataset.from_mapping(_load_json(args.dataset))
        baseline = ProviderModelTarget(
            provider=args.baseline_provider,
            model=args.baseline_model,
            estimated_cost_microusd=args.baseline_cost_microusd,
        )
        candidate = ProviderModelTarget(
            provider=args.candidate_provider,
            model=args.candidate_model,
            estimated_cost_microusd=args.candidate_cost_microusd,
        )
        settings = get_settings()
        _validate_configured_targets(settings, baseline, candidate)
        report = asyncio.run(
            EvaluationRegressionRunner(_provider_registry(settings)).run(
                dataset=dataset,
                baseline=baseline,
                candidate=candidate,
                gates=RegressionGates(
                    max_quality_regression_points=args.max_quality_regression_points,
                    max_latency_regression_percent=args.max_latency_regression_percent,
                    max_cost_regression_percent=args.max_cost_regression_percent,
                ),
            )
        )
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise SystemExit(f"Evaluation regression request is invalid: {exc}") from exc

    encoded_report = json.dumps(report, indent=2, sort_keys=True)
    if args.output is not None:
        Path(args.output).write_text(f"{encoded_report}\n", encoding="utf-8")
    print(encoded_report)
    if not report["passed"]:
        raise SystemExit(1)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compare a candidate provider/model against a baseline."
    )
    parser.add_argument("--dataset", required=True, help="Path to a versioned dataset JSON file")
    parser.add_argument("--baseline-provider", required=True)
    parser.add_argument("--baseline-model")
    parser.add_argument("--baseline-cost-microusd", default=0, type=int)
    parser.add_argument("--candidate-provider", required=True)
    parser.add_argument("--candidate-model")
    parser.add_argument("--candidate-cost-microusd", default=0, type=int)
    parser.add_argument("--max-quality-regression-points", default=0.0, type=float)
    parser.add_argument("--max-latency-regression-percent", type=float)
    parser.add_argument("--max-cost-regression-percent", type=float)
    parser.add_argument("--output", help="Optional path for the machine-readable report JSON")
    return parser


def _load_json(path: str) -> dict[str, Any]:
    value = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("dataset root must be an object")
    return value


def _provider_registry(settings: Settings) -> ProviderRegistry:
    return ProviderRegistry(
        [
            DeterministicProvider(),
            OpenAIResponsesProvider(
                api_key=settings.openai_api_key,
                base_url=str(settings.openai_base_url),
                default_model=settings.openai_default_model,
                timeout_seconds=settings.provider_timeout_seconds,
            ),
        ]
    )


def _validate_configured_targets(settings: Settings, *targets: ProviderModelTarget) -> None:
    allowed = {"deterministic", "openai"}
    for target in targets:
        if target.provider not in allowed:
            raise ValueError(f"provider '{target.provider}' is not configured")
        if target.provider == "openai" and settings.openai_api_key is None:
            raise ValueError("openai target requires APP_OPENAI_API_KEY")


if __name__ == "__main__":
    main()
