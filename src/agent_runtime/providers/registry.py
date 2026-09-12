from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from agent_runtime.application.execution import ExecutionResult
from agent_runtime.observability.telemetry import safe_span
from agent_runtime.providers.contracts import ProviderAdapter, ProviderConfigurationError


class ProviderRegistry:
    """Resolves the provider selected by the immutable run policy."""

    def __init__(self, adapters: Iterable[ProviderAdapter]) -> None:
        self._adapters = {adapter.name: adapter for adapter in adapters}
        if not self._adapters:
            raise ValueError("At least one provider adapter is required")

    async def execute(
        self,
        *,
        provider: str,
        input_payload: dict[str, Any],
        policy_snapshot: dict[str, Any],
    ) -> ExecutionResult:
        adapter = self._adapters.get(provider)
        if adapter is None:
            raise ProviderConfigurationError(f"Provider '{provider}' is not configured")
        with safe_span("arr.provider.execute") as span:
            span.set_attribute("arr.provider", provider)
            return await adapter.execute(
                input_payload=input_payload, policy_snapshot=policy_snapshot
            )
