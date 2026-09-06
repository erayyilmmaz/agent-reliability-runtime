from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import aio_pika
from aio_pika.abc import AbstractChannel, AbstractConnection, AbstractExchange
from pamqp.commands import Basic

from agent_runtime.infrastructure.database.models import OutboxEvent

EXCHANGE_NAME = "agent_runtime"
QUEUE_NAME = "agent_runtime.execution"
ROUTING_KEY = "run.queued"


class PublishNotConfirmedError(RuntimeError):
    """RabbitMQ accepted the connection but did not confirm the message."""


class RabbitMqPublisher:
    """Durable publisher that requires a broker confirmation for every message."""

    def __init__(self, *, url: str, publish_timeout_seconds: float) -> None:
        self._url = url
        self._publish_timeout_seconds = publish_timeout_seconds
        self._connection: AbstractConnection | None = None
        self._channel: AbstractChannel | None = None
        self._exchange: AbstractExchange | None = None

    async def publish(self, event: OutboxEvent) -> None:
        exchange = await self._get_exchange()
        message = aio_pika.Message(
            body=json.dumps(
                self.message_payload(event), separators=(",", ":"), sort_keys=True
            ).encode("utf-8"),
            content_type="application/json",
            correlation_id=str(event.aggregate_id),
            message_id=str(event.id),
            delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
        )
        try:
            confirmation = await exchange.publish(
                message,
                routing_key=ROUTING_KEY,
                mandatory=True,
                timeout=self._publish_timeout_seconds,
            )
        except Exception:
            await self.close()
            raise

        if confirmation is not None and not isinstance(confirmation, Basic.Ack):
            raise PublishNotConfirmedError(f"Broker confirmation was {type(confirmation).__name__}")

    @staticmethod
    def message_payload(event: OutboxEvent) -> dict[str, Any]:
        """Keep messages small; workers rehydrate all authoritative data from PostgreSQL."""

        trace_context = event.payload.get("trace_context", {})
        if not isinstance(trace_context, Mapping):
            trace_context = {}
        return {
            "event_id": str(event.id),
            "run_id": str(event.aggregate_id),
            "trace_context": dict(trace_context),
        }

    async def close(self) -> None:
        if self._connection is not None:
            await self._connection.close()
        self._connection = None
        self._channel = None
        self._exchange = None

    async def _get_exchange(self) -> AbstractExchange:
        if self._exchange is not None:
            return self._exchange

        self._connection = await aio_pika.connect_robust(self._url)
        self._channel = await self._connection.channel(publisher_confirms=True)
        self._exchange = await self._channel.declare_exchange(
            EXCHANGE_NAME, aio_pika.ExchangeType.DIRECT, durable=True
        )
        queue = await self._channel.declare_queue(QUEUE_NAME, durable=True)
        await queue.bind(self._exchange, routing_key=ROUTING_KEY)
        return self._exchange
