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
    routing_decision: dict[str, JsonValue] | None = None


class RunResponse(BaseModel):
    run_id: UUID
    execution_status: ExecutionStatus
    evaluation_status: EvaluationStatus
    created_at: datetime
    started_at: datetime | None
    completed_at: datetime | None
    replay_of_run_id: UUID | None
    error_code: str | None
    routing_decision: dict[str, JsonValue] | None


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


class EvaluationRegressionCaseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    case_id: str = Field(min_length=1, max_length=128)
    input: dict[str, JsonValue] = Field(min_length=1)
    rules: list[dict[str, JsonValue]] = Field(min_length=1, max_length=32)


class EvaluationRegressionDatasetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset_id: str = Field(min_length=1, max_length=128)
    version: str = Field(min_length=1, max_length=64)
    cases: list[EvaluationRegressionCaseRequest] = Field(min_length=1, max_length=100)


class EvaluationRegressionTargetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    provider: str = Field(min_length=1, max_length=64)
    model: str | None = Field(default=None, min_length=1, max_length=128)
    estimated_cost_microusd: int = Field(default=0, ge=0)


class EvaluationRegressionGatesRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    max_quality_regression_points: float = Field(default=0.0, ge=0)
    max_latency_regression_percent: float | None = Field(default=None, ge=0)
    max_cost_regression_percent: float | None = Field(default=None, ge=0)


class EvaluationRegressionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dataset: EvaluationRegressionDatasetRequest
    baseline: EvaluationRegressionTargetRequest
    candidate: EvaluationRegressionTargetRequest
    gates: EvaluationRegressionGatesRequest = Field(
        default_factory=EvaluationRegressionGatesRequest
    )


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
