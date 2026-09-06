from __future__ import annotations

import json
import logging
from typing import Any

from opentelemetry.context import attach, detach
from opentelemetry.trace import NonRecordingSpan, SpanContext, TraceFlags, set_span_in_context

from agent_runtime.infrastructure.messaging.worker import RabbitMqWorker
from agent_runtime.observability.logging import JsonFormatter
from agent_runtime.observability.metrics import RuntimeMetrics, safe_error_code, safe_provider
from agent_runtime.observability.telemetry import inject_trace_context


def test_trace_context_is_injected_and_worker_drops_non_trace_fields() -> None:
    span_context = SpanContext(
        trace_id=1,
        span_id=2,
        is_remote=False,
        trace_flags=TraceFlags(TraceFlags.SAMPLED),
        trace_state=None,
    )
    token = attach(set_span_in_context(NonRecordingSpan(span_context)))
    try:
        trace_context = inject_trace_context()
    finally:
        detach(token)

    body = json.dumps(
        {
            "run_id": "00000000-0000-0000-0000-000000000001",
            "trace_context": trace_context | {"authorization": "raw-secret"},
        }
    ).encode()

    assert trace_context["traceparent"].startswith("00-")
    assert RabbitMqWorker.trace_context_from_message(body) == trace_context


def test_structured_log_allowlists_only_safe_fields() -> None:
    record = logging.LogRecord("arr", logging.INFO, __file__, 1, "run completed", (), None)
    record.run_id = "run-123"
    record.provider = "openai"
    record.prompt = "customer prompt"
    record.api_key = "secret-key"
    record.response = "provider response"

    rendered = json.loads(JsonFormatter().format(record))

    assert rendered["run_id"] == "run-123"
    assert rendered["provider"] == "openai"
    assert "prompt" not in rendered
    assert "api_key" not in rendered
    assert "response" not in rendered
    assert "customer prompt" not in json.dumps(rendered)
    assert "secret-key" not in json.dumps(rendered)


class _Counter:
    def __init__(self, name: str, calls: list[tuple[str, dict[str, str]]]) -> None:
        self._name = name
        self._calls = calls

    def add(self, _: int, attributes: dict[str, str]) -> None:
        self._calls.append((self._name, attributes))


class _Meter:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, str]]] = []

    def create_counter(self, name: str, **_: Any) -> _Counter:
        return _Counter(name, self.calls)


def test_metrics_normalize_unbounded_values_and_never_accept_run_id_labels() -> None:
    meter = _Meter()
    runtime_metrics = RuntimeMetrics(meter)  # type: ignore[arg-type]

    runtime_metrics.run_submitted("customer-defined-provider")
    runtime_metrics.attempt_completed(
        provider="customer-defined-provider", outcome="FAILED", error_code="raw-provider-error"
    )

    assert safe_provider("customer-defined-provider") == "other"
    assert safe_error_code("raw-provider-error") == "other"
    assert meter.calls == [
        ("arr.runs.submitted", {"provider": "other"}),
        (
            "arr.attempts.completed",
            {"provider": "other", "outcome": "FAILED", "error_code": "other"},
        ),
    ]
    assert all("run_id" not in attributes for _, attributes in meter.calls)
