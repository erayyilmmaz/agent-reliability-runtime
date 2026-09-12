from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class RoutingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    strategy: Literal["balanced", "lowest_latency", "lowest_cost", "quality_first"] = "balanced"
    candidates: list[Literal["deterministic", "openai"]] | None = Field(
        default=None, min_length=1, max_length=2
    )


class RunPolicyRequest(BaseModel):
    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)
    max_attempts: int | None = Field(default=None, ge=1, le=20, strict=True)
    attempt_timeout_seconds: float | None = Field(default=None, ge=1, le=600, strict=True)
    initial_backoff_seconds: float | None = Field(default=None, ge=1, le=3600, strict=True)
    max_backoff_seconds: float | None = Field(default=None, ge=1, le=86400, strict=True)
    provider_order: list[Literal["deterministic", "openai"]] | None = Field(
        default=None, min_length=1, max_length=2
    )
    routing: RoutingRequest | None = None
    model: str | None = Field(default=None, min_length=1, max_length=128)
    instructions: str | None = Field(default=None, min_length=1, max_length=4096)
    max_output_tokens: int | None = Field(default=None, ge=1, le=8192, strict=True)
