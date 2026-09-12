# Evaluation Regression Suite

ARR-18 compares a candidate provider/model with a baseline against the same,
versioned dataset. It reports quality, latency, and estimated cost separately;
it never mutates the source run or its evaluation history. API requests create
their own durable regression run; the operator CLI executes directly.

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

`POST /v1/evaluation-regressions` requires an authenticated credential and an
`Idempotency-Key`. It accepts the dataset, targets, and optional gates inline,
then returns `202` with `job_id`, `execution_status`, `replayed`, and `status_url`.
Poll `GET /v1/jobs/{job_id}`: its terminal `result` contains the report shape
described above. A successful job means report generation completed; inspect
`result.passed` separately. Provider execution errors fail comparison gates,
even if baseline and candidate fail equally. Quota exhaustion fails the job.

No provider runs inside the HTTP request. Jobs share the transactional outbox,
RabbitMQ worker, manual acknowledgements, and execution lease with regular runs.
They have one attempt: a crash/expired lease is terminal instead of silently
repeating potentially paid calls. Resubmit explicitly with a fresh key after
investigation. Duplicate submissions with the same payload/key return the same job.

The API allows at most 10 cases and a configurable budget of at most 20 calls
(one baseline plus one candidate per case), with principal/tenant quotas as well.
It rejects anonymous local-mode use with `401`. `openai` is accepted only when
`APP_OPENAI_API_KEY` is configured; the API never reads an arbitrary file path
supplied by a caller. The CLI is operator-controlled, not quota-backed API work.
See [resource controls](../security/resource-controls.md) for rule/schema restrictions.
