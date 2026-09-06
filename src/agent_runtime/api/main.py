from __future__ import annotations

import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import FastAPI, Header, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncEngine

from agent_runtime.api.schemas import (
    ApiErrorResponse,
    AttemptResponse,
    CreateRunRequest,
    CreateRunResponse,
    EventResponse,
    RunResponse,
)
from agent_runtime.application.runs import (
    AttemptSnapshot,
    EventSnapshot,
    IdempotencyConflictError,
    RunNotFoundError,
    RunSnapshot,
    RunSubmissionService,
)
from agent_runtime.domain.retry import build_policy_snapshot
from agent_runtime.infrastructure.database.run_service import SqlAlchemyRunService
from agent_runtime.infrastructure.database.session import (
    create_database_engine,
    create_session_factory,
)
from agent_runtime.observability.telemetry import extract_trace_context, get_tracer
from agent_runtime.settings import Settings, get_settings

IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,254}$")


class ApiProblem(Exception):
    def __init__(self, *, status_code: int, code: str, message: str) -> None:
        self.status_code = status_code
        self.code = code
        self.message = message


def _to_run_response(run: RunSnapshot) -> RunResponse:
    return RunResponse(
        run_id=run.id,
        execution_status=run.execution_status,
        evaluation_status=run.evaluation_status,
        created_at=run.created_at,
        started_at=run.started_at,
        completed_at=run.completed_at,
        replay_of_run_id=run.replay_of_run_id,
        error_code=run.error_code,
    )


def _to_attempt_response(attempt: AttemptSnapshot) -> AttemptResponse:
    return AttemptResponse(
        attempt_id=attempt.id,
        attempt_number=attempt.attempt_number,
        provider=attempt.provider,
        started_at=attempt.started_at,
        finished_at=attempt.finished_at,
        outcome=attempt.outcome,
        error_code=attempt.error_code,
        retryable=attempt.retryable,
        latency_ms=attempt.latency_ms,
    )


def _to_event_response(event: EventSnapshot) -> EventResponse:
    return EventResponse(
        event_id=event.id,
        attempt_id=event.attempt_id,
        event_type=event.event_type,
        metadata=event.metadata,
        created_at=event.created_at,
    )


def _validate_idempotency_key(value: str) -> str:
    if not IDEMPOTENCY_KEY_PATTERN.fullmatch(value):
        raise ApiProblem(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            code="INVALID_IDEMPOTENCY_KEY",
            message="Idempotency-Key must contain 8-255 URL-safe characters.",
        )
    return value


def _get_service(request: Request) -> RunSubmissionService:
    return cast(RunSubmissionService, request.app.state.run_service)


def create_app(
    settings: Settings | None = None, run_service: RunSubmissionService | None = None
) -> FastAPI:
    """Create the HTTP process without initializing workers or providers."""

    runtime_settings = settings or get_settings()
    engine: AsyncEngine | None = None
    if run_service is None:
        engine = create_database_engine(runtime_settings)
        run_service = SqlAlchemyRunService(create_session_factory(engine))

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        if engine is not None:
            await engine.dispose()

    app = FastAPI(title="Agent Reliability Runtime", version="0.1.0", lifespan=lifespan)
    app.state.settings = runtime_settings
    app.state.run_service = run_service

    @app.middleware("http")
    async def trace_http_request(request: Request, call_next: Any) -> Any:
        carrier = {
            header: request.headers[header]
            for header in ("traceparent", "tracestate")
            if header in request.headers
        }
        with get_tracer().start_as_current_span(
            f"HTTP {request.method}", context=extract_trace_context(carrier)
        ) as span:
            span.set_attribute("http.request.method", request.method)
            try:
                response = await call_next(request)
            except Exception as exc:
                span.record_exception(exc)
                raise
            span.set_attribute("http.response.status_code", response.status_code)
            return response

    @app.exception_handler(ApiProblem)
    async def api_problem_handler(_: Request, exc: ApiProblem) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code,
            content={"error": {"code": exc.code, "message": exc.message}},
        )

    @app.exception_handler(RequestValidationError)
    async def validation_error_handler(_: Request, exc: RequestValidationError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={
                "error": {
                    "code": "VALIDATION_ERROR",
                    "message": "Request validation failed.",
                    "details": exc.errors(),
                }
            },
        )

    @app.get("/healthz", tags=["operations"])
    async def healthz() -> dict[str, str]:
        return {"status": "ok", "service": "api"}

    @app.post(
        "/v1/runs",
        response_model=CreateRunResponse,
        responses={422: {"model": ApiErrorResponse}, 409: {"model": ApiErrorResponse}},
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def create_run(
        payload: CreateRunRequest,
        request: Request,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
        client_id: Annotated[str | None, Header(alias="X-Client-Id")] = None,
    ) -> CreateRunResponse:
        if idempotency_key is None:
            raise ApiProblem(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                code="MISSING_IDEMPOTENCY_KEY",
                message="Idempotency-Key header is required.",
            )
        if client_id is None or not client_id.strip():
            raise ApiProblem(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                code="MISSING_CLIENT_ID",
                message="X-Client-Id header is required until authentication is introduced.",
            )

        try:
            policy_snapshot = build_policy_snapshot(
                payload.policy,
                max_attempts=runtime_settings.retry_max_attempts,
                attempt_timeout_seconds=runtime_settings.retry_attempt_timeout_seconds,
                initial_backoff_seconds=runtime_settings.retry_base_delay_seconds,
                max_backoff_seconds=runtime_settings.retry_max_backoff_seconds,
            )
            run, replayed = await _get_service(request).submit(
                client_id=client_id.strip(),
                idempotency_key=_validate_idempotency_key(idempotency_key),
                input_payload=payload.input,
                policy_snapshot=policy_snapshot,
            )
        except IdempotencyConflictError as exc:
            raise ApiProblem(
                status_code=status.HTTP_409_CONFLICT,
                code="IDEMPOTENCY_KEY_REUSED",
                message=str(exc),
            ) from exc
        except ValueError as exc:
            raise ApiProblem(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                code="INVALID_RETRY_POLICY",
                message=str(exc),
            ) from exc

        return CreateRunResponse(
            run_id=run.id, execution_status=run.execution_status, replayed=replayed
        )

    @app.get(
        "/v1/runs/{run_id}",
        response_model=RunResponse,
        responses={404: {"model": ApiErrorResponse}},
    )
    async def get_run(
        run_id: UUID,
        request: Request,
        client_id: Annotated[str | None, Header(alias="X-Client-Id")] = None,
    ) -> RunResponse:
        try:
            run = await _get_service(request).get_run(
                client_id=_required_client_id(client_id), run_id=run_id
            )
        except RunNotFoundError as exc:
            raise ApiProblem(
                status_code=status.HTTP_404_NOT_FOUND, code="RUN_NOT_FOUND", message=str(exc)
            ) from exc
        return _to_run_response(run)

    @app.get(
        "/v1/runs/{run_id}/attempts",
        response_model=list[AttemptResponse],
        responses={404: {"model": ApiErrorResponse}},
    )
    async def get_attempts(
        run_id: UUID,
        request: Request,
        client_id: Annotated[str | None, Header(alias="X-Client-Id")] = None,
    ) -> list[AttemptResponse]:
        try:
            attempts = await _get_service(request).get_attempts(
                client_id=_required_client_id(client_id), run_id=run_id
            )
        except RunNotFoundError as exc:
            raise ApiProblem(
                status_code=status.HTTP_404_NOT_FOUND, code="RUN_NOT_FOUND", message=str(exc)
            ) from exc
        return [_to_attempt_response(attempt) for attempt in attempts]

    @app.get(
        "/v1/runs/{run_id}/events",
        response_model=list[EventResponse],
        responses={404: {"model": ApiErrorResponse}},
    )
    async def get_events(
        run_id: UUID,
        request: Request,
        client_id: Annotated[str | None, Header(alias="X-Client-Id")] = None,
    ) -> list[EventResponse]:
        try:
            events = await _get_service(request).get_events(
                client_id=_required_client_id(client_id), run_id=run_id
            )
        except RunNotFoundError as exc:
            raise ApiProblem(
                status_code=status.HTTP_404_NOT_FOUND, code="RUN_NOT_FOUND", message=str(exc)
            ) from exc
        return [_to_event_response(event) for event in events]

    return app


def _required_client_id(client_id: str | None) -> str:
    if client_id is None or not client_id.strip():
        raise ApiProblem(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            code="MISSING_CLIENT_ID",
            message="X-Client-Id header is required until authentication is introduced.",
        )
    return client_id.strip()


app = create_app()
