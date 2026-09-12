from __future__ import annotations

import time
from collections.abc import Iterable
from typing import Any

from agent_runtime.application.execution import ExecutionResult
from agent_runtime.observability.metrics import get_runtime_metrics
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
            started = time.perf_counter()
            try:
                return await adapter.execute(
                    input_payload=input_payload, policy_snapshot=policy_snapshot
                )
            finally:
                # PERF-006: a failed call is still a latency sample. Timeouts
                # are exactly what this histogram exists to make visible, so
                # excluding them would hide the case that matters most.
                get_runtime_metrics().provider_call_duration(
                    provider, time.perf_counter() - started
                )
