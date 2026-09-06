from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

import aio_pika
from aio_pika.abc import AbstractChannel, AbstractConnection, AbstractExchange
from pamqp.commands import Basic

from agent_runtime.infrastructure.database.models import OutboxEvent

EXCHANGE_NAME = "agent_runtime"
LEGACY_QUEUE_NAME = "agent_runtime.execution"
QUEUE_NAME = "agent_runtime.execution.v2"
ROUTING_KEY = "run.queued"
DEAD_LETTER_EXCHANGE_NAME = "agent_runtime.dlx"
DEAD_LETTER_QUEUE_NAME = "agent_runtime.dead_letter"
DEAD_LETTER_ROUTING_KEY = "run.dead_lettered"
POISON_ROUTING_KEY = "poison"


class PublishNotConfirmedError(RuntimeError):
    """RabbitMQ accepted the connection but did not confirm the message."""


class RabbitMqPublisher:
    """Durable publisher that requires a broker confirmation for every message."""

    def __init__(self, *, url: str, publish_timeout_seconds: float) -> None:
        self._url = url
        self._publish_timeout_seconds = publish_timeout_seconds
        self._connection: AbstractConnection | None = None
        self._channel: AbstractChannel | None = None
        self._exchanges: dict[str, AbstractExchange] = {}

    async def publish(self, event: OutboxEvent) -> None:
        exchange, routing_key = await self._destination_for(event)
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
                routing_key=routing_key,
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
        self._exchanges = {}

    async def _destination_for(self, event: OutboxEvent) -> tuple[AbstractExchange, str]:
        exchanges = await self._get_exchanges()
        if event.event_type == "RUN_QUEUED":
            return exchanges[EXCHANGE_NAME], ROUTING_KEY
        if event.event_type == "RUN_DEAD_LETTERED":
            return exchanges[DEAD_LETTER_EXCHANGE_NAME], DEAD_LETTER_ROUTING_KEY
        raise ValueError(f"Unsupported outbox event type: {event.event_type}")

    async def _get_exchanges(self) -> dict[str, AbstractExchange]:
        if self._exchanges:
            return self._exchanges

        self._connection = await aio_pika.connect_robust(self._url)
        self._channel = await self._connection.channel(publisher_confirms=True)
        execution_exchange = await self._channel.declare_exchange(
            EXCHANGE_NAME, aio_pika.ExchangeType.DIRECT, durable=True
        )
        dead_letter_exchange = await self._channel.declare_exchange(
            DEAD_LETTER_EXCHANGE_NAME, aio_pika.ExchangeType.DIRECT, durable=True
        )
        queue = await self._channel.declare_queue(
            QUEUE_NAME,
            durable=True,
            arguments={
                "x-dead-letter-exchange": DEAD_LETTER_EXCHANGE_NAME,
                "x-dead-letter-routing-key": POISON_ROUTING_KEY,
            },
        )
        dead_letter_queue = await self._channel.declare_queue(DEAD_LETTER_QUEUE_NAME, durable=True)
        await queue.bind(execution_exchange, routing_key=ROUTING_KEY)
        await dead_letter_queue.bind(dead_letter_exchange, routing_key=DEAD_LETTER_ROUTING_KEY)
        await dead_letter_queue.bind(dead_letter_exchange, routing_key=POISON_ROUTING_KEY)
        self._exchanges = {
            EXCHANGE_NAME: execution_exchange,
            DEAD_LETTER_EXCHANGE_NAME: dead_letter_exchange,
        }
        return self._exchanges
