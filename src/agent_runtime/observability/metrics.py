from __future__ import annotations

from typing import Final

from opentelemetry import metrics
from opentelemetry.metrics import Meter

from agent_runtime.domain.retry import ExecutionErrorCode

_KNOWN_PROVIDERS: Final = frozenset({"deterministic", "openai"})
_KNOWN_ERROR_CODES: Final = frozenset(code.value for code in ExecutionErrorCode)
_KNOWN_DB_OPERATIONS: Final = frozenset(
    {"submit", "get_run", "get_attempts", "get_events", "get_evaluations", "evaluate", "replay"}
)

# PERF-006: the OpenTelemetry semantic convention for HTTP server duration.
# Chosen so the data is portable to any dashboard that understands semconv.
HTTP_DURATION_BUCKETS: Final = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.075,
    0.1,
    0.25,
    0.5,
    0.75,
    1.0,
    2.5,
    5.0,
    7.5,
    10.0,
)
# Internal work is faster than a request, so it needs finer low-end resolution:
# the hot query measures 0.028 ms and would otherwise land in a single bucket.
INTERNAL_DURATION_BUCKETS: Final = (
    0.0005,
    0.001,
    0.0025,
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
)
# Provider calls and outbox lag can legitimately run to minutes: the provider
# timeout alone defaults to 60 s, and a broker outage stalls the outbox for as
# long as it lasts. These need their own boundaries, because
# histogram_quantile can never return more than the largest finite bucket --
# reusing the 10 s HTTP boundaries would silently make any alert threshold
# above 10 s unreachable.
SLOW_OPERATION_BUCKETS: Final = (
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
    60.0,
    120.0,
    300.0,
)


def safe_provider(provider: str) -> str:
    return provider if provider in _KNOWN_PROVIDERS else "other"


def safe_error_code(error_code: str | None) -> str:
    if error_code is None:
        return "none"
    return error_code if error_code in _KNOWN_ERROR_CODES else "other"


def safe_route(route: str | None) -> str:
    """Collapse an unmatched path to a constant.

    A raw URL as a metric label is the classic cardinality explosion: every
    run UUID would create its own time series. Only a matched route template
    is ever emitted.
    """

    return route if route else "unmatched"


def safe_db_operation(operation: str) -> str:
    return operation if operation in _KNOWN_DB_OPERATIONS else "other"


def status_class(status_code: int) -> str:
    """2xx/4xx/5xx rather than the exact code, to keep the series count flat."""

    return f"{status_code // 100}xx"


class RuntimeMetrics:
    """Low-cardinality domain metrics. Run and attempt IDs are deliberately excluded."""

    def __init__(self, meter: Meter) -> None:
        self._runs_submitted = meter.create_counter("arr.runs.submitted", unit="{run}")
        self._attempts_started = meter.create_counter("arr.attempts.started", unit="{attempt}")
        self._attempts_completed = meter.create_counter("arr.attempts.completed", unit="{attempt}")
        self._retries_scheduled = meter.create_counter("arr.retries.scheduled", unit="{retry}")
        self._provider_fallbacks = meter.create_counter("arr.provider.fallbacks", unit="{fallback}")
        self._outbox_dispatches = meter.create_counter("arr.outbox.dispatches", unit="{event}")
        self._security = meter.create_counter("arr.security.events", unit="{event}")
        self._provider_calls = meter.create_counter("arr.provider.calls", unit="{call}")

        # PERF-006: before these, every instrument was a counter and no
        # percentile of anything could be computed from the metrics this
        # runtime emits. Labels stay deliberately low-cardinality: route
        # templates, never raw paths; status classes, never exact codes; and
        # never run_id, attempt_id or principal_id.
        self._http_duration = meter.create_histogram(
            "http.server.request.duration",
            unit="s",
            description="Duration of inbound HTTP requests.",
            explicit_bucket_boundaries_advisory=list(HTTP_DURATION_BUCKETS),
        )
        self._db_duration = meter.create_histogram(
            "arr.db.operation.duration",
            unit="s",
            description="Duration of a named persistence operation.",
            explicit_bucket_boundaries_advisory=list(INTERNAL_DURATION_BUCKETS),
        )
        self._provider_duration = meter.create_histogram(
            "arr.provider.call.duration",
            unit="s",
            description="Duration of an outbound provider call.",
            explicit_bucket_boundaries_advisory=list(SLOW_OPERATION_BUCKETS),
        )
        self._outbox_lag = meter.create_histogram(
            "arr.outbox.lag",
            unit="s",
            description="Age of an outbox event when it is published.",
            explicit_bucket_boundaries_advisory=list(SLOW_OPERATION_BUCKETS),
        )

    def http_request(
        self, *, route: str | None, method: str, status_code: int, seconds: float
    ) -> None:
        self._http_duration.record(
            seconds,
            {
                "http.route": safe_route(route),
                "http.request.method": method,
                "http.response.status_class": status_class(status_code),
            },
        )

    def db_operation(self, operation: str, seconds: float) -> None:
        self._db_duration.record(seconds, {"operation": safe_db_operation(operation)})

    def provider_call_duration(self, provider: str, seconds: float) -> None:
        self._provider_duration.record(seconds, {"provider": safe_provider(provider)})

    def outbox_lag(self, event_type: str, seconds: float) -> None:
        self._outbox_lag.record(seconds, {"event_type": event_type})

    def security_event(self, category: str, outcome: str) -> None:
        self._security.add(
            1,
            {
                "category": category
                if category
                in {"auth", "rate_limit", "audit_write", "quota", "sensitive_read", "trace_context"}
                else "other",
                "outcome": outcome
                if outcome in {"allowed", "denied", "error", "success", "dropped"}
                else "other",
            },
        )

    def provider_call(self, provider: str) -> None:
        self._provider_calls.add(1, {"provider": safe_provider(provider)})

    def run_submitted(self, provider: str) -> None:
        self._runs_submitted.add(1, {"provider": safe_provider(provider)})

    def attempt_started(self, provider: str) -> None:
        self._attempts_started.add(1, {"provider": safe_provider(provider)})

    def attempt_completed(self, *, provider: str, outcome: str, error_code: str | None) -> None:
        self._attempts_completed.add(
            1,
            {
                "provider": safe_provider(provider),
                "outcome": outcome,
                "error_code": safe_error_code(error_code),
            },
        )

    def retry_scheduled(self, error_code: str) -> None:
        self._retries_scheduled.add(1, {"error_code": safe_error_code(error_code)})

    def provider_fallback(self, *, from_provider: str, to_provider: str) -> None:
        self._provider_fallbacks.add(
            1,
            {
                "from_provider": safe_provider(from_provider),
                "to_provider": safe_provider(to_provider),
            },
        )

    def outbox_dispatch(self, *, event_type: str, outcome: str) -> None:
        self._outbox_dispatches.add(1, {"event_type": event_type, "outcome": outcome})


_runtime_metrics = RuntimeMetrics(metrics.get_meter("agent_runtime"))


def get_runtime_metrics() -> RuntimeMetrics:
    return _runtime_metrics
