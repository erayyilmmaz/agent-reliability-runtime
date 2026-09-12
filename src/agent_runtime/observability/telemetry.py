from __future__ import annotations

import re
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass

from opentelemetry import metrics, trace
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

from agent_runtime.observability.exceptions import record_safe_exception
from agent_runtime.settings import Settings

TRACER_NAME = "agent_runtime"


@dataclass(frozen=True)
class TelemetryRuntime:
    tracer_provider: TracerProvider
    meter_provider: MeterProvider

    def shutdown(self) -> None:
        self.meter_provider.shutdown()
        self.tracer_provider.shutdown()


def configure_telemetry(*, settings: Settings, service_name: str) -> TelemetryRuntime | None:
    """Configure OTLP export once per runtime process; no payload capture is enabled."""

    if not settings.otel_enabled:
        return None
    resource = Resource.create({SERVICE_NAME: service_name})
    endpoint = str(settings.otel_endpoint).rstrip("/")
    tracer_provider = TracerProvider(resource=resource)
    tracer_provider.add_span_processor(
        BatchSpanProcessor(OTLPSpanExporter(endpoint=f"{endpoint}/v1/traces"))
    )
    meter_provider = MeterProvider(
        resource=resource,
        metric_readers=[
            PeriodicExportingMetricReader(
                OTLPMetricExporter(endpoint=f"{endpoint}/v1/metrics"), export_interval_millis=5_000
            )
        ],
    )
    trace.set_tracer_provider(tracer_provider)
    metrics.set_meter_provider(meter_provider)
    return TelemetryRuntime(tracer_provider=tracer_provider, meter_provider=meter_provider)


def get_tracer() -> trace.Tracer:
    return trace.get_tracer(TRACER_NAME)


def inject_trace_context() -> dict[str, str]:
    carrier: dict[str, str] = {}
    TraceContextTextMapPropagator().inject(carrier)
    return sanitize_trace_context(carrier)


def extract_trace_context(carrier: Mapping[str, str]) -> Context:
    return TraceContextTextMapPropagator().extract(
        sanitize_trace_context(carrier), context=Context()
    )


def sanitize_trace_context(carrier: Mapping[str, str]) -> dict[str, str]:
    parent = carrier.get("traceparent", "")
    if not isinstance(parent, str) or not re.fullmatch(
        r"00-[0-9a-f]{32}-[0-9a-f]{16}-0[0-3]", parent
    ):
        return {}
    if int(parent[3:35], 16) == 0 or int(parent[36:52], 16) == 0:
        return {}
    result = {"traceparent": parent}
    state = carrier.get("tracestate", "")
    if not isinstance(state, str) or not state or len(state) > 512:
        return result
    entries = state.split(",")
    seen = set()
    if len(entries) > 32:
        return result
    for entry in entries:
        key, separator, value = entry.strip(" \t").partition("=")
        if (
            not separator
            or key in seen
            or not re.fullmatch(
                r"(?:[a-z][a-z0-9_*/-]{0,255}|[a-z0-9][a-z0-9_*/-]{0,240}@[a-z][a-z0-9_*/-]{0,13})",
                key,
            )
            or not 1 <= len(value) <= 256
            or value.endswith(" ")
            or any(ord(c) < 32 or ord(c) > 126 or c in ",=" for c in value)
        ):
            return result
        seen.add(key)
    result["tracestate"] = state
    return result


@contextmanager
def safe_span(name: str, *, context: Context | None = None) -> Iterator[trace.Span]:
    # OTel's default context manager records str(exc) and the raw traceback.
    with get_tracer().start_as_current_span(
        name, context=context, record_exception=False, set_status_on_exception=False
    ) as span:
        try:
            yield span
        except BaseException as exc:
            record_safe_exception(exc, event="SPAN_FAILURE")
            raise
