from __future__ import annotations

from typing import Any, Protocol

from agent_runtime.application.execution import ExecutionResult


class ProviderAdapter(Protocol):
    """A provider implementation with no database or message-broker responsibility."""

    name: str

    async def execute(
        self, *, input_payload: dict[str, Any], policy_snapshot: dict[str, Any]
    ) -> ExecutionResult: ...


class ProviderConfigurationError(RuntimeError):
    """A non-retryable local provider configuration or input error."""

    status_code = 400


class ProviderHttpError(RuntimeError):
    """A sanitized provider HTTP failure; response bodies and secrets are never retained."""

    def __init__(self, status_code: int) -> None:
        super().__init__(f"Provider returned HTTP {status_code}")
        self.status_code = status_code
