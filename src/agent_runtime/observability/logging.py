from __future__ import annotations

import json
import logging
from typing import Any

from opentelemetry import trace

from agent_runtime.observability.exceptions import safe_exception_context

_SAFE_LOG_FIELDS = frozenset({"event", "run_id", "attempt_id", "provider", "error_code"})


class JsonFormatter(logging.Formatter):
    """Allowlist-only JSON formatter that prevents payloads and secrets reaching stdout."""

    def format(self, record: logging.LogRecord) -> str:
        span_context = trace.get_current_span().get_span_context()
        payload: dict[str, Any] = {
            "timestamp": self.formatTime(record, "%Y-%m-%dT%H:%M:%S%z"),
            "level": record.levelname,
            "logger": record.name,
            # Library log arguments and exception messages can contain URLs, SQL and secrets.
            "message": "runtime event",
        }
        for field in _SAFE_LOG_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = str(value)
        if record.exc_info and record.exc_info[1] is not None:
            payload.update(safe_exception_context(record.exc_info[1]))
        elif hasattr(record, "exception_type"):
            payload["exception_type"] = record.exception_type
            payload["exception_frames"] = getattr(record, "exception_frames", [])
        if span_context.is_valid:
            payload["trace_id"] = format(span_context.trace_id, "032x")
            payload["span_id"] = format(span_context.span_id, "016x")
        return json.dumps(payload, separators=(",", ":"), sort_keys=True)


def configure_structured_logging(level: str) -> None:
    root_logger = logging.getLogger()
    if any(isinstance(handler.formatter, JsonFormatter) for handler in root_logger.handlers):
        return
    handler = logging.StreamHandler()
    handler.setFormatter(JsonFormatter())
    root_logger.handlers = [handler]
    root_logger.setLevel(level)
