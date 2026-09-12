from __future__ import annotations

import uuid
from typing import Any

from agent_runtime.infrastructure.database.models import OutboxEvent
from agent_runtime.infrastructure.messaging.publisher import (
    DEAD_LETTER_EXCHANGE_NAME,
    DEAD_LETTER_QUEUE_NAME,
    LEGACY_QUEUE_NAME,
    QUEUE_NAME,
    RabbitMqPublisher,
)


def _event(payload: dict[str, Any] | None = None) -> OutboxEvent:
    return OutboxEvent(
        id=uuid.UUID("00000000-0000-0000-0000-000000000001"),
        aggregate_id=uuid.UUID("00000000-0000-0000-0000-000000000002"),
        event_type="RUN_QUEUED",
        payload=payload or {"run_id": "not-used-by-message"},
    )


def test_outbox_message_is_minimal_and_correlatable() -> None:
    assert RabbitMqPublisher.message_payload(
        _event({"trace_context": {"traceparent": "00-abc"}})
    ) == {
        "event_id": "00000000-0000-0000-0000-000000000001",
        "run_id": "00000000-0000-0000-0000-000000000002",
        "trace_context": {},
    }


def test_outbox_message_discards_invalid_trace_context() -> None:
    assert (
        RabbitMqPublisher.message_payload(_event({"trace_context": "not-a-mapping"}))[
            "trace_context"
        ]
        == {}
    )


def test_retry_topology_uses_separate_execution_and_dead_letter_queues() -> None:
    assert QUEUE_NAME.endswith(".v2")
    assert LEGACY_QUEUE_NAME == "agent_runtime.execution"
    assert DEAD_LETTER_EXCHANGE_NAME.endswith(".dlx")
    assert DEAD_LETTER_QUEUE_NAME.endswith(".dead_letter")
