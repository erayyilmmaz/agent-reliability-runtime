# Policy-aware provider routing

ARR-17 turns the immutable `policy.provider_order` fallback list into an
explainable routing decision made when a run is accepted. The worker still uses
that persisted order; it does not make a new selection during execution.

## Contract

`POST /v1/runs` accepts an optional policy object:

```json
{
  "routing": {
    "strategy": "quality_first",
    "candidates": ["openai", "deterministic"]
  }
}
```

Supported strategies are `lowest_latency`, `lowest_cost`, `quality_first`, and
`balanced`. Omitting routing preserves the safe `deterministic` default.

The acceptance path filters candidates that are not configured for this runtime
(for example, `openai` without `APP_OPENAI_API_KEY`). It rejects unknown,
duplicate, empty, or entirely unavailable candidate lists with a 422 response.

## Explainability and durability

The accepted policy snapshot stores the ranked `provider_order` and a
`routing.decision` record. The create and get-run responses expose that record;
the database also records a `ROUTING_DECISION_RECORDED` run event with its
strategy, selected provider, reason, and metric source. A retry or provider
fallback therefore follows the original decision even if the catalog changes.

Each decision includes the selected provider, all ranked candidates, their
latency/cost/evaluation-pass-rate/availability values, a deterministic score,
and human-readable reason.

## Metric source and current boundary

V0 deliberately uses the versioned, trusted `provider-catalog.v1` historical
baseline in `agent_runtime.domain.routing`. Callers cannot provide metric values.
This is a deterministic rule engine, not a live billing or telemetry system.
Replacing the catalog with a governed rolling-metrics pipeline can retain the
same persisted decision contract while making future decisions data-driven.
