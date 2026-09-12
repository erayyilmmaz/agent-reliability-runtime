from __future__ import annotations

import json
import uuid
from collections.abc import Callable

import httpx
import pytest

from agent_runtime.client import AgentRuntimeApiError, AgentRuntimeClient

RUN_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")
REPLAY_ID = uuid.UUID("00000000-0000-0000-0000-000000000002")


def _client(handler: Callable[[httpx.Request], httpx.Response]) -> AgentRuntimeClient:
    return AgentRuntimeClient(
        base_url="https://runtime.example",
        client_id="sdk-test-client",
        api_key="sdk-test-key",
        http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )


def test_client_submit_read_history_replay_and_wait() -> None:
    requests: list[httpx.Request] = []
    statuses = iter(["QUEUED", "SUCCEEDED"])

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.method == "POST" and request.url.path == "/v1/runs":
            return httpx.Response(
                202,
                json={
                    "run_id": str(RUN_ID),
                    "execution_status": "QUEUED",
                    "replayed": False,
                    "routing_decision": {"selected_provider": "deterministic"},
                },
            )
        if request.method == "GET" and request.url.path == f"/v1/runs/{RUN_ID}":
            return httpx.Response(
                200,
                json={
                    "run_id": str(RUN_ID),
                    "execution_status": next(statuses),
                    "evaluation_status": "NOT_RUN",
                    "error_code": None,
                    "routing_decision": None,
                },
            )
        if request.method == "GET" and request.url.path.endswith("/attempts"):
            return httpx.Response(200, json=[{"attempt_number": 1, "provider": "deterministic"}])
        if request.method == "GET" and request.url.path.endswith("/events"):
            return httpx.Response(200, json=[{"event_type": "RUN_QUEUED"}])
        if request.method == "POST" and request.url.path.endswith("/replay"):
            return httpx.Response(
                202,
                json={
                    "run_id": str(REPLAY_ID),
                    "execution_status": "QUEUED",
                    "replay_of_run_id": str(RUN_ID),
                    "replayed": False,
                },
            )
        raise AssertionError(f"Unexpected request: {request.method} {request.url}")

    client = _client(handler)
    submitted = client.run({"prompt": "hello"}, idempotency_key="sdk-run-0001")
    completed = client.wait_for_terminal(RUN_ID, poll_seconds=0.001)
    attempts = client.get_attempts(RUN_ID)
    events = client.get_events(RUN_ID)
    replay = client.replay(RUN_ID, idempotency_key="sdk-replay-0001")

    assert submitted.run_id == RUN_ID
    assert submitted.routing_decision == {"selected_provider": "deterministic"}
    assert submitted.idempotency_key == "sdk-run-0001"
    assert completed.execution_status == "SUCCEEDED"
    assert attempts == [{"attempt_number": 1, "provider": "deterministic"}]
    assert events == [{"event_type": "RUN_QUEUED"}]
    assert replay.replay_of_run_id == RUN_ID
    assert replay.idempotency_key == "sdk-replay-0001"
    assert requests[0].headers["x-client-id"] == "sdk-test-client"
    assert requests[0].headers["x-api-key"] == "sdk-test-key"
    assert requests[0].headers["idempotency-key"] == "sdk-run-0001"
    assert json.loads(requests[0].content) == {"input": {"prompt": "hello"}, "policy": {}}


def test_client_raises_controlled_api_error() -> None:
    client = _client(
        lambda _: httpx.Response(
            409,
            json={"error": {"code": "IDEMPOTENCY_KEY_REUSED", "message": "key conflicts"}},
        )
    )

    with pytest.raises(AgentRuntimeApiError) as captured:
        client.run({"prompt": "hello"}, idempotency_key="sdk-run-0001")

    assert captured.value.status_code == 409
    assert captured.value.code == "IDEMPOTENCY_KEY_REUSED"


def test_client_exposes_history_cursor_and_job_result() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/v1/jobs/"):
            return httpx.Response(
                200, json={"execution_status": "SUCCEEDED", "result": {"passed": True}}
            )
        assert request.url.params["limit"] == "10"
        assert request.url.params["cursor"] == str(RUN_ID)
        return httpx.Response(
            200, json=[{"event_id": str(REPLAY_ID)}], headers={"X-Next-Cursor": str(REPLAY_ID)}
        )

    client = _client(handler)
    page = client.get_history_page(RUN_ID, resource="events", limit=10, cursor=RUN_ID)
    assert page.items == [{"event_id": str(REPLAY_ID)}] and page.next_cursor == str(REPLAY_ID)
    assert client.get_job(RUN_ID)["result"]["passed"]
    with pytest.raises(ValueError):
        client.get_history_page(RUN_ID, resource="events", limit=101)
