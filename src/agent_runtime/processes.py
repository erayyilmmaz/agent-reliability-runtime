from __future__ import annotations

import asyncio
import logging
import socket

import uvicorn

from agent_runtime.execution.deterministic import DeterministicExecutor
from agent_runtime.infrastructure.database.execution_service import ExecutionPersistenceService
from agent_runtime.infrastructure.database.session import (
    create_database_engine,
    create_session_factory,
)
from agent_runtime.infrastructure.messaging.dispatcher import OutboxDispatcher
from agent_runtime.infrastructure.messaging.publisher import RabbitMqPublisher
from agent_runtime.infrastructure.messaging.worker import RabbitMqWorker
from agent_runtime.settings import Settings, get_settings


def run_api() -> None:
    """Run only the FastAPI process."""

    settings = get_settings()
    uvicorn.run(
        "agent_runtime.api.main:app",
        host="0.0.0.0",
        port=8000,
        log_level=settings.log_level.lower(),
    )


async def _run_idle_process(name: str, settings: Settings) -> None:
    """Temporary isolated process boundary until its ARR implementation lands."""

    logging.basicConfig(level=settings.log_level, format="%(message)s")
    logging.getLogger(__name__).info("%s process started", name)
    await asyncio.Event().wait()


async def _run_dispatcher(settings: Settings) -> None:
    engine = create_database_engine(settings)
    publisher = RabbitMqPublisher(
        url=str(settings.rabbitmq_url),
        publish_timeout_seconds=settings.outbox_publish_timeout_seconds,
    )
    dispatcher = OutboxDispatcher(
        session_factory=create_session_factory(engine),
        publisher=publisher,
        batch_size=settings.outbox_batch_size,
    )
    logger = logging.getLogger(__name__)
    try:
        while True:
            published_count = await dispatcher.dispatch_once()
            if published_count:
                logger.info("outbox_dispatch_complete published_count=%s", published_count)
                continue
            await asyncio.sleep(settings.outbox_poll_interval_seconds)
    finally:
        await publisher.close()
        await engine.dispose()


def run_dispatcher() -> None:
    logging.basicConfig(level=get_settings().log_level, format="%(message)s")
    asyncio.run(_run_dispatcher(get_settings()))


async def _run_worker(settings: Settings) -> None:
    engine = create_database_engine(settings)
    worker = RabbitMqWorker(
        url=str(settings.rabbitmq_url),
        worker_id=socket.gethostname(),
        prefetch_count=settings.worker_concurrency,
        execution_service=ExecutionPersistenceService(
            create_session_factory(engine), lease_seconds=settings.execution_lease_seconds
        ),
        executor=DeterministicExecutor(),
    )
    try:
        await worker.run()
    finally:
        await worker.close()
        await engine.dispose()


def run_worker() -> None:
    logging.basicConfig(level=get_settings().log_level, format="%(message)s")
    asyncio.run(_run_worker(get_settings()))


async def _run_recovery(settings: Settings) -> None:
    engine = create_database_engine(settings)
    recovery = ExecutionPersistenceService(
        create_session_factory(engine), lease_seconds=settings.execution_lease_seconds
    )
    logger = logging.getLogger(__name__)
    try:
        while True:
            recovered_count = await recovery.recover_expired_leases()
            if recovered_count:
                logger.info("execution_lease_recovery_complete recovered_count=%s", recovered_count)
            await asyncio.sleep(settings.lease_recovery_poll_interval_seconds)
    finally:
        await engine.dispose()


def run_recovery() -> None:
    logging.basicConfig(level=get_settings().log_level, format="%(message)s")
    asyncio.run(_run_recovery(get_settings()))
