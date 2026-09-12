"""PERF-006: latency histograms, and the cardinality discipline they must keep."""

from __future__ import annotations

import pathlib

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from agent_runtime.observability import metrics as metrics_module
from agent_runtime.observability.metrics import (
    RuntimeMetrics,
    safe_db_operation,
    safe_route,
    status_class,
)


@pytest.fixture
def collected():
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    runtime = RuntimeMetrics(provider.get_meter("test"))

    def read() -> dict[str, list[dict]]:
        data = reader.get_metrics_data()
        found: dict[str, list[dict]] = {}
        for resource in data.resource_metrics:
            for scope in resource.scope_metrics:
                for metric in scope.metrics:
                    points = getattr(metric.data, "data_points", [])
                    found.setdefault(metric.name, []).extend(
                        dict(point.attributes or {}) for point in points
                    )
        return found

    yield runtime, read
    provider.shutdown()


def test_histograms_exist_for_every_latency_the_audit_needed(collected) -> None:
    """Before PERF-006 all eight instruments were counters and no percentile
    of anything could be computed from what this runtime emits."""
    runtime, read = collected

    runtime.http_request(route="/v1/runs/{run_id}", method="GET", status_code=200, seconds=0.007)
    runtime.db_operation("get_run", 0.000028)
    runtime.provider_call_duration("deterministic", 0.004)
    runtime.outbox_lag("RUN_QUEUED", 0.12)

    names = read().keys()
    assert "http.server.request.duration" in names
    assert "arr.db.operation.duration" in names
    assert "arr.provider.call.duration" in names
    assert "arr.outbox.lag" in names


def test_http_labels_use_the_route_template_never_the_raw_path(collected) -> None:
    runtime, read = collected

    runtime.http_request(route="/v1/runs/{run_id}", method="GET", status_code=200, seconds=0.01)

    labels = read()["http.server.request.duration"][0]
    assert labels["http.route"] == "/v1/runs/{run_id}"
    assert labels["http.response.status_class"] == "2xx"
    assert labels["http.request.method"] == "GET"


def test_unmatched_paths_collapse_to_one_series(collected) -> None:
    """A raw URL as a label would give every run UUID its own time series."""
    runtime, read = collected

    for path in ("/v1/runs/aaa", "/v1/runs/bbb", "/nope"):
        assert safe_route(None) == "unmatched", path
        runtime.http_request(route=None, method="GET", status_code=404, seconds=0.001)

    routes = {point["http.route"] for point in read()["http.server.request.duration"]}
    assert routes == {"unmatched"}


@pytest.mark.parametrize(
    "code,expected", [(200, "2xx"), (202, "2xx"), (404, "4xx"), (429, "4xx"), (503, "5xx")]
)
def test_status_is_bucketed_by_class_not_exact_code(code: int, expected: str) -> None:
    assert status_class(code) == expected


def test_unknown_db_operation_is_collapsed() -> None:
    assert safe_db_operation("get_run") == "get_run"
    assert safe_db_operation("DROP TABLE runs") == "other"


def test_no_identifier_is_ever_used_as_a_label(collected) -> None:
    """run_id, attempt_id and principal_id must never become labels.

    A latency histogram keyed by run_id would be worse than no histogram: it
    would multiply series without bound and take the metrics backend with it.
    """
    runtime, read = collected

    runtime.http_request(route="/v1/runs/{run_id}", method="GET", status_code=200, seconds=0.01)
    runtime.db_operation("get_run", 0.001)
    runtime.provider_call_duration("openai", 0.5)
    runtime.outbox_lag("RUN_QUEUED", 0.05)

    forbidden = {"run_id", "attempt_id", "principal_id", "client_id", "tenant_id", "http.target"}
    for points in read().values():
        for labels in points:
            assert not forbidden & set(labels), labels


def test_bucket_boundaries_resolve_the_measured_range() -> None:
    """The hot query is 0.028 ms and a request is ~7 ms; one bucket set cannot
    resolve both, so internal work gets finer low-end boundaries."""
    assert min(metrics_module.INTERNAL_DURATION_BUCKETS) < 0.001
    assert 0.005 in metrics_module.HTTP_DURATION_BUCKETS
    assert max(metrics_module.HTTP_DURATION_BUCKETS) >= 10.0


def test_alert_thresholds_are_reachable_within_their_bucket_range() -> None:
    """histogram_quantile never returns more than the largest finite bucket.

    Writing the promtool tests exposed this: arr.outbox.lag and
    arr.provider.call.duration originally reused the 10 s HTTP boundaries while
    their alerts fired above 30 s, so those two rules could never have fired at
    all. The instrument's range and the rule's threshold have to be checked
    together, and this test is what keeps them together.
    """
    import re

    rules = pathlib.Path("docker/performance-alerts.yml").read_text()
    thresholds = {
        "arr_outbox_lag_seconds_bucket": metrics_module.SLOW_OPERATION_BUCKETS,
        "arr_provider_call_duration_seconds_bucket": metrics_module.SLOW_OPERATION_BUCKETS,
        "arr_db_operation_duration_seconds_bucket": metrics_module.INTERNAL_DURATION_BUCKETS,
        "http_server_request_duration_seconds_bucket": metrics_module.HTTP_DURATION_BUCKETS,
    }

    checked = 0
    for metric, buckets in thresholds.items():
        for block in re.findall(rf"{metric}.*?\)\)\s*>\s*([0-9.]+)", rules, re.S):
            assert float(block) <= max(buckets), (
                f"{metric}: alert threshold {block}s exceeds the largest finite "
                f"bucket {max(buckets)}s, so the rule can never fire"
            )
            checked += 1
    assert checked >= 4, f"expected to check several thresholds, checked {checked}"
