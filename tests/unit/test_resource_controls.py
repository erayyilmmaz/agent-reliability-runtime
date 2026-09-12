from __future__ import annotations

import asyncio

import httpx
import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from agent_runtime.api.body_limit import BodyLimitMiddleware
from agent_runtime.api.main import create_app
from agent_runtime.domain.policy import RunPolicyRequest
from agent_runtime.domain.retry import RetryPolicy, build_policy_snapshot
from agent_runtime.evaluation.cli import _provider_registry
from agent_runtime.evaluation.safety import (
    EvaluationConfigurationError,
    bounded_value,
    validate_rules,
)
from agent_runtime.settings import Settings


@pytest.mark.parametrize(
    "rule",
    [
        {"type": "rule", "operator": "matches", "value": "(a+)+$"},
        {"type": "rule", "operator": []},
        {"type": "rule", "operator": {}},
        *[
            {"type": "json_schema", "schema": schema}
            for schema in (
                {"$ref": "https://untrusted.invalid/schema"},
                {"$ref": "#"},
                {"pattern": "(a+)+$"},
                {"patternProperties": {"(a+)+$": {}}},
                {"oneOf": [{}] * 100},
                {"allOf": [{}] * 100},
                {"uniqueItems": True},
                {"properties": {f"key-{i}": {} for i in range(33)}},
            )
        ],
    ],
)
def test_unsafe_rules_are_rejected_before_evaluation(rule) -> None:
    with pytest.raises(EvaluationConfigurationError):
        validate_rules([rule])
    app = create_app(Settings(environment="local", auth_mode="disabled"), run_service=object())
    with TestClient(app) as client:
        result = client.post(
            "/v1/runs/00000000-0000-0000-0000-000000000001/evaluations",
            headers={"X-Client-Id": "test"},
            json={"rules": [rule]},
        )
    assert result.status_code == 422


def test_schema_depth_and_instance_size_are_bounded() -> None:
    schema = {"type": "string"}
    for _ in range(8):
        schema = {"properties": {"child": schema}}
    with pytest.raises(EvaluationConfigurationError):
        validate_rules([{"type": "json_schema", "schema": schema}])
    for value in ([0] * 10001, {"text": "a" * 65537}, {"text": "\ud800"}, {"\ud800": 1}):
        with pytest.raises(EvaluationConfigurationError):
            bounded_value(value)


@pytest.mark.parametrize(
    "policy",
    [
        {"untrusted": 1},
        {"routing": {"decision": {}, "candidates": ["openai"]}},
        {"max_attempts": 1000000},
        {"max_attempts": True},
        {"max_attempts": "3"},
        {"attempt_timeout_seconds": 0.001},
        {"initial_backoff_seconds": 0.000001},
        {"max_backoff_seconds": float("inf")},
        {"attempt_timeout_seconds": float("nan")},
        {"max_output_tokens": 10000000},
        {"provider_order": ["unknown"]},
    ],
)
def test_policy_fields_and_numbers_are_allowlisted(policy) -> None:
    with pytest.raises(ValidationError):
        RunPolicyRequest.model_validate(policy)


def test_server_policy_caps_and_retry_floor() -> None:
    policy = build_policy_snapshot(
        {
            "max_attempts": 20,
            "attempt_timeout_seconds": 600,
            "max_backoff_seconds": 86400,
            "initial_backoff_seconds": 1,
            "max_output_tokens": 8192,
        },
        max_attempts=3,
        attempt_timeout_seconds=10,
        initial_backoff_seconds=2,
        max_backoff_seconds=60,
        max_output_tokens=1000,
    )
    assert (
        policy["max_attempts"],
        policy["attempt_timeout_seconds"],
        policy["initial_backoff_seconds"],
        policy["max_backoff_seconds"],
        policy["max_output_tokens"],
    ) == (3, 10, 2, 60, 1000)
    legacy = RetryPolicy(3, 1, 0.000001, 0.00001, ("deterministic",))
    assert legacy.delay_after_attempt(1) == 1
    assert legacy.delay_after_attempt(10000000) == 1


@pytest.mark.parametrize(
    "headers,chunks,expected",
    [
        ([], [b"a" * 32, b"b" * 33, b"unread"], 413),
        ([(b"transfer-encoding", b"chunked")], [b"a" * 65], 413),
        ([(b"content-length", b"10")], [b"a" * 65], 413),
        ([(b"content-length", b"10")], [b"a" * 11], 400),
        ([(b"content-length", b"bad")], [], 400),
        ([(b"content-length", b"-1")], [], 400),
        ([(b"content-length", b"1"), (b"content-length", b"1")], [], 400),
        ([(b"content-length", b"1"), (b"transfer-encoding", b"chunked")], [], 400),
        ([], [b"a" * 32, b"b" * 32], 200),
    ],
)
async def test_asgi_stream_limits_actual_bytes_before_handler(headers, chunks, expected) -> None:
    consumed = 0
    invoked = False
    messages = []

    async def receive():
        nonlocal consumed
        consumed += 1
        return {
            "type": "http.request",
            "body": chunks[consumed - 1],
            "more_body": consumed < len(chunks),
        }

    async def send(message):
        messages.append(message)

    async def handler(scope, receive, send):
        nonlocal invoked
        invoked = True
        assert len((await receive())["body"]) == 64
        await send({"type": "http.response.start", "status": 200, "headers": []})

    await BodyLimitMiddleware(handler, max_bytes=64, timeout_seconds=1)(
        {"type": "http", "headers": headers}, receive, send
    )
    assert messages[0]["status"] == expected
    assert invoked == (expected == 200)
    if chunks and chunks[-1] == b"unread":
        assert consumed == 2


async def test_stream_read_deadline_is_fail_closed() -> None:
    messages = []

    async def receive():
        await asyncio.sleep(2)
        return {"type": "http.request", "body": b"", "more_body": False}

    async def handler(*args):
        pytest.fail("handler must not run")

    async def send(message):
        messages.append(message)

    await BodyLimitMiddleware(handler, max_bytes=64, timeout_seconds=0.01)(
        {"type": "http", "headers": []}, receive, send
    )
    assert messages[0]["status"] == 408


async def test_provider_timeout_setting_reaches_http_client() -> None:
    settings = Settings(
        environment="local",
        auth_mode="disabled",
        provider_timeout_seconds=17,
        openai_api_key="disposable-test-key",
    )
    registry = _provider_registry(settings)

    def handler(request: httpx.Request) -> httpx.Response:
        assert request.extensions["timeout"] == {"connect": 17, "read": 17, "write": 17, "pool": 17}
        return httpx.Response(200, json={"output_text": "ready"})

    registry._adapters["openai"]._transport = httpx.MockTransport(handler)
    result = await registry.execute(
        provider="openai", input_payload={"prompt": "test"}, policy_snapshot={}
    )
    assert result.result_payload["output_text"] == "ready"
