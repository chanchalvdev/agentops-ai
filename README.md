# AgentOps AI

A production-minded agentic RAG platform that runs fully on local, free models. It covers
ingestion, hybrid retrieval, grounded answers with validated citations, and (in progress) an agent
harness, LangGraph workflow, evaluation, drift monitoring, sandboxed workspace tools, and a web UI.

- Product spec: [product.txt](product.txt) · Architecture: [docs/architecture/system-design.png](docs/architecture/system-design.png)
- Progress: [docs/day-checklist.md](docs/day-checklist.md) · Learning notes: [docs/journal/](docs/journal/) · Interview prep: [docs/interview/](docs/interview/)

## Quick start

Prerequisites: Docker, [uv](https://docs.astral.sh/uv/), [Ollama](https://ollama.com) (`brew install ollama`).

```bash
ollama serve &                                   # if not already running
ollama pull qwen3:8b && ollama pull qwen3:1.7b && ollama pull nomic-embed-text

docker compose up -d db                          # PostgreSQL 17 + pgvector 0.8 on localhost:5433
uv sync                                          # install pinned dependencies (uv.lock)
uv run alembic upgrade head                      # create the schema

uv run agentops ingest datasets/corpus           # index the synthetic sample corpus
uv run agentops search "how long are transaction records kept"
uv run agentops ask "What is the RPO in v2 and how does it differ from v1?"

uv run uvicorn --factory app.main:create_app --port 8000   # API + docs at http://localhost:8000/docs
```

In development, two API keys exist out of the box: `dev-key-tenant-a` and `dev-key-tenant-b`.
Set `AGENTOPS_API_KEYS` for anything else (see [.env.example](.env.example)); `AGENTOPS_ENV=prod`
refuses to start without explicit keys and database URL.

```bash
H='X-API-Key: dev-key-tenant-a'
curl -H "$H" -F file=@datasets/corpus/runbook-database-failover.md -F 'metadata={"team":"payments"}' \
     localhost:8000/api/v1/documents
curl -H "$H" -H 'content-type: application/json' localhost:8000/api/v1/rag/answer \
     -d '{"question": "Who is notified when authorization errors exceed 2%?"}'
```

## API (Day 1)

| Method | Path | Purpose |
|---|---|---|
| GET | `/health`, `/ready` | Liveness; readiness checks DB, Ollama, and that configured models are installed |
| POST | `/api/v1/documents` | Upload TXT/MD/PDF (+ optional flat JSON `metadata`). 202 + background indexing; 200 if identical content exists |
| GET | `/api/v1/documents`, `/api/v1/documents/{id}` | List (paginated, `status` filter) / fetch |
| DELETE | `/api/v1/documents/{id}` | Delete document and its chunks |
| POST | `/api/v1/documents/{id}/reindex` | Re-chunk and re-embed with the current configuration |
| POST | `/api/v1/retrieval/search` | `vector` / `keyword` / `hybrid` search with per-result ranks and timing |
| POST | `/api/v1/rag/answer` | Non-agentic baseline: retrieve, answer, validate citations |
| POST | `/api/v1/llm/chat` | Direct bounded model call with usage, cost, latency, attempts |
| GET | `/api/v1/models` | Model per role and whether it is installed |

Errors always look like `{"error": {"code", "message", "request_id"}}`; validation errors add
`details` with field locations only (submitted values are never echoed).

## How it works

```
upload ─► validate (size, extension, magic bytes, UTF-8) ─► extract (PDF pages kept as \f) ─► normalize
       ─► store (idempotent on tenant+sha256, versioned by filename) ─► 202
background ─► chunk (structured: paragraph/heading-aware, or fixed) ─► embed ("search_document:" prefix)
           ─► one transaction: replace chunks + mark READY + demote older versions   (else FAILED, 0 chunks)

query ─► embed ("search_query:") ─► pgvector HNSW cosine  ┐
      ─► websearch_to_tsquery (AND→OR, -negations as NOT) + ts_rank_cd       ┴► RRF (k=60) ─► dedupe ─► top-k
answer ─► evidence within char budget ─► fenced as untrusted <source id="S1"> blocks ─► qwen3:8b with
          JSON-schema constrained output ─► citations checked against retrieved IDs ─► ≤1 correction
```

Key decisions (details in [docs/journal/](docs/journal/)):

- **Tenant isolation in one place**: every retriever builds on `scoped_select`, which filters both
  the chunk and its document by tenant. Cross-tenant reads return 404, not 403.
- **Never silently partial**: chunks and the READY flag commit in one transaction.
- **RRF over score blending**: cosine similarity and `ts_rank_cd` live on incomparable scales.
- **No evidence, no model call**: cheaper and cannot hallucinate. Until a relevance threshold
  exists (Day 4) this only fires for empty tenants or filters matching nothing; otherwise the
  model itself must report `insufficient_evidence`.
- **Fallback only for availability**: retries and the optional hosted fallback trigger on
  transient errors only (connect/timeout/5xx/429), never on bad output.

## Development

```bash
uv run pytest                 # unit + integration (needs `docker compose up -d db`; uses agentops_test DB)
uv run pytest -m live         # opt-in checks against the real Ollama models
uv run ruff check app tests && uv run ruff format --check app tests && uv run mypy app
```

Integration tests create `agentops_test`, run Alembic `downgrade base` + `upgrade head`, and
use a deterministic fake embedder and fake LLM. CI (`.github/workflows/ci.yml`) runs the same
suite with a pgvector service container and fails if the database is missing.

## Models

Chosen for an Apple M4 with 16 GB (see product.txt §23): `qwen3:8b` (answers, tools), `qwen3:1.7b`
(classification/routing), `nomic-embed-text` (768-d). Measured on this machine: qwen3:8b ≈ 16
tok/s, qwen3:1.7b ≈ 52 tok/s; a grounded answer takes ~6–17 s. Cost for local models is
reported as `0` with `pricing_source: "local"`; hosted models without a configured price report
`null` / `"unavailable"`, never a guess.

## Troubleshooting

- `/ready` returns `ollama: unreachable` → run `ollama serve`; `missing_models` → `ollama pull <name>`.
- Integration tests skipped → start the database: `docker compose up -d db`.
- Port 5433 in use → change the host port in `docker-compose.yml` and `AGENTOPS_DATABASE_URL`.

## Known limitations (Day 1)

- Background indexing uses FastAPI `BackgroundTasks` (in-process): a crash mid-indexing leaves the
  document `processing`. A durable queue is a later hardening step.
- No relevance threshold: vector search always returns its nearest neighbours, even weak ones.
  The threshold will be tuned against the evaluation set (Day 4), not guessed.
- Scanned PDFs (no text layer) are rejected; there is no OCR.
- Changing the embedding model's dimension needs a migration and a full reindex.
- No rate limiting yet (Day 4).
- Indexing is fail-closed: a document is unsearchable while reindexing, a failed reindex drops its
  previous chunks, and deleting the latest version does not re-promote the previous one.
- Re-uploading an older version's exact bytes returns that document as a duplicate; it does not
  become latest again (call reindex). The same applies to a FAILED document's bytes.
