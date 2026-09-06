from __future__ import annotations

from collections.abc import Mapping
from copy import deepcopy
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_runtime.application.runs import (
    AttemptSnapshot,
    EvaluationSnapshot,
    EventSnapshot,
    IdempotencyConflictError,
    RunNotFoundError,
    RunNotSucceededError,
    RunSnapshot,
    canonical_request_hash,
)
from agent_runtime.domain.states import EvaluationStatus, ExecutionStatus
from agent_runtime.evaluation import EvaluationConfigurationError, evaluate_rules
from agent_runtime.infrastructure.database.models import (
    Evaluation,
    OutboxEvent,
    Run,
    RunAttempt,
    RunEvent,
)
from agent_runtime.observability.metrics import get_runtime_metrics
from agent_runtime.observability.telemetry import get_tracer, inject_trace_context


def _routing_decision(policy_snapshot: Mapping[str, Any]) -> dict[str, Any] | None:
    routing = policy_snapshot.get("routing")
    if not isinstance(routing, Mapping):
        return None
    decision = routing.get("decision")
    return dict(decision) if isinstance(decision, Mapping) else None


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

    async def evaluate(
        self, *, client_id: str, run_id: UUID, rules: list[dict[str, Any]]
    ) -> EvaluationSnapshot:
        """Persist evaluation lifecycle separately from the completed execution."""

        async with self._session_factory() as session:
            async with session.begin():
                run = await self._get_run_for_client(session, client_id=client_id, run_id=run_id)
                if run.execution_status != ExecutionStatus.SUCCEEDED or run.result_payload is None:
                    raise RunNotSucceededError(
                        "Only a SUCCEEDED run with a persisted result can be evaluated"
                    )
                successful_attempt = cast(
                    RunAttempt | None,
                    await session.scalar(
                        select(RunAttempt)
                        .where(RunAttempt.run_id == run.id, RunAttempt.outcome == "SUCCEEDED")
                        .order_by(RunAttempt.attempt_number.desc())
                        .limit(1)
                    ),
                )
                evaluation = Evaluation(
                    run_id=run.id,
                    evaluator="deterministic_rules",
                    status=EvaluationStatus.PENDING,
                    details={"rule_count": len(rules)},
                )
                run.evaluation_status = EvaluationStatus.PENDING
                session.add(evaluation)
                session.add(
                    RunEvent(
                        run_id=run.id,
                        attempt_id=(
                            successful_attempt.id if successful_attempt is not None else None
                        ),
                        event_type="EVALUATION_PENDING",
                        metadata_={"evaluator": evaluation.evaluator, "rule_count": len(rules)},
                    )
                )
                await session.flush()
                evaluation_id = evaluation.id
                result_payload = deepcopy(run.result_payload)
                latency_ms = (
                    successful_attempt.latency_ms if successful_attempt is not None else None
                )

        try:
            outcome = evaluate_rules(
                rules=rules, result_payload=result_payload, latency_ms=latency_ms
            )
            evaluation_status = (
                EvaluationStatus.PASSED if outcome.passed else EvaluationStatus.FAILED
            )
            result = outcome.as_persisted_result()
            details: dict[str, Any] = {"rule_count": len(rules)}
        except EvaluationConfigurationError:
            evaluation_status = EvaluationStatus.ERROR
            result = None
            details = {"error_code": "INVALID_EVALUATION_RULE"}
        except Exception:
            evaluation_status = EvaluationStatus.ERROR
            result = None
            details = {"error_code": "EVALUATION_ERROR"}

        now = datetime.now(UTC)
        async with self._session_factory() as session:
            async with session.begin():
                run = await self._get_run_for_client(session, client_id=client_id, run_id=run_id)
                persisted_evaluation = await session.get(
                    Evaluation, evaluation_id, with_for_update=True
                )
                if persisted_evaluation is None or persisted_evaluation.run_id != run.id:
                    raise RuntimeError("Evaluation lifecycle record was not persisted")
                persisted_evaluation.status = evaluation_status
                persisted_evaluation.result = result
                persisted_evaluation.details = details
                persisted_evaluation.completed_at = now
                run.evaluation_status = evaluation_status
                session.add(
                    RunEvent(
                        run_id=run.id,
                        event_type=f"EVALUATION_{evaluation_status}",
                        metadata_={"evaluator": persisted_evaluation.evaluator, **details},
                    )
                )
                await session.flush()
                return self._to_evaluation_snapshot(persisted_evaluation)

    async def get_evaluations(self, *, client_id: str, run_id: UUID) -> list[EvaluationSnapshot]:
        async with self._session_factory() as session:
            await self._get_run_for_client(session, client_id=client_id, run_id=run_id)
            evaluations = await session.scalars(
                select(Evaluation)
                .where(Evaluation.run_id == run_id)
                .order_by(Evaluation.created_at.asc())
            )
            return [self._to_evaluation_snapshot(evaluation) for evaluation in evaluations]

    async def replay(
        self, *, client_id: str, run_id: UUID, idempotency_key: str
    ) -> tuple[RunSnapshot, bool]:
        """Create a distinct durable run from an immutable source snapshot."""

        with get_tracer().start_as_current_span("arr.db.run.replay") as span:
            async with self._session_factory() as session:
                try:
                    async with session.begin():
                        source = await self._get_run_for_client(
                            session, client_id=client_id, run_id=run_id
                        )
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

                        trace_context = inject_trace_context()
                        replay = Run(
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
