from __future__ import annotations

import json
import logging
import sys
from datetime import UTC, datetime, timedelta

import pytest
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from agent_runtime.observability import telemetry
from agent_runtime.observability.exceptions import record_safe_exception
from agent_runtime.observability.logging import JsonFormatter
from agent_runtime.observability.telemetry import sanitize_trace_context
from agent_runtime.providers.deterministic import DeterministicProvider
from agent_runtime.security.audit import safe_text
from agent_runtime.settings import Settings


def _settings(**overrides) -> Settings:
    return Settings(environment="local", auth_mode="disabled", **overrides)


PARENT = "00-00000000000000000000000000000001-0000000000000002-01"


@pytest.mark.parametrize(
    "parent",
    [
        "bad",
        PARENT.upper(),
        "ff" + PARENT[2:],
        PARENT[:-2] + "ff",
        "00-" + "0" * 32 + PARENT[35:],
        PARENT + "-extra",
        "x" * 10000,
    ],
)
def test_invalid_parent_drops_entire_context(parent):
    # PARENT uses only digits, so explicitly add an uppercase hex digit.
    if parent == PARENT:
        parent = PARENT.replace("0001", "000A")
    assert sanitize_trace_context({"traceparent": parent, "tracestate": "vendor=value"}) == {}


@pytest.mark.parametrize(
    "state",
    [
        "x" * 513,
        "a=1,a=2",
        "a=bad=secret",
        "a=\nsecret",
        ",".join(f"v{i}=x" for i in range(33)),
        "a=" + "x" * 257,
        "A=bad",
    ],
)
def test_invalid_tracestate_is_not_forwarded(state):
    assert sanitize_trace_context({"traceparent": PARENT, "tracestate": state}) == {
        "traceparent": PARENT
    }


def test_valid_trace_context_is_bounded_and_baggage_is_dropped():
    assert sanitize_trace_context(
        {"traceparent": PARENT, "tracestate": "v=x,2@vendor=y", "baggage": "prompt=secret"}
    ) == {"traceparent": PARENT, "tracestate": "v=x,2@vendor=y"}


def test_exception_log_and_exported_span_exclude_messages_locals_and_chains(monkeypatch):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    monkeypatch.setattr(telemetry, "get_tracer", lambda: provider.get_tracer("test"))
    secret = "prompt=private API_KEY=top-secret provider-body=classified"
    with pytest.raises(RuntimeError), telemetry.safe_span("test"):
        try:
            raise ValueError(secret)
        except ValueError as cause:
            try:
                raise RuntimeError(secret) from cause
            except RuntimeError as exc:
                record = logging.LogRecord(
                    "test", logging.ERROR, __file__, 1, "failure %s", (secret,), sys.exc_info()
                )
                rendered = JsonFormatter().format(record)
                assert secret not in rendered and "RuntimeError" in rendered
                record_safe_exception(exc, event="WORKER_EXECUTION_FAILED")
                raise
    spans = exporter.get_finished_spans()
    assert spans and len(spans[0].events) == 2
    exported = "".join(span.to_json() for span in spans)
    assert secret not in exported and "exception.type" in exported
    assert "ValueError:" not in exported
    provider.shutdown()


def test_audit_sink_text_bounds_and_invalid_characters():
    assert safe_text("a" * 1000, 128) == "a" * 128
    assert safe_text("hello\n\x00\ud800", 64) == "hello___"
    assert safe_text(None, 64) is None


async def test_deterministic_echo_requires_operator_opt_in_and_never_echoes_policy():
    payload = {"prompt": "private input"}
    policy = {"instructions": "secret instruction", "echo_input": True}
    safe = await DeterministicProvider().execute(input_payload=payload, policy_snapshot=policy)
    assert safe.result_payload == {"accepted": True}
    explicit = await DeterministicProvider(echo_input=True).execute(
        input_payload=payload, policy_snapshot=policy
    )
    assert explicit.result_payload["accepted_input"] == payload
    assert "secret instruction" not in json.dumps(explicit.result_payload)


@pytest.mark.parametrize(
    "args",
    [
        ["--apply"],
        ["--apply", "--tenant-id", "tenant-a"],
        ["--apply", "--tenant-id", "tenant-a", "--confirm-tenant", "tenant-b"],
    ],
)
def test_retention_cli_requires_exact_erasure_confirmation(args, monkeypatch):
    from agent_runtime.security.retention_cli import main

    monkeypatch.setattr(sys, "argv", ["agent-runtime-retention", *args])
    with pytest.raises(SystemExit) as caught:
        main()
    assert caught.value.code == 2


@pytest.mark.parametrize(
    "args",
    [
        ["--apply"],
        ["--apply", "--confirm-retention-days", "7"],
    ],
)
def test_audit_purge_refuses_apply_without_a_matching_horizon(args, monkeypatch):
    """PERF-005: deletion on an append-only table needs the horizon restated."""

    from agent_runtime.security import audit_retention

    monkeypatch.setattr(audit_retention, "get_settings", lambda: _settings(audit_retention_days=30))
    monkeypatch.setattr(sys, "argv", ["agent-runtime-audit-purge", *args])
    with pytest.raises(SystemExit) as caught:
        audit_retention.main()
    assert caught.value.code == 2


def test_audit_purge_refuses_apply_when_retention_is_unset(monkeypatch):
    from agent_runtime.security import audit_retention

    monkeypatch.setattr(audit_retention, "get_settings", _settings)
    monkeypatch.setattr(
        sys, "argv", ["agent-runtime-audit-purge", "--apply", "--confirm-retention-days", "30"]
    )
    with pytest.raises(SystemExit) as caught:
        audit_retention.main()
    assert caught.value.code == 2


def test_audit_purge_cutoff_follows_the_configured_horizon():
    from agent_runtime.security.audit_retention import resolve_cutoff

    now = datetime(2026, 9, 12, tzinfo=UTC)
    assert resolve_cutoff(_settings(audit_retention_days=30), now=now) == now - timedelta(days=30)
