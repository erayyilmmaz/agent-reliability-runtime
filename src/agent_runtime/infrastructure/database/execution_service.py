from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_runtime.application.execution import ClaimDecision, ClaimResult, ExecutionResult
from agent_runtime.domain.retry import RetryPolicy, is_retryable_error
from agent_runtime.domain.states import ExecutionStatus, is_terminal_execution_status
from agent_runtime.infrastructure.database.models import OutboxEvent, Run, RunAttempt, RunEvent


class ExecutionPersistenceService:
    """Owns durable claims, attempts, terminal writes, and stale lease recovery."""

    def __init__(
        self, session_factory: async_sessionmaker[AsyncSession], *, lease_seconds: int
    ) -> None:
        self._session_factory = session_factory
        self._lease_seconds = lease_seconds

    async def claim(self, *, run_id: UUID, worker_id: str) -> ClaimResult:
        now = datetime.now(UTC)
        async with self._session_factory() as session:
            async with session.begin():
                run = await self._locked_run(session, run_id)
                if run is None:
                    return ClaimResult(decision=ClaimDecision.MISSING, run_id=run_id)
                if is_terminal_execution_status(run.execution_status):
                    return ClaimResult(decision=ClaimDecision.TERMINAL, run_id=run.id)
                if run.execution_status == ExecutionStatus.RUNNING:
                    if (
                        run.execution_lease_expires_at is None
                        or run.execution_lease_expires_at > now
                    ):
                        return ClaimResult(decision=ClaimDecision.ACTIVE_LEASE, run_id=run.id)
                    return ClaimResult(decision=ClaimDecision.NOT_READY, run_id=run.id)
                if run.execution_status != ExecutionStatus.QUEUED:
                    return ClaimResult(decision=ClaimDecision.NOT_READY, run_id=run.id)

                attempt_number = await self._next_attempt_number(session, run.id)
                retry_policy = RetryPolicy.from_snapshot(run.policy_snapshot)
                provider = retry_policy.provider_order[
                    min(attempt_number - 1, len(retry_policy.provider_order) - 1)
                ]
                attempt = RunAttempt(
                    run_id=run.id,
                    attempt_number=attempt_number,
                    provider=provider,
                    worker_id=worker_id,
                )
                run.execution_status = ExecutionStatus.RUNNING
                run.execution_lease_owner = worker_id
                run.execution_lease_expires_at = now + timedelta(seconds=self._lease_seconds)
                run.started_at = run.started_at or now
                session.add(attempt)
                await session.flush()
                session.add(
                    RunEvent(
                        run_id=run.id,
                        attempt_id=attempt.id,
                        event_type="RUN_CLAIMED",
                        metadata_={"worker_id": worker_id, "attempt_number": attempt_number},
                    )
                )
                return ClaimResult(
                    decision=ClaimDecision.CLAIMED,
                    run_id=run.id,
                    attempt_id=attempt.id,
                    input_payload=run.input_payload,
                    policy_snapshot=run.policy_snapshot,
                    provider=provider,
                )

    async def complete_success(
        self,
        *,
        run_id: UUID,
        attempt_id: UUID,
        worker_id: str,
        result: ExecutionResult,
    ) -> bool:
        now = datetime.now(UTC)
        async with self._session_factory() as session:
            async with session.begin():
                run = await self._locked_run(session, run_id)
                if run is None or not self._owns_active_lease(run, worker_id, now):
                    return False
                attempt = await self._locked_attempt(session, attempt_id)
                if attempt is None or attempt.run_id != run.id:
                    return False
                attempt.provider = result.provider
                attempt.usage_metadata = result.usage_metadata
                attempt.finished_at = now
                attempt.outcome = "SUCCEEDED"
                attempt.latency_ms = int((now - attempt.started_at).total_seconds() * 1000)
                run.execution_status = ExecutionStatus.SUCCEEDED
                run.result_payload = result.result_payload
                run.completed_at = now
                run.error_code = None
                run.next_attempt_at = None
                self._clear_lease(run)
                session.add(
                    RunEvent(
                        run_id=run.id,
                        attempt_id=attempt.id,
                        event_type="RUN_SUCCEEDED",
                        metadata_={"provider": result.provider},
                    )
                )
                return True

    async def complete_failure(
        self,
        *,
        run_id: UUID,
        attempt_id: UUID,
        worker_id: str,
        error_code: str,
    ) -> bool:
        now = datetime.now(UTC)
        async with self._session_factory() as session:
            async with session.begin():
                run = await self._locked_run(session, run_id)
                if run is None or not self._owns_active_lease(run, worker_id, now):
                    return False
                attempt = await self._locked_attempt(session, attempt_id)
                if attempt is None or attempt.run_id != run.id:
                    return False
                self._finish_failed_attempt(attempt, now, error_code)
                self._schedule_retry_or_finalize(
                    session=session,
                    run=run,
                    attempt=attempt,
                    now=now,
                    error_code=error_code,
                )
                return True

    async def recover_expired_leases(self, *, batch_size: int = 100) -> int:
        now = datetime.now(UTC)
        async with self._session_factory() as session:
            async with session.begin():
                runs = list(
                    await session.scalars(
                        select(Run)
                        .where(
                            Run.execution_status == ExecutionStatus.RUNNING,
                            Run.execution_lease_expires_at.is_not(None),
                            Run.execution_lease_expires_at <= now,
                        )
                        .order_by(Run.execution_lease_expires_at.asc())
                        .with_for_update(skip_locked=True)
                        .limit(batch_size)
                    )
                )
                for run in runs:
                    attempt = await self._latest_open_attempt(session, run.id)
                    if attempt is None:
                        continue
                    self._finish_failed_attempt(attempt, now, "EXECUTION_LEASE_EXPIRED")
                    self._schedule_retry_or_finalize(
                        session=session,
                        run=run,
                        attempt=attempt,
                        now=now,
                        error_code="EXECUTION_LEASE_EXPIRED",
                        recovery=True,
                    )
                return len(runs)

    async def schedule_due_retries(self, *, batch_size: int = 100) -> int:
        """Turn persisted due retries into outbox work; safe across scheduler restarts."""

        now = datetime.now(UTC)
        async with self._session_factory() as session:
            async with session.begin():
                runs = list(
                    await session.scalars(
                        select(Run)
                        .where(
                            Run.execution_status == ExecutionStatus.RETRY_SCHEDULED,
                            Run.next_attempt_at.is_not(None),
                            Run.next_attempt_at <= now,
                        )
                        .order_by(Run.next_attempt_at.asc())
                        .with_for_update(skip_locked=True)
                        .limit(batch_size)
                    )
                )
                for run in runs:
                    run.execution_status = ExecutionStatus.QUEUED
                    run.next_attempt_at = None
                    session.add(
                        OutboxEvent(
                            aggregate_id=run.id,
                            event_type="RUN_QUEUED",
                            payload={"trace_context": {}, "reason": "retry_due"},
                        )
                    )
                    session.add(
                        RunEvent(
                            run_id=run.id,
                            event_type="RUN_RETRY_QUEUED",
                            metadata_={"scheduled_at": now.isoformat()},
                        )
                    )
                return len(runs)

    @staticmethod
    async def _locked_run(session: AsyncSession, run_id: UUID) -> Run | None:
        return cast(
            Run | None,
            await session.scalar(select(Run).where(Run.id == run_id).with_for_update()),
        )

    @staticmethod
    async def _locked_attempt(session: AsyncSession, attempt_id: UUID) -> RunAttempt | None:
        return cast(
            RunAttempt | None,
            await session.scalar(
                select(RunAttempt).where(RunAttempt.id == attempt_id).with_for_update()
            ),
        )

    @staticmethod
    async def _next_attempt_number(session: AsyncSession, run_id: UUID) -> int:
        latest_number = await session.scalar(
            select(func.max(RunAttempt.attempt_number)).where(RunAttempt.run_id == run_id)
        )
        return int(latest_number or 0) + 1

    @staticmethod
    async def _latest_open_attempt(session: AsyncSession, run_id: UUID) -> RunAttempt | None:
        return cast(
            RunAttempt | None,
            await session.scalar(
                select(RunAttempt)
                .where(RunAttempt.run_id == run_id, RunAttempt.finished_at.is_(None))
                .order_by(RunAttempt.attempt_number.desc())
                .with_for_update()
            ),
        )

    @staticmethod
    def _owns_active_lease(run: Run | None, worker_id: str, now: datetime) -> bool:
        return (
            run is not None
            and run.execution_status == ExecutionStatus.RUNNING
            and run.execution_lease_owner == worker_id
            and run.execution_lease_expires_at is not None
            and run.execution_lease_expires_at > now
        )

    @staticmethod
    def _clear_lease(run: Run) -> None:
        run.execution_lease_owner = None
        run.execution_lease_expires_at = None

    @staticmethod
    def _finish_failed_attempt(run_attempt: RunAttempt, now: datetime, error_code: str) -> None:
        run_attempt.finished_at = now
        run_attempt.outcome = "FAILED"
        run_attempt.error_code = error_code
        run_attempt.retryable = is_retryable_error(error_code)
        run_attempt.latency_ms = int((now - run_attempt.started_at).total_seconds() * 1000)

    def _schedule_retry_or_finalize(
        self,
        *,
        session: AsyncSession,
        run: Run,
        attempt: RunAttempt,
        now: datetime,
        error_code: str,
        recovery: bool = False,
    ) -> None:
        retry_policy = RetryPolicy.from_snapshot(run.policy_snapshot)
        retryable = bool(attempt.retryable)
        self._clear_lease(run)
        run.error_code = error_code

        if retryable and attempt.attempt_number < retry_policy.max_attempts:
            delay_seconds = retry_policy.delay_after_attempt(attempt.attempt_number)
            next_attempt_at = now + timedelta(seconds=delay_seconds)
            run.execution_status = ExecutionStatus.RETRY_SCHEDULED
            run.next_attempt_at = next_attempt_at
            session.add(
                RunEvent(
                    run_id=run.id,
                    attempt_id=attempt.id,
                    event_type="RUN_RETRY_SCHEDULED",
                    metadata_={
                        "error_code": error_code,
                        "attempt_number": attempt.attempt_number,
                        "next_attempt_at": next_attempt_at.isoformat(),
                        "delay_seconds": delay_seconds,
                        "recovery": recovery,
                    },
                )
            )
            return

        run.next_attempt_at = None
        run.completed_at = now
        if retryable:
            run.execution_status = ExecutionStatus.DEAD_LETTERED
            session.add(
                OutboxEvent(
                    aggregate_id=run.id,
                    event_type="RUN_DEAD_LETTERED",
                    payload={"trace_context": {}, "error_code": error_code},
                )
            )
            event_type = "RUN_DEAD_LETTERED"
            metadata = {
                "error_code": error_code,
                "attempt_number": attempt.attempt_number,
                "max_attempts": retry_policy.max_attempts,
                "recovery": recovery,
            }
        else:
            run.execution_status = ExecutionStatus.FAILED
            event_type = "RUN_FAILED"
            metadata = {"error_code": error_code, "recovery": recovery}
        session.add(
            RunEvent(
                run_id=run.id,
                attempt_id=attempt.id,
                event_type=event_type,
                metadata_=metadata,
            )
        )
