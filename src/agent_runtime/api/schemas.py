from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, JsonValue, model_validator

from agent_runtime.domain.states import EvaluationStatus, ExecutionStatus

MAX_INPUT_BYTES = 65_536
MAX_REQUEST_BYTES = 131_072


class CreateRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    input: dict[str, JsonValue] = Field(min_length=1)
    policy: dict[str, JsonValue] = Field(default_factory=dict)

    @model_validator(mode="after")
    def validate_input_size(self) -> CreateRunRequest:
        encoded_input = self.model_dump_json(include={"input"}).encode("utf-8")
        if len(encoded_input) > MAX_INPUT_BYTES:
            raise ValueError(f"input must not exceed {MAX_INPUT_BYTES} bytes")
        if len(self.model_dump_json().encode("utf-8")) > MAX_REQUEST_BYTES:
            raise ValueError(f"request must not exceed {MAX_REQUEST_BYTES} bytes")
        return self


class CreateRunResponse(BaseModel):
    run_id: UUID
    execution_status: ExecutionStatus
    replayed: bool


class RunResponse(BaseModel):
    run_id: UUID
    execution_status: ExecutionStatus
    evaluation_status: EvaluationStatus
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    replay_of_run_id: UUID | None
    error_code: str | None


class AttemptResponse(BaseModel):
    attempt_id: UUID
    attempt_number: int
    provider: str
    started_at: datetime
    finished_at: datetime | None
    outcome: str | None
    error_code: str | None
    retryable: bool | None
    latency_ms: int | None


class EventResponse(BaseModel):
    event_id: UUID
    attempt_id: UUID | None
    event_type: str
    metadata: dict[str, Any] | None
    created_at: datetime


class EvaluateRunRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    rules: list[dict[str, JsonValue]] = Field(min_length=1, max_length=32)


class EvaluationResponse(BaseModel):
    evaluation_id: UUID
    evaluator: str
    status: EvaluationStatus
    score: float | None
    result: dict[str, JsonValue] | None
    details: dict[str, JsonValue] | None
    created_at: datetime
    completed_at: datetime | None


class ReplayRunResponse(BaseModel):
    run_id: UUID
    execution_status: ExecutionStatus
    replay_of_run_id: UUID
    replayed: bool


class ApiErrorBody(BaseModel):
    code: str
    message: str
    details: list[dict[str, Any]] | None = None


class ApiErrorResponse(BaseModel):
    error: ApiErrorBody
