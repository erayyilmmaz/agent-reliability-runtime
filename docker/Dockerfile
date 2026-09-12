FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy

WORKDIR /app

# The UID is pinned so the Helm chart can assert runAsUser/runAsNonRoot against
# a known identity instead of trusting whatever the base image allocates.
RUN groupadd --system --gid 10001 runtime \
    && useradd --system --uid 10001 --gid runtime --home-dir /app \
       --shell /usr/sbin/nologin runtime

COPY --from=ghcr.io/astral-sh/uv:0.6.3 /uv /uvx /bin/
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
