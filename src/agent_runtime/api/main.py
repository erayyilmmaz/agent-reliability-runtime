from __future__ import annotations

import asyncio
import re
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import partial
from typing import Annotated, Any, cast
from uuid import UUID

from fastapi import FastAPI, Header, Query, Request, Response, status
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from sqlalchemy.ext.asyncio import AsyncEngine
from starlette.concurrency import run_in_threadpool

from agent_runtime.api.body_limit import BodyLimitMiddleware
from agent_runtime.api.schemas import (
    ApiErrorResponse,
    AttemptResponse,
    CreateRunRequest,
    CreateRunResponse,
    EvaluateRunRequest,
    EvaluationRegressionRequest,
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
    InvalidCursorError,
    RunNotFoundError,
    RunNotSucceededError,
    RunSnapshot,
    RunSubmissionService,
)
from agent_runtime.domain.retry import build_policy_snapshot
from agent_runtime.evaluation.regression import (
    ProviderModelTarget,
    RegressionDataset,
)
from agent_runtime.infrastructure.database.quotas import QuotaExceededError, QuotaLimits
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
from agent_runtime.observability.exceptions import record_safe_exception
from agent_runtime.observability.heartbeat import Heartbeat
from agent_runtime.observability.metrics import get_runtime_metrics
from agent_runtime.observability.telemetry import (
    extract_trace_context,
    safe_span,
    sanitize_trace_context,
)
from agent_runtime.security import (
    NoopSecurityAuditSink,
    SecurityAuditRecord,
    SecurityAuditSink,
    SqlAlchemySecurityAuditSink,
    authenticate_api_key,
)
from agent_runtime.security.credentials import CLIENT_ID_PATTERN
from agent_runtime.settings import Settings, get_settings

IDEMPOTENCY_KEY_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{7,254}$")
PUBLIC_ENDPOINTS = frozenset({("GET", "/healthz")})


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


def _validate_regression_targets(settings: Settings, *targets: ProviderModelTarget) -> None:
    for target in targets:
        if target.provider not in {"deterministic", "openai"}:
            raise ValueError(f"provider '{target.provider}' is not configured")
        if target.provider == "openai" and settings.openai_api_key is None:
            raise ValueError("openai target requires APP_OPENAI_API_KEY")


SENSITIVE_READ_PATH = re.compile(
    r"/v1/(runs|jobs)/([0-9a-fA-F-]{36})(?:/(attempts|events|evaluations))?"
)


def _record_http_duration(request: Request, status_code: int, seconds: float) -> None:
    """Record request latency against the matched route template.

    Starlette populates scope["route"] during routing, so the template is
    available once the handler has run. An unmatched path has no template and
    collapses to a single series rather than emitting the raw URL.
    """

    route = getattr(request.scope.get("route"), "path_format", None)
    get_runtime_metrics().http_request(
        route=route,
        method=request.method,
        status_code=status_code,
        seconds=seconds,
    )


def _sensitive_read_target(request: Request) -> tuple[UUID, str] | None:
    """Return (target, resource) when this request will produce a read audit row."""

    if request.method != "GET":
        return None
    match = SENSITIVE_READ_PATH.fullmatch(request.url.path)
    if match is None:
        return None
    try:
        target = UUID(match[2])
    except ValueError:
        return None
    return target, match[3] or ("job" if match[1] == "jobs" else "run")


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
        run_service = SqlAlchemyRunService(
            session_factory,
            quotas=QuotaLimits.from_settings(runtime_settings),
            job_timeout_seconds=min(
                runtime_settings.retry_attempt_timeout_seconds,
                runtime_settings.execution_lease_seconds - 1,
            ),
        )
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

    async def _emit_heartbeat(heartbeat: Heartbeat) -> None:
        """Prove the event loop is still scheduling work (SEC-OPS-01).

        A blocked loop stops writing this, which is exactly the failure mode a
        long-running evaluation rule would cause.
        """

        while True:
            heartbeat.beat()
            await asyncio.sleep(runtime_settings.heartbeat_interval_seconds)

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        heartbeat = Heartbeat(runtime_settings.heartbeat_path, component="api")
        pulse = asyncio.create_task(_emit_heartbeat(heartbeat)) if heartbeat.enabled else None
        try:
            yield
        finally:
            if pulse is not None:
                pulse.cancel()
                await asyncio.gather(pulse, return_exceptions=True)
            if owns_rate_limiter:
                await rate_limiter.close()
            if engine is not None:
                await engine.dispose()

    local_docs = runtime_settings.auth_mode == "disabled"
    app = FastAPI(
        title="Agent Reliability Runtime",
        version="0.1.0",
        lifespan=lifespan,
        docs_url="/docs" if local_docs else None,
        redoc_url="/redoc" if local_docs else None,
        openapi_url="/openapi.json" if local_docs else None,
    )
    app.state.settings = runtime_settings
    app.state.run_service = run_service

    async def write_security_audit(
        *,
        event_type: str,
        outcome: str,
        reason: str,
        client_id: str | None,
        credential_fingerprint: str | None,
        principal_id: str | None = None,
        target_run_id: UUID | None = None,
        resource: str | None = None,
    ) -> bool:
        try:
            await asyncio.wait_for(
                audit_sink.record(
                    SecurityAuditRecord(
                        event_type=event_type,
                        outcome=outcome,
                        reason=reason,
                        client_id=client_id,
                        credential_fingerprint=credential_fingerprint,
                        principal_id=principal_id,
                        target_run_id=target_run_id,
                        resource=resource,
                    )
                ),
                timeout=runtime_settings.audit_write_timeout_seconds,
            )
            get_runtime_metrics().security_event("audit_write", "success")
            return True
        except Exception as exc:
            get_runtime_metrics().security_event("audit_write", "error")
            record_safe_exception(exc, event="AUDIT_WRITE_FAILED")
            return runtime_settings.audit_failure_policy == "fail_open"

    @app.middleware("http")
    async def enforce_security_boundary(request: Request, call_next: Any) -> Any:
        if (request.method, request.url.path) in PUBLIC_ENDPOINTS:
            return await call_next(request)
        caller_client_ids = request.headers.getlist("X-Client-Id")
        caller_client_id = caller_client_ids[0] if caller_client_ids else None
        if len(caller_client_ids) > 1 or (
            caller_client_id is not None
            and re.fullmatch(CLIENT_ID_PATTERN, caller_client_id) is None
        ):
            return _security_error(
                status.HTTP_422_UNPROCESSABLE_CONTENT,
                "INVALID_CLIENT_ID",
                "X-Client-Id must contain 1-128 URL-safe identity characters.",
            )
        # Never persist unauthenticated identity assertions in audit records.
        client_id = None
        authentication = await run_in_threadpool(
            partial(
                authenticate_api_key,
                settings=runtime_settings,
                x_api_key=request.headers.get("X-API-Key"),
                authorization=request.headers.get("Authorization"),
            )
        )
        if not authentication.authenticated:
            get_runtime_metrics().security_event("auth", "denied")
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

        client_id = authentication.tenant_id
        request.state.tenant_id = client_id
        request.state.principal_id = authentication.principal_id
        get_runtime_metrics().security_event("auth", "allowed")
        if runtime_settings.auth_mode == "api_key":
            if authentication.principal_id is None or client_id is None:
                raise RuntimeError("Authenticated credential has no identity binding")
            try:
                decision = await rate_limiter.check(authentication.principal_id)
            except RateLimitUnavailable:
                get_runtime_metrics().security_event("rate_limit", "error")
                await write_security_audit(
                    event_type="RATE_LIMIT",
                    outcome="ERROR",
                    reason="RATE_LIMIT_UNAVAILABLE",
                    client_id=client_id,
                    credential_fingerprint=authentication.credential_fingerprint,
                )
                return _security_error(
                    status.HTTP_503_SERVICE_UNAVAILABLE,
                    "RATE_LIMIT_UNAVAILABLE",
                    "Request rate limiting is temporarily unavailable.",
                )
            if not decision.allowed:
                get_runtime_metrics().security_event("rate_limit", "denied")
                await write_security_audit(
                    event_type="RATE_LIMIT",
                    outcome="DENIED",
                    reason="RATE_LIMIT_EXCEEDED",
                    client_id=client_id,
                    credential_fingerprint=authentication.credential_fingerprint,
                )
                return _security_error(
                    status.HTTP_429_TOO_MANY_REQUESTS,
                    "RATE_LIMIT_EXCEEDED",
                    "Request rate limit exceeded.",
                    headers={"Retry-After": str(decision.retry_after_seconds)},
                )

        read_target = _sensitive_read_target(request)

        # PERF-003: a SENSITIVE_READ record carries every field an
        # AUTHENTICATION/ALLOWED record carries, plus the target and resource.
        # On an audited read path the second row is therefore pure write
        # amplification -- and reads are the highest-volume path, because
        # wait_for_terminal polls. Writes and unaudited paths still get the
        # AUTHENTICATION row, which is their only record.
        #
        # The two cannot simply share a transaction: this one must commit
        # before the handler runs so a fail-closed policy can reject without
        # doing the work, while the read record needs the response status.
        # Skipping the redundant row is the change that holds that ordering.
        if runtime_settings.auth_mode == "api_key" and read_target is None:
            audited = await write_security_audit(
                event_type="AUTHENTICATION",
                outcome="ALLOWED",
                reason="AUTHENTICATED",
                client_id=client_id,
                credential_fingerprint=authentication.credential_fingerprint,
                principal_id=authentication.principal_id,
            )
            if not audited:
                return _security_error(503, "AUDIT_UNAVAILABLE", "Security audit is unavailable.")

        async def audit_read(status_code: int) -> bool:
            if read_target is None:
                return True
            target, resource = read_target
            outcome = (
                "ALLOWED" if status_code < 400 else ("ERROR" if status_code >= 500 else "DENIED")
            )
            get_runtime_metrics().security_event("sensitive_read", outcome.lower())
            return await write_security_audit(
                event_type="SENSITIVE_READ",
                outcome=outcome,
                reason="READ_COMPLETED" if outcome == "ALLOWED" else "READ_REJECTED",
                client_id=client_id,
                principal_id=authentication.principal_id,
                credential_fingerprint=authentication.credential_fingerprint,
                target_run_id=target,
                resource=resource,
            )

        try:
            response = await call_next(request)
        except Exception:
            await audit_read(500)
            raise
        if not await audit_read(response.status_code):
            return _security_error(503, "AUDIT_UNAVAILABLE", "Security audit is unavailable.")
        return response

    @app.middleware("http")
    async def trace_http_request(request: Request, call_next: Any) -> Any:
        carrier = {
            header: request.headers[header]
            for header in ("traceparent", "tracestate")
            if header in request.headers
        }
        valid = sanitize_trace_context(carrier)
        if not runtime_settings.trust_inbound_trace_context or any(
            len(request.headers.getlist(h)) > 1 for h in ("traceparent", "tracestate")
        ):
            valid = {}
        if carrier != valid:
            get_runtime_metrics().security_event("trace_context", "dropped")
        with safe_span(f"HTTP {request.method}", context=extract_trace_context(valid)) as span:
            span.set_attribute("http.request.method", request.method)
            started = time.perf_counter()
            try:
                response = await call_next(request)
            except Exception as exc:
                # PERF-006: a failed request is still a latency sample, and
                # excluding it would make the p99 look better than it is.
                _record_http_duration(request, 500, time.perf_counter() - started)
                record_safe_exception(exc, event="API_REQUEST_FAILED")
                raise
            span.set_attribute("http.response.status_code", response.status_code)
            _record_http_duration(request, response.status_code, time.perf_counter() - started)
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
                    "details": [
                        {"loc": list(e["loc"]), "type": e["type"], "msg": e["msg"]}
                        for e in exc.errors()
                    ],
                }
            },
        )

    @app.exception_handler(QuotaExceededError)
    async def quota_error_handler(_: Request, exc: QuotaExceededError) -> JSONResponse:
        get_runtime_metrics().security_event("quota", "denied")
        return _security_error(
            429,
            "RESOURCE_QUOTA_EXCEEDED",
            str(exc),
            headers={"Retry-After": str(runtime_settings.quota_window_seconds)},
        )

    @app.exception_handler(InvalidCursorError)
    async def cursor_error_handler(_: Request, exc: InvalidCursorError) -> JSONResponse:
        return _security_error(422, "INVALID_CURSOR", str(exc))

    @app.exception_handler(Exception)
    async def internal_error_handler(_: Request, exc: Exception) -> JSONResponse:
        record_safe_exception(exc, event="API_UNHANDLED_ERROR")
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
        tenant_id = _required_client_id(request, client_id)

        try:
            policy_snapshot = build_policy_snapshot(
                payload.policy.model_dump(exclude_none=True),
                max_attempts=runtime_settings.retry_max_attempts,
                attempt_timeout_seconds=min(
                    runtime_settings.retry_attempt_timeout_seconds,
                    runtime_settings.execution_lease_seconds - 1,
                ),
                max_output_tokens=runtime_settings.provider_max_output_tokens,
                initial_backoff_seconds=runtime_settings.retry_base_delay_seconds,
                max_backoff_seconds=runtime_settings.retry_max_backoff_seconds,
                available_providers={"deterministic"}
                | ({"openai"} if runtime_settings.openai_api_key is not None else set()),
            )
            run, replayed = await _get_service(request).submit(
                client_id=tenant_id,
                principal_id=_principal_id(request, tenant_id),
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
        except QuotaExceededError:
            raise
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
                client_id=_required_client_id(request, client_id), run_id=run_id
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
        response: Response,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        cursor: Annotated[UUID | None, Query()] = None,
        client_id: Annotated[str | None, Header(alias="X-Client-Id")] = None,
    ) -> list[AttemptResponse]:
        try:
            attempts = await _get_service(request).get_attempts(
                client_id=_required_client_id(request, client_id),
                run_id=run_id,
                limit=limit,
                cursor=cursor,
            )
        except RunNotFoundError as exc:
            raise ApiProblem(
                status_code=status.HTTP_404_NOT_FOUND, code="RUN_NOT_FOUND", message=str(exc)
            ) from exc
        if len(attempts) == limit:
            response.headers["X-Next-Cursor"] = str(attempts[-1].id)
        return [_to_attempt_response(attempt) for attempt in attempts]

    @app.get(
        "/v1/runs/{run_id}/events",
        response_model=list[EventResponse],
        responses={404: {"model": ApiErrorResponse}},
    )
    async def get_events(
        run_id: UUID,
        request: Request,
        response: Response,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        cursor: Annotated[UUID | None, Query()] = None,
        client_id: Annotated[str | None, Header(alias="X-Client-Id")] = None,
    ) -> list[EventResponse]:
        try:
            events = await _get_service(request).get_events(
                client_id=_required_client_id(request, client_id),
                run_id=run_id,
                limit=limit,
                cursor=cursor,
            )
        except RunNotFoundError as exc:
            raise ApiProblem(
                status_code=status.HTTP_404_NOT_FOUND, code="RUN_NOT_FOUND", message=str(exc)
            ) from exc
        if len(events) == limit:
            response.headers["X-Next-Cursor"] = str(events[-1].id)
        return [_to_event_response(event) for event in events]

    @app.post(
        "/v1/runs/{run_id}/evaluations",
        response_model=EvaluationResponse,
        status_code=status.HTTP_202_ACCEPTED,
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
                client_id=_required_client_id(request, client_id),
                run_id=run_id,
                rules=payload.rules,
                principal_id=_principal_id(request, _required_client_id(request, client_id)),
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
        response: Response,
        limit: Annotated[int, Query(ge=1, le=100)] = 50,
        cursor: Annotated[UUID | None, Query()] = None,
        client_id: Annotated[str | None, Header(alias="X-Client-Id")] = None,
    ) -> list[EvaluationResponse]:
        try:
            evaluations = await _get_service(request).get_evaluations(
                client_id=_required_client_id(request, client_id),
                run_id=run_id,
                limit=limit,
                cursor=cursor,
            )
        except RunNotFoundError as exc:
            raise ApiProblem(
                status_code=status.HTTP_404_NOT_FOUND, code="RUN_NOT_FOUND", message=str(exc)
            ) from exc
        if len(evaluations) == limit:
            response.headers["X-Next-Cursor"] = str(evaluations[-1].id)
        return [_to_evaluation_response(evaluation) for evaluation in evaluations]

    @app.post("/v1/evaluation-regressions", status_code=202)
    async def run_evaluation_regression(
        payload: EvaluationRegressionRequest,
        request: Request,
        idempotency_key: Annotated[str | None, Header(alias="Idempotency-Key")] = None,
    ) -> dict[str, Any]:
        if runtime_settings.auth_mode != "api_key":
            raise ApiProblem(
                status_code=401,
                code="AUTHENTICATION_REQUIRED",
                message="Regression jobs require API-key mode.",
            )
        tenant = _required_client_id(request, None)
        if (
            len(payload.dataset.cases) > runtime_settings.regression_max_cases
            or 2 * len(payload.dataset.cases) > runtime_settings.regression_provider_call_budget
        ):
            raise ApiProblem(
                status_code=422,
                code="REGRESSION_BUDGET_EXCEEDED",
                message="Regression exceeds the server provider-call budget.",
            )
        if idempotency_key is None:
            raise ApiProblem(
                status_code=422,
                code="MISSING_IDEMPOTENCY_KEY",
                message="Idempotency-Key is required.",
            )
        try:
            RegressionDataset.from_mapping(payload.dataset.model_dump())
            _validate_regression_targets(
                runtime_settings,
                ProviderModelTarget(**payload.baseline.model_dump()),
                ProviderModelTarget(**payload.candidate.model_dump()),
            )
            job, replayed = await _get_service(request).submit_regression(
                client_id=tenant,
                principal_id=_principal_id(request, tenant),
                payload=payload.model_dump(),
                idempotency_key=_validate_idempotency_key(idempotency_key),
            )
        except IdempotencyConflictError as exc:
            raise ApiProblem(
                status_code=409, code="IDEMPOTENCY_KEY_REUSED", message=str(exc)
            ) from exc
        except QuotaExceededError:
            raise
        except ValueError as exc:
            raise ApiProblem(
                status_code=422, code="INVALID_REGRESSION_REQUEST", message=str(exc)
            ) from exc
        return {
            "job_id": str(job.id),
            "execution_status": job.execution_status,
            "replayed": replayed,
            "status_url": f"/v1/jobs/{job.id}",
        }

    @app.get("/v1/jobs/{job_id}")
    async def get_job(
        job_id: UUID,
        request: Request,
        client_id: Annotated[str | None, Header(alias="X-Client-Id")] = None,
    ) -> dict[str, Any]:
        try:
            job = await _get_service(request).get_run(
                client_id=_required_client_id(request, client_id), run_id=job_id
            )
            if job.work_kind not in {"evaluation", "regression"}:
                raise RunNotFoundError("Job was not found")
        except RunNotFoundError as exc:
            raise ApiProblem(status_code=404, code="JOB_NOT_FOUND", message=str(exc)) from exc
        return {
            "job_id": str(job.id),
            "execution_status": job.execution_status,
            "result": job.result_payload,
            "error_code": job.error_code,
        }

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
                client_id=_required_client_id(request, client_id),
                run_id=run_id,
                principal_id=_principal_id(request, _required_client_id(request, client_id)),
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

    app.add_middleware(
        BodyLimitMiddleware,
        max_bytes=runtime_settings.max_request_bytes,
        timeout_seconds=runtime_settings.request_body_timeout_seconds,
    )
    return app


def _principal_id(request: Request, tenant: str) -> str:
    principal = getattr(request.state, "principal_id", None)
    if isinstance(principal, str):
        return principal
    if request.app.state.settings.auth_mode == "disabled":
        import hashlib

        return "local-" + hashlib.sha256(tenant.encode()).hexdigest()
    raise RuntimeError("Missing authenticated principal")


def _required_client_id(request: Request, client_id: str | None) -> str:
    if request.app.state.settings.auth_mode == "api_key":
        tenant_id = request.state.tenant_id
        if not isinstance(tenant_id, str):
            raise RuntimeError("Missing authenticated tenant")
        return tenant_id
    if client_id is None or not client_id.strip():
        raise ApiProblem(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            code="MISSING_CLIENT_ID",
            message="X-Client-Id is required in local-development mode.",
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
