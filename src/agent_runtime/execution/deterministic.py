from __future__ import annotations

from typing import Any

from agent_runtime.application.execution import ExecutionResult


class DeterministicExecutor:
    """Safe V0 placeholder used until ARR-8 provider adapters are introduced."""

    async def execute(
        self, *, input_payload: dict[str, Any], policy_snapshot: dict[str, Any]
    ) -> ExecutionResult:
        return ExecutionResult(
            provider="deterministic",
            result_payload={"accepted_input": input_payload, "policy_applied": policy_snapshot},
        )
