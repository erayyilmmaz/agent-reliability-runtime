import pytest

from agent_runtime.domain.states import (
    ALLOWED_EXECUTION_TRANSITIONS,
    ExecutionStatus,
    InvalidExecutionTransition,
    ensure_execution_transition,
    is_terminal_execution_status,
)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (current, target)
        for current, targets in ALLOWED_EXECUTION_TRANSITIONS.items()
        for target in targets
    ],
)
def test_all_documented_transitions_are_allowed(
    current: ExecutionStatus, target: ExecutionStatus
) -> None:
    ensure_execution_transition(current, target)


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (current, target)
        for current in ExecutionStatus
        for target in ExecutionStatus
        if target not in ALLOWED_EXECUTION_TRANSITIONS[current]
    ],
)
def test_every_undocumented_transition_is_rejected(
    current: ExecutionStatus, target: ExecutionStatus
) -> None:
    with pytest.raises(InvalidExecutionTransition):
        ensure_execution_transition(current, target)


@pytest.mark.parametrize(
    "status",
    [ExecutionStatus.SUCCEEDED, ExecutionStatus.FAILED, ExecutionStatus.DEAD_LETTERED],
)
def test_terminal_states_are_terminal(status: ExecutionStatus) -> None:
    assert is_terminal_execution_status(status)
