from __future__ import annotations

from typing import Final

from opentelemetry import metrics
from opentelemetry.metrics import Meter

from agent_runtime.domain.retry import ExecutionErrorCode

_KNOWN_PROVIDERS: Final = frozenset({"deterministic", "openai"})
_KNOWN_ERROR_CODES: Final = frozenset(code.value for code in ExecutionErrorCode)


def safe_provider(provider: str) -> str:
    return provider if provider in _KNOWN_PROVIDERS else "other"


def safe_error_code(error_code: str | None) -> str:
    if error_code is None:
        return "none"
    return error_code if error_code in _KNOWN_ERROR_CODES else "other"


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
