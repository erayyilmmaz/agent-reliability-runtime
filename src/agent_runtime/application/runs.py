from __future__ import annotations

import hashlib
import json
import uuid
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Protocol

from agent_runtime.domain.states import EvaluationStatus, ExecutionStatus


def canonical_request_hash(input_payload: dict[str, Any], policy_snapshot: dict[str, Any]) -> str:
    """Hash the semantic create-run payload, independent of JSON key ordering."""

    canonical_payload = json.dumps(
        {"input": input_payload, "policy": policy_snapshot},
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(canonical_payload).hexdigest()


@dataclass(frozen=True)
class RunSnapshot:
    id: uuid.UUID
    execution_status: ExecutionStatus
    evaluation_status: EvaluationStatus
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    replay_of_run_id: uuid.UUID | None
    error_code: str | None
    routing_decision: dict[str, Any] | None = None


@dataclass(frozen=True)
class AttemptSnapshot:
    id: uuid.UUID
    attempt_number: int
    provider: str
    started_at: datetime
    finished_at: datetime | None
    outcome: str | None
    error_code: str | None
    retryable: bool | None
    latency_ms: int | None


@dataclass(frozen=True)
class EventSnapshot:
    id: uuid.UUID
    attempt_id: uuid.UUID | None
    event_type: str
    metadata: dict[str, Any] | None
    created_at: datetime


@dataclass(frozen=True)
class EvaluationSnapshot:
    id: uuid.UUID
    evaluator: str
    status: EvaluationStatus
    score: float | None
    result: dict[str, Any] | None
    details: dict[str, Any] | None
    created_at: datetime
    completed_at: datetime | None


class RunNotFoundError(LookupError):
    """A requested run does not exist in the caller's visible scope."""


class IdempotencyConflictError(ValueError):
    """The same key was reused for a semantically different request."""


class RunNotSucceededError(ValueError):
    """Evaluations can only inspect a durable successful provider result."""


class RunSubmissionService(Protocol):
    async def submit(
        self,
        *,
        client_id: str,
        idempotency_key: str,
        input_payload: dict[str, Any],
        policy_snapshot: dict[str, Any],
    ) -> tuple[RunSnapshot, bool]: ...

    async def get_run(self, *, client_id: str, run_id: uuid.UUID) -> RunSnapshot: ...

    async def get_attempts(self, *, client_id: str, run_id: uuid.UUID) -> list[AttemptSnapshot]: ...

    async def get_events(self, *, client_id: str, run_id: uuid.UUID) -> list[EventSnapshot]: ...

    async def evaluate(
        self, *, client_id: str, run_id: uuid.UUID, rules: list[dict[str, Any]]
    ) -> EvaluationSnapshot: ...

    async def get_evaluations(
        self, *, client_id: str, run_id: uuid.UUID
    ) -> list[EvaluationSnapshot]: ...

    async def replay(
        self, *, client_id: str, run_id: uuid.UUID, idempotency_key: str
    ) -> tuple[RunSnapshot, bool]: ...
