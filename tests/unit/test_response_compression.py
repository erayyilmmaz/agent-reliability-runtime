"""PERF-007: compress the pages that are large enough to be worth it, and only
those, without moving the work outside the latency histogram."""

from __future__ import annotations

import gzip
import inspect
import json

from fastapi.testclient import TestClient
from starlette.middleware.gzip import GZipMiddleware

from agent_runtime.api.main import create_app
from agent_runtime.settings import Settings


def _app(**overrides):
    return create_app(Settings(environment="local", auth_mode="disabled", **overrides))


def _middleware_names(app) -> list[str]:
    names = []
    for mw in app.user_middleware:
        name = getattr(mw.cls, "__name__", str(mw.cls))
        if name == "BaseHTTPMiddleware":
            fn = mw.kwargs.get("dispatch") or (mw.args[0] if mw.args else None)
            name = getattr(fn, "__name__", name)
        names.append(name)
    return names


def test_compression_runs_inside_the_latency_middleware():
    """``user_middleware`` is ordered outermost-first, so GZip must appear
    *after* ``trace_http_request``. Two things break if it does not:

    * ``arr.http.server.duration`` (PERF-006) would exclude compression, which
      is work the client waits on;
    * and ``minimum_size`` would stop working at all —
      ``test_small_responses_are_not_compressed`` is the one that catches that.
    """

    names = _middleware_names(_app())
    assert "GZipMiddleware" in names
    assert names.index("GZipMiddleware") > names.index("trace_http_request")


def test_compression_can_be_turned_off_for_ingress_termination():
    assert "GZipMiddleware" not in _middleware_names(_app(response_compression_min_bytes=0))


def test_small_responses_are_not_compressed():
    """Below the threshold gzip costs CPU — the scarce resource here — to save
    a few hundred bytes.

    This is not automatic. ``BaseHTTPMiddleware`` re-emits responses as streams
    with no ``Content-Length``, so a ``GZipMiddleware`` registered outside it
    compresses every response regardless of ``minimum_size``; with the stack in
    that order this healthz body came back gzipped and chunked at 15 bytes.
    """

    with TestClient(_app()) as client:
        response = client.get("/healthz", headers={"Accept-Encoding": "gzip"})
    assert response.status_code == 200
    assert "content-encoding" not in response.headers


def test_a_page_above_the_threshold_is_compressed_and_round_trips():
    payload = json.dumps([{"event_id": str(i), "metadata": {"source": "api"}} for i in range(200)])
    assert len(payload) > 1024

    app = _app()

    @app.get("/_compression_probe")
    async def probe() -> list[dict[str, object]]:  # pragma: no cover - exercised below
        return json.loads(payload)

    with TestClient(app) as client:
        compressed = client.get("/_compression_probe", headers={"Accept-Encoding": "gzip"})
        plain = client.get("/_compression_probe", headers={"Accept-Encoding": "identity"})

    assert compressed.headers["content-encoding"] == "gzip"
    assert "content-encoding" not in plain.headers
    # httpx decodes transparently, so compare the decoded bodies and measure the
    # saving from the wire length the server reported.
    assert compressed.json() == plain.json()
    assert int(compressed.headers["content-length"]) < int(plain.headers["content-length"])


def test_identity_is_honoured():
    """A client that asks for no encoding must not receive gzip."""

    app = _app()

    @app.get("/_compression_identity")
    async def probe() -> list[int]:  # pragma: no cover - exercised below
        return list(range(2000))

    with TestClient(app) as client:
        response = client.get("/_compression_identity", headers={"Accept-Encoding": "identity"})
    assert "content-encoding" not in response.headers


def test_configured_level_is_the_measured_trade_off_not_the_library_default():
    """Level 1 was chosen deliberately: on a measured 100-event page it kept
    97.8% of level 9's byte saving for 38% of the CPU, and compression runs on
    the event loop for every page this API can produce."""

    settings = Settings(environment="local", auth_mode="disabled")
    assert settings.response_compression_level == 1
    library_default = inspect.signature(GZipMiddleware.__init__)
    library_default = library_default.parameters["compresslevel"].default
    assert settings.response_compression_level != library_default

    sample = json.dumps(
        [{"event_id": f"{i:08d}", "event_type": "RUN_QUEUED"} for i in range(100)]
    ).encode()
    assert len(gzip.compress(sample, compresslevel=1)) < len(sample) * 0.5
