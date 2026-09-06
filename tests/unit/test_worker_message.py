from __future__ import annotations

import asyncio
import uuid

import pytest

from agent_runtime.application.execution import ClaimDecision, ClaimResult
from agent_runtime.domain.retry import ExecutionErrorCode
from agent_runtime.infrastructure.messaging.worker import RabbitMqWorker


def test_worker_extracts_run_id_from_minimal_message() -> None:
    assert (
        str(
            RabbitMqWorker.run_id_from_message(
                b'{"event_id":"unused","run_id":"00000000-0000-0000-0000-000000000001"}'
            )
        )
        == "00000000-0000-0000-0000-000000000001"
    )


@pytest.mark.parametrize("body", [b"not-json", b"{}", b'{"run_id":"invalid"}'])
def test_worker_rejects_malformed_message(body: bytes) -> None:
    with pytest.raises(ValueError):
        RabbitMqWorker.run_id_from_message(body)


class _SleepingExecutor:
    async def execute(self, **_: object) -> object:
        await asyncio.sleep(0.02)
        raise AssertionError("wait_for should time out first")


class _FailurePersistence:
    def __init__(self) -> None:
        self.error_code: str | None = None

    async def complete_failure(self, **kwargs: object) -> bool:
        self.error_code = str(kwargs["error_code"])
        return True


@pytest.mark.asyncio
async def test_worker_applies_attempt_timeout_and_records_retryable_code() -> None:
    persistence = _FailurePersistence()
    worker = RabbitMqWorker(
        url="amqp://unused/",
        worker_id="test-worker",
        prefetch_count=1,
        execution_service=persistence,  # type: ignore[arg-type]
        executor=_SleepingExecutor(),  # type: ignore[arg-type]
    )
    claim = ClaimResult(
        decision=ClaimDecision.CLAIMED,
        run_id=uuid.uuid4(),
        attempt_id=uuid.uuid4(),
        input_payload={"job": "timeout"},
        policy_snapshot={
            "max_attempts": 3,
            "attempt_timeout_seconds": 0.001,
            "initial_backoff_seconds": 1,
            "max_backoff_seconds": 5,
            "provider_order": ["deterministic"],
        },
    )

    assert await worker._execute_claim(claim)
    assert persistence.error_code == ExecutionErrorCode.PROVIDER_TIMEOUT
