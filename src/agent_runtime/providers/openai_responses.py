from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import httpx
from pydantic import SecretStr

from agent_runtime.application.execution import ExecutionResult
from agent_runtime.providers.contracts import ProviderConfigurationError, ProviderHttpError


class OpenAIResponsesProvider:
    """Small, explicit adapter for OpenAI's Responses HTTP API."""

    name = "openai"

    def __init__(
        self,
        *,
        api_key: SecretStr | None,
        base_url: str,
        default_model: str,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self._base_url = base_url.rstrip("/")
        self._default_model = default_model
        self._transport = transport

    async def execute(
        self, *, input_payload: dict[str, Any], policy_snapshot: dict[str, Any]
    ) -> ExecutionResult:
        if self._api_key is None:
            raise ProviderConfigurationError("OpenAI API key is not configured")
        body = self._request_body(input_payload, policy_snapshot)
        headers = {
            "Authorization": f"Bearer {self._api_key.get_secret_value()}",
            "Content-Type": "application/json",
        }
        async with httpx.AsyncClient(transport=self._transport) as client:
            try:
                response = await client.post(
                    f"{self._base_url}/responses", headers=headers, json=body
                )
            except httpx.RequestError as exc:
                raise RuntimeError("OpenAI request did not receive a response") from exc
        if response.is_error:
            raise ProviderHttpError(response.status_code)
        payload = response.json()
        if not isinstance(payload, Mapping):
            raise RuntimeError("OpenAI returned a non-object response")
        return ExecutionResult(
            provider=self.name,
            result_payload={
                "provider_response_id": _string_or_none(payload.get("id")),
                "model": _string_or_none(payload.get("model")),
                "output_text": _output_text(payload),
            },
            usage_metadata=_usage_metadata(payload.get("usage")),
        )

    def _request_body(
        self, input_payload: Mapping[str, Any], policy_snapshot: Mapping[str, Any]
    ) -> dict[str, Any]:
        model = policy_snapshot.get("model", self._default_model)
        if not isinstance(model, str) or not model.strip():
            raise ProviderConfigurationError("policy.model must be a non-empty string")

        prompt = input_payload.get("prompt")
        messages = input_payload.get("messages")
        if isinstance(prompt, str) and prompt.strip():
            provider_input: str | list[Any] = prompt
        elif isinstance(messages, list) and messages:
            provider_input = messages
        else:
            raise ProviderConfigurationError(
                "OpenAI input requires a non-empty 'prompt' string or 'messages' list"
            )

        body: dict[str, Any] = {"model": model, "input": provider_input, "store": False}
        instructions = policy_snapshot.get("instructions")
        if instructions is not None:
            if not isinstance(instructions, str) or not instructions.strip():
                raise ProviderConfigurationError("policy.instructions must be a non-empty string")
            body["instructions"] = instructions
        max_output_tokens = policy_snapshot.get("max_output_tokens")
        if max_output_tokens is not None:
            if isinstance(max_output_tokens, bool) or not isinstance(max_output_tokens, int):
                raise ProviderConfigurationError("policy.max_output_tokens must be an integer")
            body["max_output_tokens"] = max_output_tokens
        return body


def _output_text(payload: Mapping[str, Any]) -> str:
    direct_text = payload.get("output_text")
    if isinstance(direct_text, str):
        return direct_text
    output = payload.get("output")
    if not isinstance(output, list):
        return ""
    texts: list[str] = []
    for item in output:
        if not isinstance(item, Mapping):
            continue
        content = item.get("content")
        if not isinstance(content, list):
            continue
        for part in content:
            if isinstance(part, Mapping) and part.get("type") == "output_text":
                text = part.get("text")
                if isinstance(text, str):
                    texts.append(text)
    return "".join(texts)


def _usage_metadata(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    usage = {
        key: usage_value
        for key in ("input_tokens", "output_tokens", "total_tokens")
        if isinstance((usage_value := value.get(key)), int)
    }
    return usage or None


def _string_or_none(value: Any) -> str | None:
    return value if isinstance(value, str) else None
