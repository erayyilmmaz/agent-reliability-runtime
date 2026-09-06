from __future__ import annotations

from typing import Any, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_runtime.application.runs import (
    AttemptSnapshot,
    EventSnapshot,
    IdempotencyConflictError,
    RunNotFoundError,
    RunSnapshot,
    canonical_request_hash,
)
from agent_runtime.domain.states import EvaluationStatus, ExecutionStatus
from agent_runtime.infrastructure.database.models import OutboxEvent, Run, RunAttempt, RunEvent
from agent_runtime.observability.metrics import get_runtime_metrics
from agent_runtime.observability.telemetry import get_tracer, inject_trace_context


class SqlAlchemyRunService:
    """Transactional run submission backed by PostgreSQL and the outbox table."""

    def __init__(self, session_factory: async_sessionmaker[AsyncSession]) -> None:
        self._session_factory = session_factory

    async def submit(
        self,
        *,
        client_id: str,
        idempotency_key: str,
        input_payload: dict[str, Any],
        policy_snapshot: dict[str, Any],
    ) -> tuple[RunSnapshot, bool]:
        request_hash = canonical_request_hash(input_payload, policy_snapshot)

        with get_tracer().start_as_current_span("arr.db.run.submit") as span:
            async with self._session_factory() as session:
                try:
                    async with session.begin():
                        existing = await self._find_by_idempotency_key(
                            session, client_id=client_id, idempotency_key=idempotency_key
                        )
                        if existing is not None:
                            span.set_attribute("arr.run_id", str(existing.id))
                            span.set_attribute("arr.idempotency_replay", True)
                            return self._to_run_snapshot(existing), self._match_or_raise(
                                existing, request_hash
                            )

                        trace_context = inject_trace_context()
                        run = Run(
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

    async def get_run(self, *, client_id: str, run_id: UUID) -> RunSnapshot:
        async with self._session_factory() as session:
            run = await self._get_run_for_client(session, client_id=client_id, run_id=run_id)
            return self._to_run_snapshot(run)

    async def get_attempts(self, *, client_id: str, run_id: UUID) -> list[AttemptSnapshot]:
        async with self._session_factory() as session:
            await self._get_run_for_client(session, client_id=client_id, run_id=run_id)
            attempts = await session.scalars(
                select(RunAttempt)
                .where(RunAttempt.run_id == run_id)
                .order_by(RunAttempt.attempt_number.asc())
            )
            return [self._to_attempt_snapshot(attempt) for attempt in attempts]

    async def get_events(self, *, client_id: str, run_id: UUID) -> list[EventSnapshot]:
        async with self._session_factory() as session:
            await self._get_run_for_client(session, client_id=client_id, run_id=run_id)
            events = await session.scalars(
                select(RunEvent)
                .where(RunEvent.run_id == run_id)
                .order_by(RunEvent.created_at.asc())
            )
            return [self._to_event_snapshot(event) for event in events]

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
