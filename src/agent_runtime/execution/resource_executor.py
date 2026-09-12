"""Worker-only evaluation jobs and authoritative provider-call charging."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_runtime.application.execution import ExecutionResult
from agent_runtime.application.runs import QuotaExceededError
from agent_runtime.evaluation.engine import evaluate_rules
from agent_runtime.evaluation.regression import (
    EvaluationRegressionRunner,
    ProviderModelTarget,
    RegressionDataset,
    RegressionGates,
)
from agent_runtime.infrastructure.database.models import Run, RunAttempt
from agent_runtime.infrastructure.database.quotas import (
    QuotaLimits,
    charge_provider_call,
    lock_identity,
)
from agent_runtime.providers.registry import ProviderRegistry
from agent_runtime.settings import Settings


class ResourceExecutor:
    def __init__(
        self,
        registry: ProviderRegistry,
        sessions: async_sessionmaker[AsyncSession],
        settings: Settings,
    ) -> None:
        self.registry, self.sessions, self.settings = registry, sessions, settings
        self.quotas = QuotaLimits.from_settings(settings)

    async def execute(
        self, *, provider: str, input_payload: dict[str, Any], policy_snapshot: dict[str, Any]
    ) -> ExecutionResult:
        run_id = UUID(policy_snapshot["_runtime_run_id"])
        worker_id = policy_snapshot["_runtime_worker_id"]
        attempt_id = UUID(policy_snapshot["_runtime_attempt_id"])
        async with self.sessions() as session:
            run = await session.get(Run, run_id)
            if run is None:
                raise RuntimeError("Missing durable execution")
            work_kind = run.work_kind
            principal_id = run.principal_id or run.client_id
            tenant_id = run.client_id
        if work_kind == "evaluation":
            async with self.sessions() as session:
                source = await session.get(Run, UUID(input_payload["source_run_id"]))
                if source is None or source.client_id != tenant_id or source.result_payload is None:
                    raise RuntimeError("Evaluation source is unavailable")
                latency = await session.scalar(
                    select(RunAttempt.latency_ms)
                    .where(RunAttempt.run_id == source.id, RunAttempt.outcome == "SUCCEEDED")
                    .order_by(RunAttempt.attempt_number.desc())
                    .limit(1)
                )
                result_payload = source.result_payload
            outcome = await asyncio.to_thread(
                evaluate_rules,
                rules=input_payload["rules"],
                result_payload=result_payload,
                latency_ms=latency,
            )
            return ExecutionResult(
                provider="evaluation", result_payload=outcome.as_persisted_result()
            )

        budget = self.settings.regression_provider_call_budget if work_kind == "regression" else 1
        parent = self

        class ChargedExecutor:
            calls = 0

            async def execute(
                self,
                *,
                provider: str,
                input_payload: dict[str, Any],
                policy_snapshot: dict[str, Any],
            ) -> ExecutionResult:
                if self.calls >= budget:
                    raise QuotaExceededError("Job provider-call budget exceeded")
                async with parent.sessions() as session:
                    async with session.begin():
                        await lock_identity(session, principal_id, tenant_id)
                        active = await session.get(Run, run_id, with_for_update=True)
                        attempt = await session.get(RunAttempt, attempt_id)
                        if (
                            attempt is None
                            or attempt.run_id != run_id
                            or attempt.finished_at is not None
                        ):
                            raise RuntimeError("Execution attempt is no longer active")
                        if (
                            active is None
                            or active.execution_status != "RUNNING"
                            or active.execution_lease_owner != worker_id
                            or active.execution_lease_expires_at is None
                            or active.execution_lease_expires_at <= datetime.now(UTC)
                        ):
                            raise RuntimeError("Execution lease is no longer active")
                        await charge_provider_call(
                            session,
                            active.principal_id or active.client_id,
                            active.client_id,
                            parent.quotas,
                        )
                self.calls += 1
                tokens = min(
                    policy_snapshot.get(
                        "max_output_tokens", parent.settings.provider_max_output_tokens
                    ),
                    parent.settings.provider_max_output_tokens,
                )
                return await asyncio.wait_for(
                    parent.registry.execute(
                        provider=provider,
                        input_payload=input_payload,
                        policy_snapshot={**policy_snapshot, "max_output_tokens": tokens},
                    ),
                    timeout=parent.settings.provider_timeout_seconds,
                )

        charged = ChargedExecutor()
        if work_kind == "regression":
            dataset = RegressionDataset.from_mapping(input_payload["dataset"])
            if (
                len(dataset.cases) > self.settings.regression_max_cases
                or 2 * len(dataset.cases) > budget
            ):
                raise QuotaExceededError("Regression exceeds the server budget")
            report = await EvaluationRegressionRunner(charged).run(
                dataset=dataset,
                baseline=ProviderModelTarget(**input_payload["baseline"]),
                candidate=ProviderModelTarget(**input_payload["candidate"]),
                gates=RegressionGates(**input_payload.get("gates", {})),
            )
            return ExecutionResult(provider="regression", result_payload=report)
        return await charged.execute(
            provider=provider, input_payload=input_payload, policy_snapshot=policy_snapshot
        )
