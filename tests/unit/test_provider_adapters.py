from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from agent_runtime.application.execution import ExecutionResult
from agent_runtime.domain.retry import ExecutionErrorCode, classify_exception
from agent_runtime.providers.contracts import ProviderConfigurationError, ProviderHttpError
from agent_runtime.providers.openai_responses import OpenAIResponsesProvider
from agent_runtime.providers.registry import ProviderRegistry


def _provider(*, handler: Callable[[httpx.Request], httpx.Response]) -> OpenAIResponsesProvider:
    return OpenAIResponsesProvider(
        api_key=SecretStr("test-key"),
        base_url="https://provider.example/v1",
        default_model="gpt-5",
        transport=httpx.MockTransport(handler),
    )


@pytest.mark.asyncio
async def test_openai_adapter_posts_minimal_private_responses_request() -> None:
    captured: dict[str, Any] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["authorization"] = request.headers["authorization"]
        captured["body"] = json.loads(request.content)
        return httpx.Response(
            200,
            json={
                "id": "resp_123",
                "model": "gpt-5",
                "output": [
                    {
                        "type": "message",
                        "content": [{"type": "output_text", "text": "Done."}],
                    }
                ],
                "usage": {"input_tokens": 4, "output_tokens": 2, "total_tokens": 6},
            },
        )

    result = await _provider(handler=handler).execute(
        input_payload={"prompt": "Say done"},
        policy_snapshot={"instructions": "Be concise", "max_output_tokens": 10},
    )

    assert captured == {
        "authorization": "Bearer test-key",
        "body": {
            "model": "gpt-5",
            "input": "Say done",
            "store": False,
            "instructions": "Be concise",
            "max_output_tokens": 10,
        },
    }
    assert result == ExecutionResult(
        provider="openai",
        result_payload={
            "provider_response_id": "resp_123",
            "model": "gpt-5",
            "output_text": "Done.",
        },
        usage_metadata={"input_tokens": 4, "output_tokens": 2, "total_tokens": 6},
    )


@pytest.mark.asyncio
async def test_openai_http_failure_is_sanitized_and_retry_classifiable() -> None:
    def handler(_: httpx.Request) -> httpx.Response:
        return httpx.Response(429, json={"error": {"message": "secret response"}})

    with pytest.raises(ProviderHttpError) as captured:
        await _provider(handler=handler).execute(
            input_payload={"prompt": "hello"}, policy_snapshot={}
        )

    assert str(captured.value) == "Provider returned HTTP 429"
    assert classify_exception(captured.value) == ExecutionErrorCode.PROVIDER_RATE_LIMITED


@pytest.mark.asyncio
async def test_openai_adapter_requires_explicit_provider_input() -> None:
    with pytest.raises(ProviderConfigurationError, match="prompt"):
        await _provider(handler=lambda _: httpx.Response(200, json={})).execute(
            input_payload={"unknown": "value"}, policy_snapshot={}
        )


class _FakeProvider:
    name = "fake"

    async def execute(self, **_: object) -> ExecutionResult:
        return ExecutionResult(provider=self.name, result_payload={"ok": True})


@pytest.mark.asyncio
async def test_registry_resolves_selected_provider_and_rejects_unknown_provider() -> None:
    registry = ProviderRegistry([_FakeProvider()])

    result = await registry.execute(provider="fake", input_payload={}, policy_snapshot={})
    assert result == ExecutionResult(
        provider="fake", result_payload={"ok": True}
    )
    with pytest.raises(ProviderConfigurationError, match="not configured"):
        await registry.execute(provider="unknown", input_payload={}, policy_snapshot={})
