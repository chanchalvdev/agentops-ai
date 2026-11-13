# AgentOps AI API image. Models are NOT inside: the container talks to Ollama on the host
# (Apple Silicon GPU acceleration is only available to a native Ollama, not inside Docker).
FROM python:3.13-slim AS base

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

# git and grep are on the workspace executor's allowlist (product.txt 9.1).
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.12.19 /uv /usr/local/bin/uv

WORKDIR /app
# Dependencies first so code changes do not invalidate the dependency layer.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
COPY app ./app
COPY alembic.ini README.md ./
COPY datasets ./datasets
RUN uv sync --frozen --no-dev

# Unprivileged user; the sandboxed workspace is a volume owned by it.
RUN useradd --create-home --uid 10001 agentops \
    && mkdir -p /data/workspace \
    && chown -R agentops /data /app
USER agentops

ENV AGENTOPS_WORKSPACE_ROOT=/data/workspace
EXPOSE 8000
HEALTHCHECK --interval=15s --timeout=3s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=2)"
# Migrations are idempotent; running them at start keeps a fresh database usable.
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn --factory app.main:create_app --host 0.0.0.0 --port 8000"]
