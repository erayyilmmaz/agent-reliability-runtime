# Evaluation Regression Suite

ARR-18 compares a candidate provider/model with a baseline against the same,
versioned dataset. It reports quality, latency, and estimated cost separately;
it never mutates a durable run or its evaluation history.

## Dataset contract

Datasets are JSON objects with `dataset_id`, `version`, and non-empty `cases`.
Each case has a unique `case_id`, an `input` object passed to the provider, and
the ordinary deterministic evaluation `rules`. The repository includes
[`evaluation_datasets/v1/core.json`](../../evaluation_datasets/v1/core.json) as
a safe local example.

## Targets, gates, and report

Both baseline and candidate declare a provider, optional model, and an
`estimated_cost_microusd` per case. Cost is intentionally an explicit,
version-controlled estimate rather than a claim about a provider's live bill.
The report schema is `evaluation-regression-report.v1` and contains case-level
PASS/FAIL evidence plus baseline/candidate summaries and independent quality,
latency, and cost deltas.

Quality may regress by zero percentage points by default. Latency and cost are
always reported but gate a run only when their optional percentage thresholds
are configured. This prevents an implicit pricing policy while still making a
candidate's trade-offs visible.

## CLI and API

Run the local deterministic sample:

```bash
APP_ENVIRONMENT=local APP_AUTH_MODE=disabled uv run agent-runtime-regression \
  --dataset evaluation_datasets/v1/core.json \
  --baseline-provider deterministic \
  --candidate-provider deterministic \
  --output regression-report.json
```

The command writes the same machine-readable JSON to standard output and the
optional `--output` path. It exits non-zero when a configured gate fails.

`POST /v1/evaluation-regressions` accepts the dataset, targets, and optional
gates inline and returns the identical report shape. `openai` is accepted only
when `APP_OPENAI_API_KEY` is configured; the API never reads an arbitrary file
path supplied by a caller.
