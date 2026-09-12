from __future__ import annotations

import time
from collections.abc import Callable, Mapping
from copy import deepcopy
from functools import wraps
from typing import Any, TypeVar, cast
from uuid import UUID, uuid4

from sqlalchemy import select, tuple_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_runtime.application.runs import (
    AttemptSnapshot,
    EvaluationSnapshot,
    EventSnapshot,
    IdempotencyConflictError,
    InvalidCursorError,
    RunNotFoundError,
    RunNotSucceededError,
    RunSnapshot,
    canonical_request_hash,
)
from agent_runtime.domain.states import EvaluationStatus, ExecutionStatus
from agent_runtime.evaluation.safety import validate_rules
from agent_runtime.infrastructure.database.models import (
    Evaluation,
    OutboxEvent,
    Run,
    RunAttempt,
    RunEvent,
)
from agent_runtime.infrastructure.database.quotas import QuotaLimits, check_admission, lock_identity
from agent_runtime.observability.metrics import get_runtime_metrics
from agent_runtime.observability.telemetry import inject_trace_context, safe_span


def _routing_decision(policy_snapshot: Mapping[str, Any]) -> dict[str, Any] | None:
    routing = policy_snapshot.get("routing")
    if not isinstance(routing, Mapping):
        return None
    decision = routing.get("decision")
    return dict(decision) if isinstance(decision, Mapping) else None


F = TypeVar("F", bound=Callable[..., Any])


def _timed(operation: str) -> Callable[[F], F]:
    """Record how long a named persistence operation takes (PERF-006).

    Applied at the service boundary rather than inside SQLAlchemy so the label
    is a stable operation name. Instrumenting per-statement would tie metric
    cardinality to the query text.
    """

    def decorate(func: F) -> F:
        @wraps(func)
        async def wrapper(*args: Any, **kwargs: Any) -> Any:
            started = time.perf_counter()
            try:
                return await func(*args, **kwargs)
            finally:
                get_runtime_metrics().db_operation(operation, time.perf_counter() - started)

        return cast(F, wrapper)

    return decorate


class SqlAlchemyRunService:
    """Transactional run submission backed by PostgreSQL and the outbox table."""

    def __init__(
        self,
        session_factory: async_sessionmaker[AsyncSession],
        *,
        quotas: QuotaLimits | None = None,
        job_timeout_seconds: float = 60,
    ) -> None:
        self._session_factory = session_factory
        self._quotas = quotas or QuotaLimits()
        self._job_timeout_seconds = job_timeout_seconds

    @_timed("submit")
    async def submit(
        self,
        *,
        client_id: str,
        idempotency_key: str,
        input_payload: dict[str, Any],
        policy_snapshot: dict[str, Any],
        principal_id: str | None = None,
        work_kind: str = "execution",
    ) -> tuple[RunSnapshot, bool]:
        principal_id = principal_id or client_id
        request_hash = canonical_request_hash(
            input_payload,
            policy_snapshot
            if work_kind == "execution"
            else {**policy_snapshot, "work_kind": work_kind},
        )

        with safe_span("arr.db.run.submit") as span:
            async with self._session_factory() as session:
                try:
                    async with session.begin():
                        await lock_identity(session, principal_id, client_id)
                        existing = await self._find_by_idempotency_key(
                            session, client_id=client_id, idempotency_key=idempotency_key
                        )
                        if existing is not None:
                            span.set_attribute("arr.run_id", str(existing.id))
                            span.set_attribute("arr.idempotency_replay", True)
                            return self._to_run_snapshot(existing), self._match_or_raise(
                                existing, request_hash
                            )

                        await check_admission(session, principal_id, client_id, self._quotas)
                        trace_context = inject_trace_context()
                        run = Run(
                            principal_id=principal_id,
                            work_kind=work_kind,
                            client_id=client_id,
                            idempotency_key=idempotency_key,
                            request_hash=request_hash,
                            input_payload=input_payload,
                            policy_snapshot=policy_snapshot,
                            trace_context=trace_context,
                            execution_status=ExecutionStatus.QUEUED,
                            evaluation_status=EvaluationStatus.NOT_RUN,
                        )
                        session.add(run)
                        await session.flush()
                        span.set_attribute("arr.run_id", str(run.id))
                        session.add(
                            OutboxEvent(
                                aggregate_id=run.id,
                                event_type="RUN_QUEUED",
                                payload={"trace_context": trace_context},
                            )
                        )
                        session.add(
                            RunEvent(
                                run_id=run.id,
                                event_type="RUN_QUEUED",
                                metadata_={"source": "api"},
                            )
                        )
                        routing = policy_snapshot.get("routing")
                        if isinstance(routing, dict) and isinstance(routing.get("decision"), dict):
                            decision = routing["decision"]
                            session.add(
                                RunEvent(
                                    run_id=run.id,
                                    event_type="ROUTING_DECISION_RECORDED",
                                    metadata_={
                                        "strategy": decision.get("strategy"),
                                        "selected_provider": decision.get("selected_provider"),
                                        "reason": decision.get("reason"),
                                        "metrics_source": decision.get("metrics_source"),
                                    },
                                )
                            )
                        await session.flush()
                        provider = policy_snapshot.get("provider_order", ["deterministic"])[0]
                        get_runtime_metrics().run_submitted(
                            provider if isinstance(provider, str) else "other"
                        )
                        return self._to_run_snapshot(run), False
                except IntegrityError:
                    # A competing request inserted the same client/key first. The
                    # unique constraint is the final concurrency authority.
                    existing = await self._get_idempotent_run_after_race(
                        session, client_id=client_id, idempotency_key=idempotency_key
                    )
                    span.set_attribute("arr.run_id", str(existing.id))
                    span.set_attribute("arr.idempotency_replay", True)
                    return self._to_run_snapshot(existing), self._match_or_raise(
                        existing, request_hash
                    )

    @_timed("get_run")
    async def get_run(self, *, client_id: str, run_id: UUID) -> RunSnapshot:
        async with self._session_factory() as session:
            run = await self._get_run_for_client(session, client_id=client_id, run_id=run_id)
            return self._to_run_snapshot(run)

    @_timed("get_attempts")
    async def get_attempts(
        self, *, client_id: str, run_id: UUID, limit: int = 50, cursor: UUID | None = None
    ) -> list[AttemptSnapshot]:
        async with self._session_factory() as session:
            await self._get_run_for_client(session, client_id=client_id, run_id=run_id)
            statement = select(RunAttempt).where(RunAttempt.run_id == run_id)
            if cursor is not None:
                anchor = await session.scalar(
                    select(RunAttempt).where(RunAttempt.id == cursor, RunAttempt.run_id == run_id)
                )
                if anchor is None:
                    raise InvalidCursorError("Cursor does not belong to this history")
                statement = statement.where(
                    tuple_(RunAttempt.started_at, RunAttempt.id) > (anchor.started_at, anchor.id)
                )
            rows = await session.scalars(
                statement.order_by(RunAttempt.started_at, RunAttempt.id).limit(
                    max(1, min(limit, 100))
                )
            )
            return [self._to_attempt_snapshot(row) for row in rows]

    @_timed("get_events")
    async def get_events(
        self, *, client_id: str, run_id: UUID, limit: int = 50, cursor: UUID | None = None
    ) -> list[EventSnapshot]:
        async with self._session_factory() as session:
            await self._get_run_for_client(session, client_id=client_id, run_id=run_id)
            statement = select(RunEvent).where(RunEvent.run_id == run_id)
            if cursor is not None:
                anchor = await session.scalar(
                    select(RunEvent).where(RunEvent.id == cursor, RunEvent.run_id == run_id)
                )
                if anchor is None:
                    raise InvalidCursorError("Cursor does not belong to this history")
                statement = statement.where(
                    tuple_(RunEvent.created_at, RunEvent.id) > (anchor.created_at, anchor.id)
                )
            rows = await session.scalars(
                statement.order_by(RunEvent.created_at, RunEvent.id).limit(max(1, min(limit, 100)))
            )
            return [self._to_event_snapshot(row) for row in rows]

    @_timed("evaluate")
    async def evaluate(
        self,
        *,
        client_id: str,
        run_id: UUID,
        rules: list[dict[str, Any]],
        principal_id: str | None = None,
    ) -> EvaluationSnapshot:
        """Accept evaluation as a durable worker job in the same outbox transaction."""
        validate_rules(rules)
        principal_id = principal_id or client_id
        async with self._session_factory() as session:
            async with session.begin():
                await lock_identity(session, principal_id, client_id)
                run = await self._get_run_for_client(session, client_id=client_id, run_id=run_id)
                await session.refresh(run, with_for_update=True)
                if (
                    run.work_kind != "execution"
                    or run.execution_status != ExecutionStatus.SUCCEEDED
                    or run.result_payload is None
                ):
                    raise RunNotSucceededError("Only a successful execution run can be evaluated")
                await check_admission(session, principal_id, client_id, self._quotas)
                trace_context = inject_trace_context()
                job = Run(
                    client_id=client_id,
                    principal_id=principal_id,
                    work_kind="evaluation",
                    idempotency_key=f"evaluation-{uuid4()}",
                    request_hash=canonical_request_hash({"source": str(run_id)}, {"rules": rules}),
                    input_payload={"source_run_id": str(run_id), "rules": rules},
                    policy_snapshot=self.job_policy(),
                    trace_context=trace_context,
                    execution_status=ExecutionStatus.QUEUED,
                    evaluation_status=EvaluationStatus.NOT_RUN,
                )
                session.add(job)
                await session.flush()
                evaluation = Evaluation(
                    run_id=run.id,
                    job_run_id=job.id,
                    evaluator="deterministic_rules",
                    status=EvaluationStatus.PENDING,
                    details={"rule_count": len(rules), "job_run_id": str(job.id)},
                )
                run.evaluation_status = EvaluationStatus.PENDING
                session.add_all(
                    [
                        evaluation,
                        OutboxEvent(
                            aggregate_id=job.id,
                            event_type="RUN_QUEUED",
                            payload={"trace_context": trace_context},
                        ),
                        RunEvent(
                            run_id=job.id,
                            event_type="RUN_QUEUED",
                            metadata_={"work_kind": "evaluation"},
                        ),
                        RunEvent(
                            run_id=run.id,
                            event_type="EVALUATION_PENDING",
                            metadata_={"job_run_id": str(job.id)},
                        ),
                    ]
                )
                await session.flush()
                return self._to_evaluation_snapshot(evaluation)

    def job_policy(self) -> dict[str, Any]:
        return {
            "max_attempts": 1,
            "attempt_timeout_seconds": self._job_timeout_seconds,
            "initial_backoff_seconds": 1,
            "max_backoff_seconds": 1,
            "provider_order": ["deterministic"],
        }

    async def submit_regression(
        self,
        *,
        client_id: str,
        principal_id: str,
        payload: dict[str, Any],
        idempotency_key: str,
    ) -> tuple[RunSnapshot, bool]:
        return await self.submit(
            client_id=client_id,
            principal_id=principal_id,
            input_payload=payload,
            policy_snapshot=self.job_policy(),
            idempotency_key=idempotency_key,
            work_kind="regression",
        )

    @_timed("get_evaluations")
    async def get_evaluations(
        self, *, client_id: str, run_id: UUID, limit: int = 50, cursor: UUID | None = None
    ) -> list[EvaluationSnapshot]:
        async with self._session_factory() as session:
            await self._get_run_for_client(session, client_id=client_id, run_id=run_id)
            statement = select(Evaluation).where(Evaluation.run_id == run_id)
            if cursor is not None:
                anchor = await session.scalar(
                    select(Evaluation).where(Evaluation.id == cursor, Evaluation.run_id == run_id)
                )
                if anchor is None:
                    raise InvalidCursorError("Cursor does not belong to this history")
                statement = statement.where(
                    tuple_(Evaluation.created_at, Evaluation.id) > (anchor.created_at, anchor.id)
                )
            rows = await session.scalars(
                statement.order_by(Evaluation.created_at, Evaluation.id).limit(
                    max(1, min(limit, 100))
                )
            )
            return [self._to_evaluation_snapshot(row) for row in rows]

    @_timed("replay")
    async def replay(
        self, *, client_id: str, run_id: UUID, idempotency_key: str, principal_id: str | None = None
    ) -> tuple[RunSnapshot, bool]:
        """Create a distinct durable run from an immutable source snapshot."""

        principal_id = principal_id or client_id
        with safe_span("arr.db.run.replay") as span:
            async with self._session_factory() as session:
                try:
                    async with session.begin():
                        await lock_identity(session, principal_id, client_id)
                        source = await self._get_run_for_client(
                            session, client_id=client_id, run_id=run_id
                        )
                        if source.work_kind != "execution":
                            raise RunNotFoundError("Only execution runs can be replayed")
                        request_hash = canonical_request_hash(
                            {"replay_of_run_id": str(source.id), "input": source.input_payload},
                            source.policy_snapshot,
                        )
                        existing = await self._find_by_idempotency_key(
                            session, client_id=client_id, idempotency_key=idempotency_key
                        )
                        if existing is not None:
                            return self._to_run_snapshot(existing), self._match_replay_or_raise(
                                existing, request_hash=request_hash, source_run_id=source.id
                            )

                        await check_admission(session, principal_id, client_id, self._quotas)
                        trace_context = inject_trace_context()
                        replay = Run(
                            principal_id=principal_id,
                            client_id=client_id,
                            idempotency_key=idempotency_key,
                            request_hash=request_hash,
                            input_payload=deepcopy(source.input_payload),
                            policy_snapshot=deepcopy(source.policy_snapshot),
                            trace_context=trace_context,
                            replay_of_run_id=source.id,
                            execution_status=ExecutionStatus.QUEUED,
                            evaluation_status=EvaluationStatus.NOT_RUN,
                        )
                        session.add(replay)
                        await session.flush()
                        span.set_attribute("arr.run_id", str(replay.id))
                        span.set_attribute("arr.replay_of_run_id", str(source.id))
                        session.add(
                            OutboxEvent(
                                aggregate_id=replay.id,
                                event_type="RUN_QUEUED",
                                payload={"trace_context": trace_context, "reason": "replay"},
                            )
                        )
                        session.add_all(
                            [
                                RunEvent(
                                    run_id=source.id,
                                    event_type="REPLAY_CREATED",
                                    metadata_={"replay_run_id": str(replay.id)},
                                ),
                                RunEvent(
                                    run_id=replay.id,
                                    event_type="RUN_REPLAY_QUEUED",
                                    metadata_={"replay_of_run_id": str(source.id)},
                                ),
                            ]
                        )
                        return self._to_run_snapshot(replay), False
                except IntegrityError:
                    existing = await self._get_idempotent_run_after_race(
                        session, client_id=client_id, idempotency_key=idempotency_key
                    )
                    source = await self._get_run_for_client(
                        session, client_id=client_id, run_id=run_id
                    )
                    request_hash = canonical_request_hash(
                        {"replay_of_run_id": str(source.id), "input": source.input_payload},
                        source.policy_snapshot,
                    )
                    return self._to_run_snapshot(existing), self._match_replay_or_raise(
                        existing, request_hash=request_hash, source_run_id=source.id
                    )

    @staticmethod
    async def _find_by_idempotency_key(
        session: AsyncSession, *, client_id: str, idempotency_key: str
    ) -> Run | None:
        return cast(
            Run | None,
            await session.scalar(
                select(Run).where(
                    Run.client_id == client_id, Run.idempotency_key == idempotency_key
                )
            ),
        )

    async def _get_idempotent_run_after_race(
        self, session: AsyncSession, *, client_id: str, idempotency_key: str
    ) -> Run:
        existing = await self._find_by_idempotency_key(
            session, client_id=client_id, idempotency_key=idempotency_key
        )
        if existing is None:
            raise RuntimeError("Idempotency race completed without a persisted run")
        return existing

    @staticmethod
    async def _get_run_for_client(session: AsyncSession, *, client_id: str, run_id: UUID) -> Run:
        run = cast(
            Run | None,
            await session.scalar(select(Run).where(Run.id == run_id, Run.client_id == client_id)),
        )
        if run is None:
            raise RunNotFoundError(f"Run {run_id} was not found")
        return run

    @staticmethod
    def _match_or_raise(run: Run, request_hash: str) -> bool:
        if run.request_hash != request_hash:
            raise IdempotencyConflictError(
                "Idempotency-Key is already bound to a different request"
            )
        return True

    @staticmethod
    def _match_replay_or_raise(run: Run, *, request_hash: str, source_run_id: UUID) -> bool:
        if run.request_hash != request_hash or run.replay_of_run_id != source_run_id:
            raise IdempotencyConflictError(
                "Idempotency-Key is already bound to a different replay request"
            )
        return True

    @staticmethod
    def _to_run_snapshot(run: Run) -> RunSnapshot:
        if run.created_at is None:
            raise RuntimeError("Persisted run must have a creation timestamp")
        return RunSnapshot(
            id=run.id,
            execution_status=run.execution_status,
            evaluation_status=run.evaluation_status,
            created_at=run.created_at,
            started_at=run.started_at,
            completed_at=run.completed_at,
            replay_of_run_id=run.replay_of_run_id,
            error_code=run.error_code,
            routing_decision=_routing_decision(run.policy_snapshot),
            work_kind=run.work_kind,
            result_payload=run.result_payload,
        )

    @staticmethod
    def _to_attempt_snapshot(attempt: RunAttempt) -> AttemptSnapshot:
        if attempt.started_at is None:
            raise RuntimeError("Persisted attempt must have a start timestamp")
        return AttemptSnapshot(
            id=attempt.id,
            attempt_number=attempt.attempt_number,
            provider=attempt.provider,
            started_at=attempt.started_at,
            finished_at=attempt.finished_at,
            outcome=attempt.outcome,
            error_code=attempt.error_code,
            retryable=attempt.retryable,
            latency_ms=attempt.latency_ms,
        )

    @staticmethod
    def _to_event_snapshot(event: RunEvent) -> EventSnapshot:
        if event.created_at is None:
            raise RuntimeError("Persisted event must have a creation timestamp")
        return EventSnapshot(
            id=event.id,
            attempt_id=event.attempt_id,
            event_type=event.event_type,
            metadata=event.metadata_,
            created_at=event.created_at,
        )

    @staticmethod
    def _to_evaluation_snapshot(evaluation: Evaluation) -> EvaluationSnapshot:
        if evaluation.created_at is None:
            raise RuntimeError("Persisted evaluation must have a creation timestamp")
        return EvaluationSnapshot(
            id=evaluation.id,
            evaluator=evaluation.evaluator,
            status=evaluation.status,
            score=evaluation.score,
            result=evaluation.result,
            details=evaluation.details,
            created_at=evaluation.created_at,
            completed_at=evaluation.completed_at,
        )
