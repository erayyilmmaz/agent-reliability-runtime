from __future__ import annotations

import asyncio
import logging
import socket

import uvicorn

from agent_runtime.infrastructure.database.execution_service import ExecutionPersistenceService
from agent_runtime.infrastructure.database.session import (
    create_database_engine,
    create_session_factory,
)
from agent_runtime.infrastructure.messaging.dispatcher import OutboxDispatcher
from agent_runtime.infrastructure.messaging.publisher import RabbitMqPublisher
from agent_runtime.infrastructure.messaging.worker import RabbitMqWorker
from agent_runtime.observability.logging import configure_structured_logging
from agent_runtime.observability.telemetry import TelemetryRuntime, configure_telemetry
from agent_runtime.providers.deterministic import DeterministicProvider
from agent_runtime.providers.openai_responses import OpenAIResponsesProvider
from agent_runtime.providers.registry import ProviderRegistry
from agent_runtime.settings import Settings, get_settings


def run_api() -> None:
    """Run only the FastAPI process."""

    settings = get_settings()
    telemetry = _configure_process_observability(settings, "agent-runtime-api")
    try:
        uvicorn.run(
            "agent_runtime.api.main:app",
            host="0.0.0.0",
            port=8000,
            log_level=settings.log_level.lower(),
            log_config=None,
            access_log=False,
        )
    finally:
        _shutdown_telemetry(telemetry)


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
    settings = get_settings()
    telemetry = _configure_process_observability(settings, "agent-runtime-dispatcher")
    try:
        asyncio.run(_run_dispatcher(settings))
    finally:
        _shutdown_telemetry(telemetry)


async def _run_worker(settings: Settings) -> None:
    engine = create_database_engine(settings)
    worker = RabbitMqWorker(
        url=str(settings.rabbitmq_url),
        worker_id=socket.gethostname(),
        prefetch_count=settings.worker_concurrency,
        execution_service=ExecutionPersistenceService(
            create_session_factory(engine), lease_seconds=settings.execution_lease_seconds
        ),
        executor=ProviderRegistry(
            [
                DeterministicProvider(),
                OpenAIResponsesProvider(
                    api_key=settings.openai_api_key,
                    base_url=str(settings.openai_base_url),
                    default_model=settings.openai_default_model,
                ),
            ]
        ),
    )
    try:
        await worker.run()
    finally:
        await worker.close()
        await engine.dispose()


def run_worker() -> None:
    settings = get_settings()
    telemetry = _configure_process_observability(settings, "agent-runtime-worker")
    try:
        asyncio.run(_run_worker(settings))
    finally:
        _shutdown_telemetry(telemetry)


async def _run_recovery(settings: Settings) -> None:
    engine = create_database_engine(settings)
    recovery = ExecutionPersistenceService(
        create_session_factory(engine), lease_seconds=settings.execution_lease_seconds
    )
    logger = logging.getLogger(__name__)
    try:
        while True:
            recovered_count = await recovery.recover_expired_leases()
            queued_count = await recovery.schedule_due_retries()
            if recovered_count:
                logger.info("execution_lease_recovery_complete recovered_count=%s", recovered_count)
            if queued_count:
                logger.info("retry_schedule_complete queued_count=%s", queued_count)
            await asyncio.sleep(
                min(
                    settings.lease_recovery_poll_interval_seconds,
                    settings.retry_scheduler_poll_interval_seconds,
                )
            )
    finally:
        await engine.dispose()


def run_recovery() -> None:
    settings = get_settings()
    telemetry = _configure_process_observability(settings, "agent-runtime-scheduler")
    try:
        asyncio.run(_run_recovery(settings))
    finally:
        _shutdown_telemetry(telemetry)


def _configure_process_observability(
    settings: Settings, service_name: str
) -> TelemetryRuntime | None:
    configure_structured_logging(settings.log_level)
    return configure_telemetry(settings=settings, service_name=service_name)


def _shutdown_telemetry(telemetry: TelemetryRuntime | None) -> None:
    if telemetry is not None:
        telemetry.shutdown()
