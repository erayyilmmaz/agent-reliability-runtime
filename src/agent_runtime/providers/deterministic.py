from __future__ import annotations

from typing import Any

from agent_runtime.application.execution import ExecutionResult


class DeterministicProvider:
    """Local no-network adapter retained for development and deterministic tests."""

    name = "deterministic"

    async def execute(
        self, *, input_payload: dict[str, Any], policy_snapshot: dict[str, Any]
    ) -> ExecutionResult:
        return ExecutionResult(
            provider=self.name,
            result_payload={"accepted_input": input_payload, "policy_applied": policy_snapshot},
        )
