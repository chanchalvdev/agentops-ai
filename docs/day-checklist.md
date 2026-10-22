# Day Checklist

Plan: product.txt §18 (5 days, Wed 2026-10-21 → Sun 2026-10-25). `[x]` = implemented **and**
verified by a test or a recorded live run; `[~]` = partial; `[ ]` = not done.

---

## Day 1 — Foundation + LLM + RAG (Wed 21 / Thu 22 Oct)

### Environment
- [x] Machine inventoried: Apple M4, 16 GB, Docker 29.6, Python 3.13, uv 0.12
- [x] Ollama 0.40.7 installed; `qwen3:8b`, `qwen3:1.7b`, `nomic-embed-text` pulled
- [x] Models benchmarked on this machine (8b ≈ 16 tok/s, 1.7b ≈ 52 tok/s, both 100% GPU)
- [x] System design diagram: `docs/architecture/system-design.png`
- [x] product.txt revised (5-day plan, Ollama, sandboxed workspace, UI, Recorder agent)

### Backend foundation
- [x] uv project, dependencies pinned in `uv.lock`, ruff + mypy (strict) + pytest configured
- [x] Docker Compose: PostgreSQL 17 + pgvector 0.8.1 (host port 5433)
- [x] Typed settings (`AGENTOPS_*`), prod refuses to start without API keys / DB URL
- [x] Typed error hierarchy with `transient` flag driving retry decisions
- [x] Structured JSON logs with request ID + tenant, secret redaction (keys, bearer, DSN passwords)
- [x] Consistent error envelope; validation errors never echo submitted values
- [x] API-key auth → tenant; hashed key storage, constant-time compare
- [x] Alembic migration 0001 (async env); tested downgrade→upgrade on a clean DB every test run
- [x] `/health`, `/ready` (DB + Ollama + required models)

### LLM layer
- [x] Provider-neutral contracts (messages, tools, JSON schema, usage, latency)
- [x] Ollama provider (structured output, tool calls, `think=false`)
- [x] OpenAI-compatible provider for optional hosted fallback (mock-tested; **no live hosted call verified**)
- [x] Fake provider for tests/CI
- [x] Router: role → model, bounded retry with full-jitter backoff, fallback on transient errors only
- [x] Cost accounting: local = $0 (`local`), unknown hosted = `unavailable` (never invented)
- [x] `POST /api/v1/llm/chat` with usage/cost/latency/attempts — verified live (1.7b, JSON schema, 1.7 s)

### RAG
- [x] Upload validation: size limit, extension allowlist, PDF magic bytes, binary/NUL rejection, UTF-8
- [x] Extraction: TXT, Markdown, PDF (pages preserved), normalization (NFKC, control + zero-width chars stripped)
- [x] Idempotent ingestion (tenant + sha256), filename versioning, older versions demoted only when new one is READY
- [x] Chunking: `structured` (heading/paragraph aware, multi-section labels, exact offsets, overlap) and `fixed` baseline
- [x] Stable chunk IDs (uuid5 of document + config + index + text hash)
- [x] Embeddings via Ollama with nomic task prefixes, batching, dimension/NaN validation, retries
- [x] Atomic indexing: never READY with missing embeddings; failures → FAILED with 0 chunks
- [x] Vector search (HNSW cosine, iterative scan for filtered queries)
- [x] Keyword search (GIN `tsvector`, OR-matching — fixed after live run showed 0 keyword hits)
- [x] Hybrid RRF (k=60), text-hash dedupe, document-ID + metadata filters, tenant isolation
- [x] Grounded answers: fenced untrusted evidence, JSON-schema output, citation validation, ≤1 correction, no-evidence short-circuit
- [x] Document CRUD + reindex API, retrieval API, answer API
- [x] CLI: `agentops ingest | search | ask`
- [x] Synthetic corpus with deliberate cross-document conflicts (`datasets/corpus/`)

### Verification
- [x] 107 tests passing (unit + integration on real Postgres/pgvector), 3 live Ollama tests passing, no warnings
- [x] ruff, ruff format, mypy strict: clean
- [x] Live end-to-end: v1 vs v2 RPO answered with both docs cited; out-of-scope question → `insufficient_evidence`; PDF answer cites page; tenant B sees 0 results
- [x] CI workflow written (`.github/workflows/ci.yml`) — **not yet run on GitHub** (no remote)
- [x] README with setup, API, troubleshooting, limitations
- [x] Recorder agent: `docs/journal/01-foundation-rag.md` (264 lines), `docs/interview/01-foundation-rag.md` (42 Q&A)
- [x] Recorder review found 5 gaps; 2 reproduced with failing tests and fixed (tenant ContextVar in sync dependency; `-negation` inverted by OR rewrite)

### Deferred (tracked, not forgotten)
- [ ] Vector relevance threshold → Day 4, tuned on eval set
- [ ] Durable background queue (BackgroundTasks is in-process) → Day 4 hardening
- [ ] Rate limiting → Day 4
- [ ] Reranker interface → only if eval shows ranking errors RRF does not fix
- [ ] Reindex/delete availability: keep serving the previous index during reindex; re-promote the prior version on delete → Day 4
- [ ] Re-upload of an older version's bytes should make it latest (today: duplicate no-op) → Day 4
- [ ] `mark_for_reindex` is check-then-update; make it an atomic conditional UPDATE → Day 2 (with executions)
- [ ] Citations are checked for existence, not entailment → Day 2 `verify_answer` node

---

## Day 2 — Harness + custom loop + LangGraph (Thu 22 Oct)
- [ ] Execution context, budgets, typed harness errors, policy gate
- [ ] Tool interface + registry; document_search, calculator (safe AST), document_metadata
- [ ] Custom agent loop (FINAL | TOOL_CALLS, ≤8 steps) + execution ledger
- [ ] LangGraph workflow, checkpointing, human_approval interrupt/resume
- [ ] SSE streaming, approvals API, executions API
- [ ] Recorder agent run

## Day 3 — Workspace tools + observability + evaluation (Fri 23 Oct)
## Day 4 — Optimization, drift, hardening (Sat 24 Oct)
## Day 5 — UI + end-to-end demo (Sun 25 Oct)
