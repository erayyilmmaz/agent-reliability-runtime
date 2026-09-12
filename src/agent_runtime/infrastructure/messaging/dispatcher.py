from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Protocol, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from agent_runtime.infrastructure.database.models import OutboxEvent, RunEvent
from agent_runtime.observability.metrics import get_runtime_metrics
from agent_runtime.observability.telemetry import extract_trace_context, safe_span


class OutboxPublisher(Protocol):
    async def publish(self, event: OutboxEvent) -> None: ...


@dataclass(frozen=True)
class DispatchResult:
    found_event: bool
    published: bool


class OutboxDispatcher:
    """Poll and publish outbox records without claiming exactly-once delivery."""

    def __init__(
        self,
        *,
        session_factory: async_sessionmaker[AsyncSession],
        publisher: OutboxPublisher,
        batch_size: int,
    ) -> None:
        self._session_factory = session_factory
        self._publisher = publisher
        self._batch_size = batch_size

    async def dispatch_once(self) -> int:
        """Publish at most one batch. Stop early on an unavailable broker."""

        published_count = 0
        for _ in range(self._batch_size):
            result = await self._dispatch_one()
            if not result.found_event:
                break
            if not result.published:
                break
            published_count += 1
        return published_count

    async def _dispatch_one(self) -> DispatchResult:
        async with self._session_factory() as session:
            async with session.begin():
                event = cast(
                    OutboxEvent | None,
                    await session.scalar(
                        select(OutboxEvent)
                        .where(OutboxEvent.published_at.is_(None))
                        .order_by(OutboxEvent.created_at.asc())
                        .with_for_update(skip_locked=True)
                        .limit(1)
                    ),
                )
                if event is None:
                    return DispatchResult(found_event=False, published=False)

                trace_context = event.payload.get("trace_context", {})
                carrier = trace_context if isinstance(trace_context, dict) else {}
                with safe_span(
                    "arr.outbox.dispatch", context=extract_trace_context(carrier)
                ) as span:
                    span.set_attribute("arr.run_id", str(event.aggregate_id))
                    span.set_attribute("arr.outbox.event_type", event.event_type)
                    try:
                        await self._publisher.publish(event)
                    except Exception as exc:
                        event.publish_attempts += 1
                        event.last_error = type(exc).__name__
                        get_runtime_metrics().outbox_dispatch(
                            event_type=event.event_type, outcome="failed"
                        )
                        session.add(
                            RunEvent(
                                run_id=event.aggregate_id,
                                event_type="OUTBOX_PUBLISH_FAILED",
                                metadata_={
                                    "event_id": str(event.id),
                                    "error_type": type(exc).__name__,
                                },
                            )
                        )
                        logging.getLogger(__name__).warning(
                            "outbox_publish_failed",
                            extra={"event": "outbox_publish_failed", "run_id": event.aggregate_id},
                        )
                        return DispatchResult(found_event=True, published=False)

                    event.publish_attempts += 1
                    event.published_at = datetime.now(UTC)
                    event.last_error = None
                    get_runtime_metrics().outbox_dispatch(
                        event_type=event.event_type, outcome="published"
                    )
                    session.add(
                        RunEvent(
                            run_id=event.aggregate_id,
                            event_type="OUTBOX_PUBLISHED",
                            metadata_={"event_id": str(event.id)},
                        )
                    )
                    return DispatchResult(found_event=True, published=True)
