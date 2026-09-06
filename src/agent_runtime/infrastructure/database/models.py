from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

from sqlalchemy import (
    CheckConstraint,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import (
    Enum as SqlEnum,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship
from sqlalchemy.types import Uuid

from agent_runtime.domain.states import EvaluationStatus, ExecutionStatus


class Base(DeclarativeBase):
    """SQLAlchemy metadata holder; ARR-3 introduces persistence models."""


class Run(Base):
    """Durable unit of accepted execution work; history lives in related records."""

    __tablename__ = "runs"
    __table_args__ = (
        UniqueConstraint("client_id", "idempotency_key", name="uq_runs_client_idempotency_key"),
        CheckConstraint(
            "execution_status IN ('QUEUED', 'RUNNING', 'RETRY_SCHEDULED', "
            "'SUCCEEDED', 'FAILED', 'DEAD_LETTERED')",
            name="ck_runs_execution_status",
        ),
        CheckConstraint(
            "evaluation_status IN ('NOT_RUN', 'PENDING', 'PASSED', 'FAILED', 'ERROR')",
            name="ck_runs_evaluation_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    client_id: Mapped[str] = mapped_column(String(128), nullable=False, index=True)
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False)
    request_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    input_payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    policy_snapshot: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    trace_context: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False, default=dict)
    execution_status: Mapped[ExecutionStatus] = mapped_column(
        SqlEnum(ExecutionStatus, native_enum=False, create_constraint=False, length=32),
        nullable=False,
        default=ExecutionStatus.QUEUED,
    )
    evaluation_status: Mapped[EvaluationStatus] = mapped_column(
        SqlEnum(EvaluationStatus, native_enum=False, create_constraint=False, length=32),
        nullable=False,
        default=EvaluationStatus.NOT_RUN,
    )
    result_payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    execution_lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    execution_lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )
    replay_of_run_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("runs.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    replay_of: Mapped[Run | None] = relationship(remote_side="Run.id", back_populates="replays")
    replays: Mapped[list[Run]] = relationship(back_populates="replay_of")
    attempts: Mapped[list[RunAttempt]] = relationship(back_populates="run")
    events: Mapped[list[RunEvent]] = relationship(back_populates="run")
    evaluations: Mapped[list[Evaluation]] = relationship(back_populates="run")

    __mapper_args__ = {"version_id_col": version}


class RunAttempt(Base):
    """One worker claim to execute a run; attempt numbers never repeat per run."""

    __tablename__ = "run_attempts"
    __table_args__ = (
        UniqueConstraint("run_id", "attempt_number", name="uq_run_attempts_run_number"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("runs.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    attempt_number: Mapped[int] = mapped_column(Integer, nullable=False)
    provider: Mapped[str] = mapped_column(String(64), nullable=False)
    worker_id: Mapped[str] = mapped_column(String(128), nullable=False)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    outcome: Mapped[str | None] = mapped_column(String(32), nullable=True)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    retryable: Mapped[bool | None] = mapped_column(nullable=True)
    latency_ms: Mapped[int | None] = mapped_column(Integer, nullable=True)
    usage_metadata: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)

    run: Mapped[Run] = relationship(back_populates="attempts")
    events: Mapped[list[RunEvent]] = relationship(back_populates="attempt")


class RunEvent(Base):
    """Append-only execution facts; database triggers reject updates and deletes."""

    __tablename__ = "run_events"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("runs.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    attempt_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("run_attempts.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    metadata_: Mapped[dict[str, Any] | None] = mapped_column("metadata", JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    run: Mapped[Run] = relationship(back_populates="events")
    attempt: Mapped[RunAttempt | None] = relationship(back_populates="events")


class OutboxEvent(Base):
    """Delivery intent coupled transactionally to a durable aggregate change."""

    __tablename__ = "outbox_events"

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    aggregate_id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    event_type: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    publish_attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)


class Evaluation(Base):
    """Evaluation output is separate from technical execution state."""

    __tablename__ = "evaluations"
    __table_args__ = (
        CheckConstraint(
            "status IN ('NOT_RUN', 'PENDING', 'PASSED', 'FAILED', 'ERROR')",
            name="ck_evaluations_status",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("runs.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    evaluator: Mapped[str] = mapped_column(String(128), nullable=False)
    status: Mapped[EvaluationStatus] = mapped_column(
        SqlEnum(EvaluationStatus, native_enum=False, create_constraint=False, length=32),
        nullable=False,
    )
    score: Mapped[float | None] = mapped_column(nullable=True)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )

    run: Mapped[Run] = relationship(back_populates="evaluations")
