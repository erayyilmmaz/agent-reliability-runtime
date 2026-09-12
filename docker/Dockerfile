# SEC-016: pinned by multi-arch index digest so two builds of the same commit
# resolve identical base layers. Update the digest and the comment together.
#   docker buildx imagetools inspect python:3.12-slim --format '{{.Manifest.Digest}}'
FROM python:3.14-slim@sha256:cad9a2c871761c413caa6fdd6441c783451e740a48aaeba60ae62a8b53525ef6

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# The UID is pinned so the Helm chart can assert runAsUser/runAsNonRoot against
# a known identity instead of trusting whatever the base image allocates.
RUN groupadd --system --gid 10001 runtime \
    && useradd --system --uid 10001 --gid runtime --home-dir /app \
       --shell /usr/sbin/nologin runtime

COPY --from=ghcr.io/astral-sh/uv:0.6.3@sha256:8257f3d17fd04794feaf89d83b4ccca3b2eaa5501de9399fa53929843c0a5b55 /uv /uvx /bin/

# uv manages the virtualenv, so the interpreter's bundled pip is never used at
# build or run time. It is removed because its vendored copies of msgpack and
# setuptools are what the image scanner reports, and shipping unreachable
# vulnerable code is still shipping vulnerable code.
RUN python -m pip uninstall --yes pip \
    && rm -rf /usr/local/lib/python*/site-packages/pip \
              /usr/local/lib/python*/site-packages/pip-*.dist-info \
              /usr/local/bin/pip /usr/local/bin/pip3*

COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
COPY README.md ./
COPY alembic.ini ./
COPY migrations ./migrations
RUN uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"

RUN chown -R runtime:runtime /app
USER runtime

# SEC-016: all four entrypoints (api, worker, dispatcher, scheduler) publish a
# heartbeat, so one image-level check is correct for every role. Kubernetes
# overrides this with the chart's own probes; this covers plain `docker run`.
# Skipped when APP_HEARTBEAT_PATH is unset rather than passing vacuously.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD ["sh", "-c", "test -z \"$APP_HEARTBEAT_PATH\" || agent-runtime-healthcheck --max-age 60"]
