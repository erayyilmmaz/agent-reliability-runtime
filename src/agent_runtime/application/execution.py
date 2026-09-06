from __future__ import annotations

import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import Any


class ClaimDecision(StrEnum):
    CLAIMED = "CLAIMED"
    MISSING = "MISSING"
    TERMINAL = "TERMINAL"
    ACTIVE_LEASE = "ACTIVE_LEASE"
    NOT_READY = "NOT_READY"


@dataclass(frozen=True)
class ClaimResult:
    decision: ClaimDecision
    run_id: uuid.UUID
    attempt_id: uuid.UUID | None = None
    input_payload: dict[str, Any] | None = None
    policy_snapshot: dict[str, Any] | None = None


@dataclass(frozen=True)
class ExecutionResult:
    provider: str
    result_payload: dict[str, Any]
