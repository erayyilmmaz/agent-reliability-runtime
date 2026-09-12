from __future__ import annotations

import pytest

from agent_runtime.domain.retry import (
    ExecutionErrorCode,
    RetryPolicy,
    build_policy_snapshot,
    classify_exception,
    is_retryable_error,
)


@pytest.mark.parametrize(
    "error_code",
    [
        ExecutionErrorCode.PROVIDER_TIMEOUT,
        ExecutionErrorCode.PROVIDER_RATE_LIMITED,
        ExecutionErrorCode.PROVIDER_SERVER_ERROR,
        ExecutionErrorCode.EXECUTION_ERROR,
    ],
)
def test_transient_errors_are_retryable(error_code: ExecutionErrorCode) -> None:
    assert is_retryable_error(error_code)


@pytest.mark.parametrize(
    "error_code",
    [ExecutionErrorCode.PROVIDER_AUTH_FAILED, ExecutionErrorCode.PROVIDER_BAD_REQUEST],
)
def test_client_errors_are_not_retryable(error_code: ExecutionErrorCode) -> None:
    assert not is_retryable_error(error_code)


def test_backoff_is_exponential_and_bounded() -> None:
    policy = RetryPolicy(
        max_attempts=5,
        attempt_timeout_seconds=10,
        initial_backoff_seconds=2,
        max_backoff_seconds=5,
        provider_order=("deterministic",),
    )

    assert [policy.delay_after_attempt(number) for number in range(1, 5)] == [2, 4, 5, 5]


def test_policy_snapshot_contains_resolved_defaults() -> None:
    snapshot = build_policy_snapshot(
        {"max_attempts": 4},
        max_attempts=3,
        attempt_timeout_seconds=30,
        initial_backoff_seconds=2,
        max_backoff_seconds=60,
    )

    assert snapshot["max_attempts"] == 3
    assert snapshot["attempt_timeout_seconds"] == 30.0
    assert snapshot["initial_backoff_seconds"] == 2.0
    assert snapshot["max_backoff_seconds"] == 60.0
    assert snapshot["provider_order"] == ["deterministic"]
    assert snapshot["routing"]["decision"]["selected_provider"] == "deterministic"


def test_invalid_policy_is_rejected() -> None:
    with pytest.raises(ValueError, match="max_backoff_seconds"):
        RetryPolicy(
            max_attempts=3,
            attempt_timeout_seconds=10,
            initial_backoff_seconds=4,
            max_backoff_seconds=2,
            provider_order=("deterministic",),
        )


class _ProviderError(Exception):
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code


@pytest.mark.parametrize(
    ("exc", "expected"),
    [
        (TimeoutError(), ExecutionErrorCode.PROVIDER_TIMEOUT),
        (_ProviderError(429), ExecutionErrorCode.PROVIDER_RATE_LIMITED),
        (_ProviderError(500), ExecutionErrorCode.PROVIDER_SERVER_ERROR),
        (_ProviderError(401), ExecutionErrorCode.PROVIDER_AUTH_FAILED),
    ],
)
def test_exception_classification(exc: Exception, expected: ExecutionErrorCode) -> None:
    assert classify_exception(exc) == expected
