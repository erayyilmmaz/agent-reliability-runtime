from __future__ import annotations

import asyncio
import logging

import uvicorn

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


def run_dispatcher() -> None:
    asyncio.run(_run_idle_process("outbox-dispatcher", get_settings()))


def run_worker() -> None:
    asyncio.run(_run_idle_process("worker", get_settings()))


def run_recovery() -> None:
    asyncio.run(_run_idle_process("scheduler-recovery", get_settings()))
