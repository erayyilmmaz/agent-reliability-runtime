"""Small synchronous Python client for the Agent Reliability Runtime API."""

from __future__ import annotations

import time
import uuid
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Literal
from urllib.parse import urlencode

import httpx

TERMINAL_STATUSES = frozenset({"SUCCEEDED", "FAILED", "DEAD_LETTERED"})


class AgentRuntimeClientError(RuntimeError):
    """Base exception raised by the client SDK."""


class AgentRuntimeTransportError(AgentRuntimeClientError):
    """The API could not be reached or did not return a valid response."""


class AgentRuntimeApiError(AgentRuntimeClientError):
    """The API returned a controlled non-success response."""

    def __init__(self, *, status_code: int, code: str, message: str) -> None:
        super().__init__(f"{code} ({status_code}): {message}")
        self.status_code = status_code
        self.code = code
        self.message = message


@dataclass(frozen=True)
class SubmittedRun:
    run_id: uuid.UUID
    execution_status: str
    replayed: bool
    idempotency_key: str
    routing_decision: dict[str, Any] | None


@dataclass(frozen=True)
class RunDetails:
    run_id: uuid.UUID
    execution_status: str
    evaluation_status: str
    error_code: str | None
    routing_decision: dict[str, Any] | None
    raw: dict[str, Any]

    @property
    def is_terminal(self) -> bool:
        return self.execution_status in TERMINAL_STATUSES


@dataclass(frozen=True)
class ReplayRun:
    run_id: uuid.UUID
    execution_status: str
    replay_of_run_id: uuid.UUID
    replayed: bool
    idempotency_key: str


@dataclass(frozen=True)
class HistoryPage:
    items: list[dict[str, Any]]
    next_cursor: str | None


class AgentRuntimeClient:
    """Typed convenience wrapper for submit, read, replay, and history APIs.

    `run()` generates an idempotency key when one is not supplied. Persist the
    returned key if the caller needs to retry the exact same submission after a
    transport error.
    """

    def __init__(
        self,
        *,
        base_url: str = "http://localhost:8000",
        client_id: str,
        api_key: str | None = None,
        timeout_seconds: float = 10.0,
        http_client: httpx.Client | None = None,
    ) -> None:
        if not client_id.strip():
            raise ValueError("client_id must be a non-empty string")
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self._base_url = base_url.rstrip("/")
        self._client_id = client_id
        self._api_key = api_key
        self._http_client = http_client or httpx.Client(timeout=timeout_seconds)
        self._owns_http_client = http_client is None

    def __enter__(self) -> AgentRuntimeClient:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def close(self) -> None:
        if self._owns_http_client:
            self._http_client.close()

    def healthz(self) -> dict[str, str]:
        payload = self._request("GET", "/healthz")
        return {key: value for key, value in payload.items() if isinstance(value, str)}

    def run(
        self,
        input_payload: Mapping[str, Any],
        *,
        policy: Mapping[str, Any] | None = None,
        idempotency_key: str | None = None,
    ) -> SubmittedRun:
        key = idempotency_key or f"sdk-{uuid.uuid4().hex}"
        payload = self._request(
            "POST",
            "/v1/runs",
            headers={"Idempotency-Key": key},
            json={"input": dict(input_payload), "policy": dict(policy or {})},
        )
        return SubmittedRun(
            run_id=_uuid(payload, "run_id"),
            execution_status=_string(payload, "execution_status"),
            replayed=_boolean(payload, "replayed"),
            idempotency_key=key,
            routing_decision=_optional_object(payload, "routing_decision"),
        )

    def get_run(self, run_id: uuid.UUID | str) -> RunDetails:
        payload = self._request("GET", f"/v1/runs/{run_id}")
        return RunDetails(
            run_id=_uuid(payload, "run_id"),
            execution_status=_string(payload, "execution_status"),
            evaluation_status=_string(payload, "evaluation_status"),
            error_code=_optional_string(payload, "error_code"),
            routing_decision=_optional_object(payload, "routing_decision"),
            raw=payload,
        )

    def get_attempts(self, run_id: uuid.UUID | str) -> list[dict[str, Any]]:
        """Return the first 50 attempts; use get_history_page for continuation."""
        payload = self._request_list("GET", f"/v1/runs/{run_id}/attempts")
        return [dict(item) for item in payload]

    def get_events(self, run_id: uuid.UUID | str) -> list[dict[str, Any]]:
        """Return the first 50 events; use get_history_page for continuation."""
        payload = self._request_list("GET", f"/v1/runs/{run_id}/events")
        return [dict(item) for item in payload]

    def get_history_page(
        self,
        run_id: uuid.UUID | str,
        *,
        resource: Literal["attempts", "events", "evaluations"],
        limit: int = 50,
        cursor: uuid.UUID | str | None = None,
    ) -> HistoryPage:
        if resource not in {"attempts", "events", "evaluations"}:
            raise ValueError("Unsupported history resource")
        if not 1 <= limit <= 100:
            raise ValueError("limit must be between 1 and 100")
        params = {"limit": str(limit)}
        if cursor is not None:
            params["cursor"] = str(uuid.UUID(str(cursor)))
        response = self._send("GET", f"/v1/runs/{run_id}/{resource}?{urlencode(params)}")
        try:
            payload = response.json()
        except ValueError as exc:
            raise AgentRuntimeTransportError("API response was not JSON") from exc
        if not isinstance(payload, list) or not all(isinstance(item, dict) for item in payload):
            raise AgentRuntimeTransportError("API response was not an object list")
        return HistoryPage(items=payload, next_cursor=response.headers.get("X-Next-Cursor"))

    def get_job(self, job_id: uuid.UUID | str) -> dict[str, Any]:
        """Read a durable evaluation/regression job, including its terminal result."""
        return self._request("GET", f"/v1/jobs/{job_id}")

    def replay(self, run_id: uuid.UUID | str, *, idempotency_key: str | None = None) -> ReplayRun:
        key = idempotency_key or f"sdk-replay-{uuid.uuid4().hex}"
        payload = self._request(
            "POST", f"/v1/runs/{run_id}/replay", headers={"Idempotency-Key": key}
        )
        return ReplayRun(
            run_id=_uuid(payload, "run_id"),
            execution_status=_string(payload, "execution_status"),
            replay_of_run_id=_uuid(payload, "replay_of_run_id"),
            replayed=_boolean(payload, "replayed"),
            idempotency_key=key,
        )

    def wait_for_terminal(
        self, run_id: uuid.UUID | str, *, timeout_seconds: float = 30.0, poll_seconds: float = 0.5
    ) -> RunDetails:
        if timeout_seconds <= 0 or poll_seconds <= 0:
            raise ValueError("timeout_seconds and poll_seconds must be positive")
        deadline = time.monotonic() + timeout_seconds
        while True:
            run = self.get_run(run_id)
            if run.is_terminal:
                return run
            if time.monotonic() >= deadline:
                raise TimeoutError(
                    f"Run {run_id} did not reach a terminal state in {timeout_seconds}s"
                )
            time.sleep(min(poll_seconds, max(0, deadline - time.monotonic())))

    def _request(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str] | None = None,
        json: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        response = self._send(method, path, headers=headers, json=json)
        try:
            payload = response.json()
        except ValueError as exc:
            raise AgentRuntimeTransportError("API response was not JSON") from exc
        if not isinstance(payload, dict):
            raise AgentRuntimeTransportError("API response was not an object")
        return payload

    def _request_list(self, method: str, path: str) -> list[Mapping[str, Any]]:
        response = self._send(method, path)
        try:
            payload = response.json()
        except ValueError as exc:
            raise AgentRuntimeTransportError("API response was not JSON") from exc
        if not isinstance(payload, list) or not all(isinstance(item, Mapping) for item in payload):
            raise AgentRuntimeTransportError("API response was not an object list")
        return payload

    def _send(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str] | None = None,
        json: dict[str, Any] | None = None,
    ) -> httpx.Response:
        request_headers = {"X-Client-Id": self._client_id}
        if self._api_key is not None:
            request_headers["X-API-Key"] = self._api_key
        request_headers.update(headers or {})
        try:
            response = self._http_client.request(
                method, f"{self._base_url}{path}", headers=request_headers, json=json
            )
        except httpx.HTTPError as exc:
            raise AgentRuntimeTransportError("API request could not be completed") from exc
        if response.is_error:
            raise _api_error(response)
        return response


def _api_error(response: httpx.Response) -> AgentRuntimeApiError:
    code = "HTTP_ERROR"
    message = "API request failed"
    try:
        payload = response.json()
    except ValueError:
        payload = None
    if isinstance(payload, Mapping):
        error = payload.get("error")
        if isinstance(error, Mapping):
            if isinstance(error.get("code"), str):
                code = error["code"]
            if isinstance(error.get("message"), str):
                message = error["message"]
    return AgentRuntimeApiError(status_code=response.status_code, code=code, message=message)


def _uuid(payload: Mapping[str, Any], field: str) -> uuid.UUID:
    value = _string(payload, field)
    try:
        return uuid.UUID(value)
    except ValueError as exc:
        raise AgentRuntimeTransportError(f"API response had an invalid {field}") from exc


def _string(payload: Mapping[str, Any], field: str) -> str:
    value = payload.get(field)
    if not isinstance(value, str):
        raise AgentRuntimeTransportError(f"API response was missing string {field}")
    return value


def _boolean(payload: Mapping[str, Any], field: str) -> bool:
    value = payload.get(field)
    if not isinstance(value, bool):
        raise AgentRuntimeTransportError(f"API response was missing boolean {field}")
    return value


def _optional_string(payload: Mapping[str, Any], field: str) -> str | None:
    value = payload.get(field)
    if value is None:
        return None
    if not isinstance(value, str):
        raise AgentRuntimeTransportError(f"API response had invalid {field}")
    return value


def _optional_object(payload: Mapping[str, Any], field: str) -> dict[str, Any] | None:
    value = payload.get(field)
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise AgentRuntimeTransportError(f"API response had invalid {field}")
    return dict(value)
