"""Diagnostic context without exception messages, locals, SQL or provider bodies."""

from __future__ import annotations

import logging
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import StatusCode


def safe_exception_context(exc: BaseException) -> dict[str, Any]:
    known = {
        "TimeoutError",
        "ConnectionError",
        "ValueError",
        "RuntimeError",
        "TypeError",
        "OperationalError",
        "IntegrityError",
        "DBAPIError",
        "QuotaExceededError",
        "ProviderExecutionError",
        "EvaluationConfigurationError",
    }
    kind = next((cls.__name__ for cls in type(exc).__mro__ if cls.__name__ in known), "Exception")
    frames: list[str] = []
    tb = exc.__traceback__
    while tb is not None:
        module = tb.tb_frame.f_globals.get("__name__", "")
        if isinstance(module, str) and module.startswith("agent_runtime."):
            frames.append(f"{module}:{tb.tb_lineno}")
        tb = tb.tb_next
    return {"exception_type": kind, "exception_frames": frames[-8:]}


def record_safe_exception(exc: BaseException, *, event: str) -> None:
    context = safe_exception_context(exc)
    span = trace.get_current_span()
    span.add_event(
        "exception",
        attributes={
            "exception.type": context["exception_type"],
            "exception.stacktrace": "\n".join(context["exception_frames"]),
            "exception.escaped": False,
        },
    )
    span.set_status(StatusCode.ERROR)
    logging.getLogger(__name__).error("runtime failure", extra={"event": event, **context})
