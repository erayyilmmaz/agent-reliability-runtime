from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping
from typing import Any, Protocol
from uuid import UUID

import aio_pika
from aio_pika.abc import AbstractConnection, AbstractIncomingMessage

from agent_runtime.application.execution import ClaimDecision, ClaimResult, ExecutionResult
from agent_runtime.domain.retry import RetryPolicy, classify_exception
from agent_runtime.infrastructure.database.execution_service import ExecutionPersistenceService
from agent_runtime.infrastructure.messaging.publisher import (
    DEAD_LETTER_EXCHANGE_NAME,
    DEAD_LETTER_QUEUE_NAME,
    DEAD_LETTER_ROUTING_KEY,
    EXCHANGE_NAME,
    LEGACY_QUEUE_NAME,
    POISON_ROUTING_KEY,
    QUEUE_NAME,
    ROUTING_KEY,
)


class WorkerExecutor(Protocol):
    async def execute(
        self, *, input_payload: dict[str, Any], policy_snapshot: dict[str, Any]
    ) -> ExecutionResult: ...


class RabbitMqWorker:
    """Consumes delivery hints and acknowledges only after durable handling."""

    def __init__(
        self,
        *,
        url: str,
        worker_id: str,
        prefetch_count: int,
        execution_service: ExecutionPersistenceService,
        executor: WorkerExecutor,
    ) -> None:
        self._url = url
        self._worker_id = worker_id
        self._prefetch_count = prefetch_count
        self._execution_service = execution_service
        self._executor = executor
        self._connection: AbstractConnection | None = None

    async def run(self) -> None:
        self._connection = await aio_pika.connect_robust(self._url)
        channel = await self._connection.channel()
        await channel.set_qos(prefetch_count=self._prefetch_count)
        exchange = await channel.declare_exchange(
            EXCHANGE_NAME, aio_pika.ExchangeType.DIRECT, durable=True
        )
        dead_letter_exchange = await channel.declare_exchange(
            DEAD_LETTER_EXCHANGE_NAME, aio_pika.ExchangeType.DIRECT, durable=True
        )
        queue = await channel.declare_queue(
            QUEUE_NAME,
            durable=True,
            arguments={
                "x-dead-letter-exchange": DEAD_LETTER_EXCHANGE_NAME,
                "x-dead-letter-routing-key": POISON_ROUTING_KEY,
            },
        )
        # ARR-6 used this queue without DLQ arguments. Consume it only to drain
        # in-flight legacy deliveries; new publications bind exclusively to v2.
        legacy_queue = await channel.declare_queue(LEGACY_QUEUE_NAME, durable=True)
        dead_letter_queue = await channel.declare_queue(DEAD_LETTER_QUEUE_NAME, durable=True)
        await queue.bind(exchange, routing_key=ROUTING_KEY)
        await dead_letter_queue.bind(dead_letter_exchange, routing_key=DEAD_LETTER_ROUTING_KEY)
        await dead_letter_queue.bind(dead_letter_exchange, routing_key=POISON_ROUTING_KEY)
        await queue.consume(self._handle_delivery, no_ack=False)
        await legacy_queue.consume(self._handle_delivery, no_ack=False)
        await asyncio.Future()

    async def close(self) -> None:
        if self._connection is not None:
            await self._connection.close()
        self._connection = None

    async def _handle_delivery(self, message: AbstractIncomingMessage) -> None:
        try:
            run_id = self.run_id_from_message(message.body)
        except ValueError:
            await message.reject(requeue=False)
            return

        try:
            claim = await self._execution_service.claim(run_id=run_id, worker_id=self._worker_id)
            if claim.decision != ClaimDecision.CLAIMED:
                await message.ack()
                return

            persisted = await self._execute_claim(claim)
            if persisted:
                await message.ack()
            else:
                await message.nack(requeue=True)
        except Exception:
            # No acknowledgement on an infrastructure failure. The broker may
            # redeliver; the lease makes the duplicate harmless.
            await message.nack(requeue=True)
            raise

    async def _execute_claim(self, claim: ClaimResult) -> bool:
        if claim.attempt_id is None or claim.input_payload is None or claim.policy_snapshot is None:
            raise RuntimeError("Claimed run must include attempt and execution payload")
        try:
            retry_policy = RetryPolicy.from_snapshot(claim.policy_snapshot)
            result = await asyncio.wait_for(
                self._executor.execute(
                    input_payload=claim.input_payload, policy_snapshot=claim.policy_snapshot
                ),
                timeout=retry_policy.attempt_timeout_seconds,
            )
        except Exception as exc:
            return await self._execution_service.complete_failure(
                run_id=claim.run_id,
                attempt_id=claim.attempt_id,
                worker_id=self._worker_id,
                error_code=str(classify_exception(exc)),
            )
        return await self._execution_service.complete_success(
            run_id=claim.run_id,
            attempt_id=claim.attempt_id,
            worker_id=self._worker_id,
            result=result,
        )

    @staticmethod
    def run_id_from_message(body: bytes) -> UUID:
        try:
            payload = json.loads(body)
        except json.JSONDecodeError as exc:
            raise ValueError("Message body is not JSON") from exc
        if not isinstance(payload, Mapping) or not isinstance(payload.get("run_id"), str):
            raise ValueError("Message does not contain a run_id")
        try:
            return UUID(payload["run_id"])
        except ValueError as exc:
            raise ValueError("Message run_id is invalid") from exc
