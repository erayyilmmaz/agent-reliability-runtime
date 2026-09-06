from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class RoutingStrategy(StrEnum):
    LOWEST_LATENCY = "lowest_latency"
    LOWEST_COST = "lowest_cost"
    QUALITY_FIRST = "quality_first"
    BALANCED = "balanced"


@dataclass(frozen=True)
class ProviderHistoricalMetrics:
    """Trusted rolling metrics used by the V0 routing rule engine.

    The values are intentionally catalog-owned rather than caller supplied. A
    future metrics pipeline can replace this catalogue without changing the
    durable routing-decision contract.
    """

    provider: str
    median_latency_ms: int
    estimated_cost_microusd: int
    evaluation_pass_rate: float
    availability_rate: float

    def as_snapshot(self, *, available: bool) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "median_latency_ms": self.median_latency_ms,
            "estimated_cost_microusd": self.estimated_cost_microusd,
            "evaluation_pass_rate": self.evaluation_pass_rate,
            "availability_rate": self.availability_rate if available else 0.0,
            "available": available,
        }


# V0's versioned catalogue is a deliberately simple historical-metric source.
# It makes routing deterministic and explainable until ARR-E2 gains a live
# provider telemetry aggregation pipeline.
CATALOG_VERSION = "provider-catalog.v1"
DEFAULT_PROVIDER_CATALOG: dict[str, ProviderHistoricalMetrics] = {
    "deterministic": ProviderHistoricalMetrics(
        provider="deterministic",
        median_latency_ms=5,
        estimated_cost_microusd=0,
        evaluation_pass_rate=0.80,
        availability_rate=1.0,
    ),
    "openai": ProviderHistoricalMetrics(
        provider="openai",
        median_latency_ms=800,
        estimated_cost_microusd=1_500,
        evaluation_pass_rate=0.96,
        availability_rate=0.995,
    ),
}


def resolve_routing_decision(
    requested: Mapping[str, Any],
    *,
    available_providers: Iterable[str] | None = None,
    catalog: Mapping[str, ProviderHistoricalMetrics] = DEFAULT_PROVIDER_CATALOG,
) -> dict[str, Any]:
    """Resolve an explainable provider ordering from trusted historical metrics."""

    routing = requested.get("routing", {})
    if not isinstance(routing, Mapping):
        raise ValueError("routing must be an object")

    raw_strategy = routing.get("strategy", RoutingStrategy.BALANCED.value)
    try:
        strategy = RoutingStrategy(raw_strategy)
    except ValueError as exc:
        allowed = ", ".join(strategy.value for strategy in RoutingStrategy)
        raise ValueError(f"routing.strategy must be one of: {allowed}") from exc

    candidates = routing.get("candidates", requested.get("provider_order", ["deterministic"]))
    if (
        not isinstance(candidates, list)
        or not candidates
        or not all(isinstance(candidate, str) and candidate.strip() for candidate in candidates)
    ):
        raise ValueError("routing.candidates must be a non-empty list of provider names")
    if len(set(candidates)) != len(candidates):
        raise ValueError("routing.candidates must not contain duplicates")

    unknown = [candidate for candidate in candidates if candidate not in catalog]
    if unknown:
        raise ValueError(f"routing.candidates contains unknown providers: {', '.join(unknown)}")

    available = set(catalog) if available_providers is None else set(available_providers)
    ranked = [
        _ranked_candidate(catalog[candidate], strategy=strategy, available=candidate in available)
        for candidate in candidates
        if candidate in available
    ]
    if not ranked:
        raise ValueError("routing.candidates has no currently available provider")

    ranked.sort(key=lambda candidate: candidate["sort_key"])
    ordered_candidates = [
        {
            "provider": candidate["provider"],
            "score": candidate["score"],
            "metrics": candidate["metrics"],
        }
        for candidate in ranked
    ]
    selected = ordered_candidates[0]
    return {
        "strategy": strategy.value,
        "metrics_source": CATALOG_VERSION,
        "selected_provider": selected["provider"],
        "provider_order": [candidate["provider"] for candidate in ordered_candidates],
        "reason": _reason(strategy, selected, len(ordered_candidates)),
        "ranked_candidates": ordered_candidates,
    }


def _ranked_candidate(
    metrics: ProviderHistoricalMetrics, *, strategy: RoutingStrategy, available: bool
) -> dict[str, Any]:
    snapshot = metrics.as_snapshot(available=available)
    sort_key: tuple[Any, ...]
    if strategy == RoutingStrategy.LOWEST_LATENCY:
        sort_key = (
            metrics.median_latency_ms,
            metrics.estimated_cost_microusd,
            -metrics.evaluation_pass_rate,
            -metrics.availability_rate,
            metrics.provider,
        )
        score = float(metrics.median_latency_ms)
    elif strategy == RoutingStrategy.LOWEST_COST:
        sort_key = (
            metrics.estimated_cost_microusd,
            metrics.median_latency_ms,
            -metrics.evaluation_pass_rate,
            -metrics.availability_rate,
            metrics.provider,
        )
        score = float(metrics.estimated_cost_microusd)
    elif strategy == RoutingStrategy.QUALITY_FIRST:
        sort_key = (
            -metrics.evaluation_pass_rate,
            -metrics.availability_rate,
            metrics.median_latency_ms,
            metrics.estimated_cost_microusd,
            metrics.provider,
        )
        score = round(1 - metrics.evaluation_pass_rate, 6)
    else:
        # Fixed weights keep V0 deterministic: lower is better. Metrics are
        # normalized against intentionally conservative reference thresholds.
        score = round(
            (metrics.estimated_cost_microusd / 2_000) * 0.35
            + (metrics.median_latency_ms / 1_000) * 0.30
            + (1 - metrics.evaluation_pass_rate) * 0.25
            + (1 - metrics.availability_rate) * 0.10,
            6,
        )
        sort_key = (score, metrics.provider)
    return {
        "provider": metrics.provider,
        "metrics": snapshot,
        "score": score,
        "sort_key": sort_key,
    }


def _reason(strategy: RoutingStrategy, selected: Mapping[str, Any], candidate_count: int) -> str:
    metrics = selected["metrics"]
    if strategy == RoutingStrategy.LOWEST_LATENCY:
        basis = f"lowest historical median latency ({metrics['median_latency_ms']} ms)"
    elif strategy == RoutingStrategy.LOWEST_COST:
        basis = f"lowest estimated cost ({metrics['estimated_cost_microusd']} micro-USD)"
    elif strategy == RoutingStrategy.QUALITY_FIRST:
        basis = f"highest historical evaluation pass rate ({metrics['evaluation_pass_rate']:.3f})"
    else:
        basis = f"lowest balanced cost/latency/quality/availability score ({selected['score']})"
    return (
        f"Selected {selected['provider']} using {strategy.value}: {basis} "
        f"among {candidate_count} available candidate(s)."
    )
