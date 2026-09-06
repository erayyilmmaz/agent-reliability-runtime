from __future__ import annotations

import pytest

from agent_runtime.domain.routing import RoutingStrategy, resolve_routing_decision


@pytest.mark.parametrize(
    ("strategy", "expected_provider"),
    [
        (RoutingStrategy.LOWEST_LATENCY, "deterministic"),
        (RoutingStrategy.LOWEST_COST, "deterministic"),
        (RoutingStrategy.QUALITY_FIRST, "openai"),
        (RoutingStrategy.BALANCED, "deterministic"),
    ],
)
def test_rule_engine_selects_provider_from_historical_metrics(
    strategy: RoutingStrategy, expected_provider: str
) -> None:
    decision = resolve_routing_decision(
        {"routing": {"strategy": strategy.value, "candidates": ["openai", "deterministic"]}},
        available_providers={"openai", "deterministic"},
    )

    assert decision["selected_provider"] == expected_provider
    assert decision["provider_order"][0] == expected_provider
    assert decision["metrics_source"] == "provider-catalog.v1"
    assert strategy.value in decision["reason"]
    assert decision["ranked_candidates"][0]["metrics"]["available"] is True


def test_unavailable_provider_is_not_selected_or_fallback_candidate() -> None:
    decision = resolve_routing_decision(
        {
            "routing": {
                "strategy": RoutingStrategy.QUALITY_FIRST.value,
                "candidates": ["openai", "deterministic"],
            }
        },
        available_providers={"deterministic"},
    )

    assert decision["selected_provider"] == "deterministic"
    assert decision["provider_order"] == ["deterministic"]
    assert "1 available candidate" in decision["reason"]


@pytest.mark.parametrize(
    "routing",
    [
        {"strategy": "unknown", "candidates": ["deterministic"]},
        {"candidates": []},
        {"candidates": ["deterministic", "deterministic"]},
        {"candidates": ["unknown"]},
    ],
)
def test_rule_engine_rejects_invalid_routing_contract(routing: dict[str, object]) -> None:
    with pytest.raises(ValueError):
        resolve_routing_decision({"routing": routing})
