# syntax=docker/dockerfile:1
# Lead Intelligence Engine - application image (API + dashboard, scheduler, migrations, CLIs).
# Reproducible: dependencies come only from uv.lock (--frozen), no dev dependencies.
# No secret is copied into any layer (.dockerignore excludes .env and secrets/); configuration
# arrives at run time through environment variables / mounted files.

FROM python:3.13-slim-bookworm AS builder
COPY --from=ghcr.io/astral-sh/uv:0.11.26 /uv /bin/uv
ENV UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PYTHON_DOWNLOADS=never
WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

FROM python:3.13-slim-bookworm AS runtime
RUN groupadd --system --gid 10001 app \
 && useradd --system --uid 10001 --gid app --home-dir /app --shell /usr/sbin/nologin app
WORKDIR /app
COPY --from=builder /app/.venv /app/.venv
COPY alembic.ini pyproject.toml ./
COPY app ./app
COPY scripts ./scripts
COPY evaluation ./evaluation
ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONPATH=/app \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    LOG_FORMAT=json
USER app
EXPOSE 8000
STOPSIGNAL SIGTERM
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD ["python", "-c", "import sys, urllib.request; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3).status == 200 else 1)"]
# uvicorn handles SIGTERM/SIGINT: in-flight requests finish, running pipeline tasks are cancelled
# and recorded (see app/main.py lifespan). Logs go to stdout/stderr.
CMD ["python", "-m", "uvicorn", "app.main:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--no-server-header"]
