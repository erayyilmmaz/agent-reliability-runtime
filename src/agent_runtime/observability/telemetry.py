from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from opentelemetry import metrics, propagate, trace
from opentelemetry.context import Context
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk.resources import SERVICE_NAME, Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor

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
    propagate.inject(carrier)
    return carrier


def extract_trace_context(carrier: Mapping[str, str]) -> Context:
    return propagate.extract(carrier)
