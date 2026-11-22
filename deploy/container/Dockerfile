# AgentOps AI, hosted image (Azure Container Apps; any container host works): the API plus the
# built web UI in one container. Chat models are hosted (OpenAI-compatible API, e.g. Groq);
# embeddings run in-process (fastembed, nomic-embed-text v1.5: the same vectors Ollama produces).
# Postgres is external (e.g. Neon). Build from the repo root:
#   docker build --platform linux/amd64 -f deploy/container/Dockerfile .

# --- web UI ---------------------------------------------------------------------------------
FROM node:22-slim AS web
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN npm ci --no-audit --no-fund
COPY web/ ./
RUN npm run build

# --- API --------------------------------------------------------------------------------------
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    PATH="/opt/venv/bin:$PATH"

LABEL org.opencontainers.image.source="https://github.com/chanchalvdev/agentops-ai" \
      org.opencontainers.image.description="AgentOps AI: agentic RAG API and web UI"

RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.12.19 /uv /usr/local/bin/uv

WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --extra hosted --no-install-project
COPY app ./app
COPY alembic.ini ./
COPY datasets ./datasets
RUN uv sync --frozen --no-dev --extra hosted
COPY --from=web /web/dist ./web/dist

# Bake the embedding model into the image so a cold start never downloads it.
ENV AGENTOPS_FASTEMBED_CACHE_DIR=/opt/models
RUN python -c "from fastembed import TextEmbedding; TextEmbedding('nomic-ai/nomic-embed-text-v1.5', cache_dir='/opt/models')"

RUN useradd --create-home --uid 1000 user \
    && mkdir -p /data/workspace \
    && chown -R user /data /app /opt/models
USER user

# Hosted defaults. Secrets (database URL, model API keys, client API keys) come from the host's
# secret store at runtime; any of these can be overridden by an environment variable there.
ENV AGENTOPS_ENV=prod \
    AGENTOPS_LLM_PROVIDER=openai \
    AGENTOPS_LLM_BASE_URL=https://api.groq.com/openai/v1 \
    AGENTOPS_LLM_MODEL_MAIN=openai/gpt-oss-120b \
    AGENTOPS_LLM_MODEL_SMALL=qwen/qwen3.8-27b \
    AGENTOPS_LLM_MODEL_MAIN_ALTERNATES=openai/gpt-oss-20b,qwen/qwen3.8-27b \
    AGENTOPS_LLM_MODEL_SMALL_ALTERNATES=openai/gpt-oss-20b \
    AGENTOPS_LLM_JSON_MODE=schema \
    AGENTOPS_LLM_REASONING_EFFORT=low \
    AGENTOPS_LLM_MAX_RETRIES=3 \
    AGENTOPS_EMBEDDING_PROVIDER=fastembed \
    AGENTOPS_EMBEDDING_MODEL=nomic-embed-text \
    AGENTOPS_WORKSPACE_ROOT=/data/workspace \
    AGENTOPS_WORKSPACE_EXEC_BACKEND=disabled \
    AGENTOPS_WEB_DIST_DIR=/app/web/dist \
    AGENTOPS_INDEXING_POLL_S=1800 \
    AGENTOPS_AGENT_MAX_EVIDENCE_PASSAGE_CHARS=16000 \
    AGENTOPS_TELEMETRY_CAPTURE_IO=false \
    AGENTOPS_RATE_LIMIT_AGENT_PER_MIN=5 \
    AGENTOPS_AGENT_RULE_ROUTING=true \
    AGENTOPS_CACHE_QUERY_EMBEDDINGS=true \
    AGENTOPS_CACHE_TOOL_RESULTS=true

EXPOSE 8000
HEALTHCHECK --interval=30s --timeout=5s --retries=5 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/health', timeout=3)"
CMD ["sh", "-c", "alembic upgrade head && exec uvicorn --factory app.main:create_app --host 0.0.0.0 --port 8000 --proxy-headers --forwarded-allow-ips='*'"]
