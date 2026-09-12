from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from enum import StrEnum
from typing import Any

from agent_runtime.domain.policy import RunPolicyRequest
from agent_runtime.domain.routing import resolve_routing_decision


class ExecutionErrorCode(StrEnum):
    PROVIDER_TIMEOUT = "PROVIDER_TIMEOUT"
    PROVIDER_RATE_LIMITED = "PROVIDER_RATE_LIMITED"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    PROVIDER_SERVER_ERROR = "PROVIDER_SERVER_ERROR"
    PROVIDER_AUTH_FAILED = "PROVIDER_AUTH_FAILED"
    PROVIDER_BAD_REQUEST = "PROVIDER_BAD_REQUEST"
    EXECUTION_ERROR = "EXECUTION_ERROR"
    EXECUTION_LEASE_EXPIRED = "EXECUTION_LEASE_EXPIRED"
    RESOURCE_QUOTA_EXCEEDED = "RESOURCE_QUOTA_EXCEEDED"


RETRYABLE_ERROR_CODES = frozenset(
    {
        ExecutionErrorCode.PROVIDER_TIMEOUT,
        ExecutionErrorCode.PROVIDER_RATE_LIMITED,
        ExecutionErrorCode.PROVIDER_UNAVAILABLE,
        ExecutionErrorCode.PROVIDER_SERVER_ERROR,
        ExecutionErrorCode.EXECUTION_ERROR,
        ExecutionErrorCode.EXECUTION_LEASE_EXPIRED,
    }
)


@dataclass(frozen=True)
class RetryPolicy:
    """The immutable retry contract stored with each accepted run."""

    max_attempts: int
    attempt_timeout_seconds: float
    initial_backoff_seconds: float
    max_backoff_seconds: float
    provider_order: tuple[str, ...]

    def __post_init__(self) -> None:
        if not 1 <= self.max_attempts <= 20:
            raise ValueError("max_attempts must be between 1 and 20")
        if not all(
            math.isfinite(v)
            for v in (
                self.attempt_timeout_seconds,
                self.initial_backoff_seconds,
                self.max_backoff_seconds,
            )
        ):
            raise ValueError("retry values must be finite")
        if self.attempt_timeout_seconds <= 0:
            raise ValueError("attempt_timeout_seconds must be greater than 0")
        if self.initial_backoff_seconds <= 0:
            raise ValueError("initial_backoff_seconds must be greater than 0")
        if self.max_backoff_seconds < self.initial_backoff_seconds:
            raise ValueError("max_backoff_seconds must be at least initial_backoff_seconds")
        if not self.provider_order or any(not provider.strip() for provider in self.provider_order):
            raise ValueError("provider_order must contain at least one non-empty provider")

    def delay_after_attempt(self, attempt_number: int) -> float:
        """Return a bounded exponential delay; attempt one uses the initial delay."""

        if attempt_number < 1:
            raise ValueError("attempt_number must be at least 1")
        return max(
            1.0,
            float(
                min(
                    self.max_backoff_seconds,
                    self.initial_backoff_seconds * (2 ** min(attempt_number - 1, 20)),
                )
            ),
        )

    def as_snapshot(self) -> dict[str, Any]:
        return asdict(self) | {"provider_order": list(self.provider_order)}

    @classmethod
    def from_snapshot(cls, value: Mapping[str, Any]) -> RetryPolicy:
        provider_order = value.get("provider_order", ["deterministic"])
        if not isinstance(provider_order, list) or not all(
            isinstance(provider, str) for provider in provider_order
        ):
            raise ValueError("provider_order must be a list of strings")
        return cls(
            max_attempts=min(20, _positive_int(value.get("max_attempts", 3), "max_attempts")),
            attempt_timeout_seconds=_positive_float(
                value.get("attempt_timeout_seconds", 60), "attempt_timeout_seconds"
            ),
            initial_backoff_seconds=_positive_float(
                value.get("initial_backoff_seconds", 1), "initial_backoff_seconds"
            ),
            max_backoff_seconds=_positive_float(
                value.get("max_backoff_seconds", 60), "max_backoff_seconds"
            ),
            provider_order=tuple(provider_order),
        )


def build_policy_snapshot(
    requested: Mapping[str, Any],
    *,
    max_attempts: int,
    attempt_timeout_seconds: float,
    initial_backoff_seconds: float,
    max_backoff_seconds: float,
    available_providers: set[str] | None = None,
    max_output_tokens: int = 2048,
) -> dict[str, Any]:
    """Merge request policy with safe defaults before it is included in the idempotency hash."""

    snapshot = RunPolicyRequest.model_validate(dict(requested)).model_dump(exclude_none=True)
    if snapshot.get("max_backoff_seconds", max_backoff_seconds) < snapshot.get(
        "initial_backoff_seconds", initial_backoff_seconds
    ):
        raise ValueError("max_backoff_seconds must be at least initial_backoff_seconds")
    snapshot.setdefault("max_attempts", max_attempts)
    snapshot.setdefault("attempt_timeout_seconds", attempt_timeout_seconds)
    snapshot.setdefault("initial_backoff_seconds", initial_backoff_seconds)
    snapshot.setdefault("max_backoff_seconds", max_backoff_seconds)
    snapshot["max_attempts"] = min(snapshot["max_attempts"], max_attempts)
    snapshot["attempt_timeout_seconds"] = min(
        snapshot["attempt_timeout_seconds"], attempt_timeout_seconds
    )
    snapshot["initial_backoff_seconds"] = min(
        max_backoff_seconds, max(1, initial_backoff_seconds, snapshot["initial_backoff_seconds"])
    )
    snapshot["max_backoff_seconds"] = max(
        snapshot["initial_backoff_seconds"],
        min(snapshot["max_backoff_seconds"], max_backoff_seconds),
    )
    snapshot["max_output_tokens"] = min(
        snapshot.get("max_output_tokens", max_output_tokens), max_output_tokens
    )
    routing_decision = resolve_routing_decision(snapshot, available_providers=available_providers)
    snapshot["provider_order"] = routing_decision["provider_order"]
    snapshot["routing"] = {
        "strategy": routing_decision["strategy"],
        "candidates": list(routing_decision["provider_order"]),
        "decision": routing_decision,
    }
    policy = RetryPolicy.from_snapshot(snapshot)
    return snapshot | policy.as_snapshot()


def is_retryable_error(error_code: str) -> bool:
    try:
        return ExecutionErrorCode(error_code) in RETRYABLE_ERROR_CODES
    except ValueError:
        return False


def classify_exception(exc: Exception) -> ExecutionErrorCode:
    """Map provider-facing failures to stable, policy-safe error codes."""

    from agent_runtime.application.runs import QuotaExceededError

    if isinstance(exc, QuotaExceededError):
        return ExecutionErrorCode.RESOURCE_QUOTA_EXCEEDED
    if isinstance(exc, TimeoutError):
        return ExecutionErrorCode.PROVIDER_TIMEOUT
    status_code = getattr(exc, "status_code", None)
    if status_code == 429:
        return ExecutionErrorCode.PROVIDER_RATE_LIMITED
    if status_code in {401, 403}:
        return ExecutionErrorCode.PROVIDER_AUTH_FAILED
    if status_code is not None and 500 <= status_code <= 599:
        return ExecutionErrorCode.PROVIDER_SERVER_ERROR
    if status_code is not None and 400 <= status_code <= 499:
        return ExecutionErrorCode.PROVIDER_BAD_REQUEST
    return ExecutionErrorCode.EXECUTION_ERROR


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError(f"{name} must be a positive integer")
    return int(value)


def _positive_float(value: Any, name: str) -> float:
    if (
        isinstance(value, bool)
        or not isinstance(value, (int, float))
        or not math.isfinite(value)
        or value <= 0
    ):
        raise ValueError(f"{name} must be a positive number")
    return float(value)
