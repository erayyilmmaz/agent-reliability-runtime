from __future__ import annotations

import logging
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
    EvaluateRunRequest,
    EvaluationResponse,
    EventResponse,
    ReplayRunResponse,
    RunResponse,
)
from agent_runtime.application.runs import (
    AttemptSnapshot,
    EvaluationSnapshot,
    EventSnapshot,
    IdempotencyConflictError,
    RunNotFoundError,
    RunNotSucceededError,
    RunSnapshot,
    RunSubmissionService,
)
from agent_runtime.domain.retry import build_policy_snapshot
from agent_runtime.infrastructure.database.run_service import SqlAlchemyRunService
from agent_runtime.infrastructure.database.session import (
    create_database_engine,
    create_session_factory,
)
from agent_runtime.infrastructure.redis.rate_limiter import (
    NoopRateLimiter,
    RateLimiter,
    RateLimitUnavailable,
    RedisFixedWindowRateLimiter,
)
from agent_runtime.observability.telemetry import extract_trace_context, get_tracer
from agent_runtime.security import (
    NoopSecurityAuditSink,
    SecurityAuditRecord,
    SecurityAuditSink,
    SqlAlchemySecurityAuditSink,
    authenticate_api_key,
)
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
        routing_decision=run.routing_decision,
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


def _to_evaluation_response(evaluation: EvaluationSnapshot) -> EvaluationResponse:
    return EvaluationResponse(
        evaluation_id=evaluation.id,
        evaluator=evaluation.evaluator,
        status=evaluation.status,
        score=evaluation.score,
        result=evaluation.result,
        details=evaluation.details,
        created_at=evaluation.created_at,
        completed_at=evaluation.completed_at,
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
    settings: Settings | None = None,
    run_service: RunSubmissionService | None = None,
    rate_limiter: RateLimiter | None = None,
    audit_sink: SecurityAuditSink | None = None,
) -> FastAPI:
    """Create the HTTP process without initializing workers or providers."""

    runtime_settings = settings or get_settings()
    engine: AsyncEngine | None = None
    owns_rate_limiter = rate_limiter is None
    if run_service is None:
        engine = create_database_engine(runtime_settings)
        session_factory = create_session_factory(engine)
        run_service = SqlAlchemyRunService(session_factory)
        audit_sink = audit_sink or SqlAlchemySecurityAuditSink(session_factory)
    else:
        audit_sink = audit_sink or NoopSecurityAuditSink()
    if rate_limiter is None:
        rate_limiter = (
            RedisFixedWindowRateLimiter(
                redis_url=str(runtime_settings.redis_url),
                limit=runtime_settings.rate_limit_requests,
                window_seconds=runtime_settings.rate_limit_window_seconds,
            )
            if runtime_settings.auth_mode == "api_key"
            else NoopRateLimiter()
        )

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        yield
        if owns_rate_limiter:
            await rate_limiter.close()
        if engine is not None:
            await engine.dispose()

    app = FastAPI(title="Agent Reliability Runtime", version="0.1.0", lifespan=lifespan)
    app.state.settings = runtime_settings
    app.state.run_service = run_service

    async def write_security_audit(
        *,
        event_type: str,
        outcome: str,
        reason: str,
        client_id: str | None,
        credential_fingerprint: str | None,
    ) -> None:
        try:
            await audit_sink.record(
                SecurityAuditRecord(
                    event_type=event_type,
                    outcome=outcome,
                    reason=reason,
                    client_id=client_id,
                    credential_fingerprint=credential_fingerprint,
                )
            )
        except Exception:
            logging.getLogger(__name__).error(
                "security audit persistence failed",
                extra={
                    "event": "SECURITY_AUDIT_PERSIST_FAILED",
                    "error_code": "AUDIT_WRITE_FAILED",
                },
            )

    @app.middleware("http")
    async def enforce_security_boundary(request: Request, call_next: Any) -> Any:
        if not request.url.path.startswith("/v1/"):
            return await call_next(request)
        client_id = request.headers.get("X-Client-Id")
        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                is_oversized = int(content_length) > runtime_settings.max_request_bytes
            except ValueError:
                is_oversized = False
            if is_oversized:
                await write_security_audit(
                    event_type="REQUEST_REJECTED",
                    outcome="DENIED",
                    reason="REQUEST_TOO_LARGE",
                    client_id=client_id,
                    credential_fingerprint=None,
                )
                return _security_error(
                    status.HTTP_413_CONTENT_TOO_LARGE,
                    "REQUEST_TOO_LARGE",
                    "Request body exceeds the configured limit.",
                )

        authentication = authenticate_api_key(
            settings=runtime_settings,
            x_api_key=request.headers.get("X-API-Key"),
            authorization=request.headers.get("Authorization"),
        )
        if not authentication.authenticated:
            status_code = (
                status.HTTP_401_UNAUTHORIZED
                if authentication.failure_code == "AUTHENTICATION_REQUIRED"
                else status.HTTP_403_FORBIDDEN
            )
            await write_security_audit(
                event_type="AUTHENTICATION",
                outcome="DENIED",
                reason=authentication.failure_code or "INVALID_CREDENTIAL",
                client_id=client_id,
                credential_fingerprint=authentication.credential_fingerprint,
            )
            return _security_error(
                status_code,
                authentication.failure_code or "INVALID_CREDENTIAL",
                (
                    "Authentication is required."
                    if status_code == 401
                    else "Credential is not accepted."
                ),
            )

        if runtime_settings.auth_mode == "api_key" and client_id is not None and client_id.strip():
            try:
                decision = await rate_limiter.check(client_id.strip())
            except RateLimitUnavailable:
                await write_security_audit(
                    event_type="RATE_LIMIT",
                    outcome="ERROR",
                    reason="RATE_LIMIT_UNAVAILABLE",
                    client_id=client_id.strip(),
                    credential_fingerprint=authentication.credential_fingerprint,
                )
                return _security_error(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    "RATE_LIMIT_UNAVAILABLE",
                    "Request rate limiting is temporarily unavailable.",
                )
            if not decision.allowed:
                await write_security_audit(
                    event_type="RATE_LIMIT",
                    outcome="DENIED",
                    reason="RATE_LIMIT_EXCEEDED",
                    client_id=client_id.strip(),
                    credential_fingerprint=authentication.credential_fingerprint,
                )
                return _security_error(
                    status.HTTP_429_TOO_MANY_REQUESTS,
                    "RATE_LIMIT_EXCEEDED",
                    "Request rate limit exceeded.",
                    headers={"Retry-After": str(decision.retry_after_seconds)},
                )

        if runtime_settings.auth_mode == "api_key":
            await write_security_audit(
                event_type="AUTHENTICATION",
                outcome="ALLOWED",
                reason="AUTHENTICATED",
                client_id=client_id.strip() if client_id and client_id.strip() else None,
                credential_fingerprint=authentication.credential_fingerprint,
            )
        return await call_next(request)

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

    @app.exception_handler(Exception)
    async def internal_error_handler(_: Request, exc: Exception) -> JSONResponse:
        logging.getLogger(__name__).error(
            "unhandled api error",
            extra={"event": "API_UNHANDLED_ERROR", "error_code": type(exc).__name__},
        )
        return _security_error(
            status.HTTP_500_INTERNAL_SERVER_ERROR,
            "INTERNAL_ERROR",
            "An internal error occurred.",
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
                available_providers={"deterministic"}
                | ({"openai"} if runtime_settings.openai_api_key is not None else set()),
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
            run_id=run.id,
            execution_status=run.execution_status,
            replayed=replayed,
            routing_decision=run.routing_decision,
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

    @app.post(
        "/v1/runs/{run_id}/evaluations",
        response_model=EvaluationResponse,
        responses={404: {"model": ApiErrorResponse}, 409: {"model": ApiErrorResponse}},
    )
    async def evaluate_run(
        run_id: UUID,
        payload: EvaluateRunRequest,
        request: Request,
        client_id: Annotated[str | None, Header(alias="X-Client-Id")] = None,
    ) -> EvaluationResponse:
        try:
            evaluation = await _get_service(request).evaluate(
                client_id=_required_client_id(client_id), run_id=run_id, rules=payload.rules
            )
        except RunNotFoundError as exc:
            raise ApiProblem(
                status_code=status.HTTP_404_NOT_FOUND, code="RUN_NOT_FOUND", message=str(exc)
            ) from exc
        except RunNotSucceededError as exc:
            raise ApiProblem(
                status_code=status.HTTP_409_CONFLICT,
                code="RUN_NOT_SUCCEEDED",
                message=str(exc),
            ) from exc
        return _to_evaluation_response(evaluation)

    @app.get(
        "/v1/runs/{run_id}/evaluations",
        response_model=list[EvaluationResponse],
        responses={404: {"model": ApiErrorResponse}},
    )
    async def get_evaluations(
        run_id: UUID,
        request: Request,
        client_id: Annotated[str | None, Header(alias="X-Client-Id")] = None,
    ) -> list[EvaluationResponse]:
        try:
            evaluations = await _get_service(request).get_evaluations(
                client_id=_required_client_id(client_id), run_id=run_id
            )
        except RunNotFoundError as exc:
            raise ApiProblem(
                status_code=status.HTTP_404_NOT_FOUND, code="RUN_NOT_FOUND", message=str(exc)
            ) from exc
        return [_to_evaluation_response(evaluation) for evaluation in evaluations]

    @app.post(
        "/v1/runs/{run_id}/replay",
        response_model=ReplayRunResponse,
        responses={
            422: {"model": ApiErrorResponse},
            404: {"model": ApiErrorResponse},
            409: {"model": ApiErrorResponse},
        },
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def replay_run(
        run_id: UUID,
        request: Request,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
        client_id: Annotated[str | None, Header(alias="X-Client-Id")] = None,
    ) -> ReplayRunResponse:
        if idempotency_key is None:
            raise ApiProblem(
                status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
                code="MISSING_IDEMPOTENCY_KEY",
                message="Idempotency-Key header is required.",
            )
        try:
            replay, replayed = await _get_service(request).replay(
                client_id=_required_client_id(client_id),
                run_id=run_id,
                idempotency_key=_validate_idempotency_key(idempotency_key),
            )
        except RunNotFoundError as exc:
            raise ApiProblem(
                status_code=status.HTTP_404_NOT_FOUND, code="RUN_NOT_FOUND", message=str(exc)
            ) from exc
        except IdempotencyConflictError as exc:
            raise ApiProblem(
                status_code=status.HTTP_409_CONFLICT,
                code="IDEMPOTENCY_KEY_REUSED",
                message=str(exc),
            ) from exc
        if replay.replay_of_run_id is None:
            raise RuntimeError("Replay must retain its source run")
        return ReplayRunResponse(
            run_id=replay.id,
            execution_status=replay.execution_status,
            replay_of_run_id=replay.replay_of_run_id,
            replayed=replayed,
        )

    return app


def _required_client_id(client_id: str | None) -> str:
    if client_id is None or not client_id.strip():
        raise ApiProblem(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            code="MISSING_CLIENT_ID",
            message="X-Client-Id header is required until authentication is introduced.",
        )
    return client_id.strip()


def _security_error(
    status_code: int, code: str, message: str, headers: dict[str, str] | None = None
) -> JSONResponse:
    return JSONResponse(
        status_code=status_code,
        content={"error": {"code": code, "message": message}},
        headers=headers,
    )


app = create_app()
