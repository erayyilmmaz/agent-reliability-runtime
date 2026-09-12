from __future__ import annotations

from typing import Any

from agent_runtime.application.execution import ExecutionResult


class DeterministicProvider:
    """Local no-network adapter retained for development and deterministic tests."""

    name = "deterministic"

    def __init__(self, *, echo_input: bool = False) -> None:
        self._echo_input = echo_input

    async def execute(
        self, *, input_payload: dict[str, Any], policy_snapshot: dict[str, Any]
    ) -> ExecutionResult:
        return ExecutionResult(
            provider=self.name,
            result_payload=(
                {"accepted": True, "accepted_input": input_payload}
                if self._echo_input
                else {"accepted": True}
            ),
        )
