# syntax=docker/dockerfile:1.7

ARG NODE_IMAGE=node:22-bookworm-slim
ARG PYTHON_IMAGE=python:3.12-slim-bookworm
ARG UV_IMAGE=ghcr.io/astral-sh/uv:0.12.0

FROM ${NODE_IMAGE} AS frontend-builder
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm npm ci
COPY frontend/ ./
RUN npm run build

FROM ${UV_IMAGE} AS uv-bin

FROM ${PYTHON_IMAGE} AS runtime

LABEL org.opencontainers.image.title="Work Harness" \
      org.opencontainers.image.description="Controlled multi-agent workflow harness"

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/work-harness-venv \
    PATH="/opt/work-harness-venv/bin:${PATH}" \
    WORK_HARNESS_PROFILE=setup \
    WORK_HARNESS_CHECKPOINT_PATH=/app/var/checkpoints.sqlite \
    WORK_HARNESS_RUN_STORE_PATH=/app/var/runs.sqlite \
    WORK_HARNESS_PROVIDER_STORE_PATH=/app/var/providers.sqlite \
    WORK_HARNESS_SETTINGS_PATH=/app/var/settings.json

COPY --from=uv-bin /uv /uvx /usr/local/bin/

WORKDIR /app

# Install locked third-party dependencies before copying the source for cache reuse.
COPY pyproject.toml uv.lock README.md LICENSE ./
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-install-project

COPY src/ ./src/
COPY config/ ./config/
COPY prompts/ ./prompts/
COPY schemas/ ./schemas/
COPY skills/ ./skills/
RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --locked --no-dev --no-editable

COPY scripts/backup_sqlite.py ./scripts/backup_sqlite.py
COPY scripts/backup_postgres.py ./scripts/backup_postgres.py
COPY --from=frontend-builder /build/frontend/dist/ ./frontend/dist/

RUN useradd --uid 10001 --create-home --home-dir /home/work-harness \
        --shell /usr/sbin/nologin work-harness \
    && mkdir -p /app/var \
    && chown -R work-harness:work-harness /app/var /home/work-harness

USER work-harness

EXPOSE 8000
VOLUME ["/app/var"]
STOPSIGNAL SIGTERM

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=5 \
    CMD ["python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health/live', timeout=3).read()"]

ENTRYPOINT ["work-harness", "--project-root", "/app"]
CMD ["serve", "--host", "0.0.0.0", "--port", "8000"]
