from __future__ import annotations

from enum import StrEnum


class ExecutionStatus(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    RETRY_SCHEDULED = "RETRY_SCHEDULED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    DEAD_LETTERED = "DEAD_LETTERED"


class EvaluationStatus(StrEnum):
    NOT_RUN = "NOT_RUN"
    PENDING = "PENDING"
    PASSED = "PASSED"
    FAILED = "FAILED"
    ERROR = "ERROR"


TERMINAL_EXECUTION_STATUSES = frozenset(
    {
        ExecutionStatus.SUCCEEDED,
        ExecutionStatus.FAILED,
        ExecutionStatus.DEAD_LETTERED,
    }
)

ALLOWED_EXECUTION_TRANSITIONS: dict[ExecutionStatus, frozenset[ExecutionStatus]] = {
    ExecutionStatus.QUEUED: frozenset({ExecutionStatus.RUNNING, ExecutionStatus.FAILED}),
    ExecutionStatus.RUNNING: frozenset(
        {
            ExecutionStatus.SUCCEEDED,
            ExecutionStatus.FAILED,
            ExecutionStatus.RETRY_SCHEDULED,
            ExecutionStatus.DEAD_LETTERED,
        }
    ),
    ExecutionStatus.RETRY_SCHEDULED: frozenset(
        {ExecutionStatus.QUEUED, ExecutionStatus.FAILED, ExecutionStatus.DEAD_LETTERED}
    ),
    ExecutionStatus.SUCCEEDED: frozenset(),
    ExecutionStatus.FAILED: frozenset(),
    ExecutionStatus.DEAD_LETTERED: frozenset(),
}


class InvalidExecutionTransition(ValueError):
    """Raised when a run is asked to move outside its lifecycle contract."""


def ensure_execution_transition(current: ExecutionStatus, target: ExecutionStatus) -> None:
    """Reject a transition not explicitly allowed by the durable run contract."""

    if target not in ALLOWED_EXECUTION_TRANSITIONS[current]:
        raise InvalidExecutionTransition(f"Cannot transition execution from {current} to {target}")


def is_terminal_execution_status(status: ExecutionStatus) -> bool:
    return status in TERMINAL_EXECUTION_STATUSES
