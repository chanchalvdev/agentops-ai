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

## Day 2 — Harness + custom loop + LangGraph (done Sun 25 Oct, branch `day-2-harness-loop-graph`)

### Harness (M1)
- [x] `ExecutionContext`: ids, tenant, limits, counters, permissions, cancel flag, ledger, listeners
- [x] `ExecutionLimits` from settings; request overrides only tighten; unknown keys / non-positive cost rejected
- [x] Policy gate before every model/tool call: cancellation → deadline → budget → permission → arguments
- [x] Hard deadline via `asyncio.timeout`; output tokens capped to remaining budget; unknown cost fails closed
- [x] Typed errors: budget, deadline, cancelled, not permitted, approval required, invalid args, tool failure/timeout, verification
- [x] Bounded corrections per failure kind; only idempotent tools retried; every attempt in the ledger
- [x] All seven §5 demonstrations tested (`tests/unit/test_harness.py`)

### Tools (M2)
- [x] Tool interface (schemas, permissions, timeout, idempotency, approval) + registry/allowlist
- [x] `calculator` (AST allowlist, no eval, exponent/magnitude/depth/length caps)
- [x] `document_search` (hybrid retrieval, tenant from context, `top_k` floor 3 after a live miss)
- [x] `document_metadata` (ownership enforced: other tenants' IDs are "not found")

### Custom loop (M3)
- [x] FINAL (`final_answer` tool or JSON) | TOOL_CALLS; prose never executed; ≤8 steps
- [x] Repeated identical successful calls refused; failed calls may be retried
- [x] Evidence store: stable S#/T# labels, per-document dedupe, fenced + escaped untrusted text
- [x] Bounded history that never splits a tool call from its results; Ollama `num_ctx` set explicitly
- [x] Migration 0002 (`agent_executions`, `execution_events`, `approvals`), idempotent ledger writes
- [x] `mark_for_reindex` atomic conditional UPDATE (Day 1 deferral)

### LangGraph (M4)
- [x] 10 nodes, 5 kinds of conditional edges, every node a harness step
- [x] Postgres checkpointer; budget snapshot restored on resume (pause time excluded from deadline)
- [x] `human_approval` interrupt/resume; rejected calls never execute
- [x] `verify_answer` entailment check (Day 1 deferral) — on qwen3:8b after a probe showed 1.7b has no signal
- [x] Answer schema field order (evidence → citations → answer) and replayed-draft corrections, both from live failures

### API + CLI (M5)
- [x] `POST /agent/run`, `POST /agent/run/stream` (SSE), executions + trace, approvals list/approve/reject
- [x] Every exit path persists the execution (incl. client disconnect → `cancelled`)
- [x] Concurrent approvals resume exactly once (conditional UPDATE; tested with 4 parallel requests)
- [x] `agentops run --mode graph|loop --trace`
- [x] SSE keep-alive every 15 s without events (a live model call took 79 s); `error` event if the run fails after `start`
- [x] Real server smoke test: lifespan opens the Postgres checkpointer, `/ready` ok, SSE calculation run answered `900 seconds [T1]` (verified)

### Verification
- [x] 230 tests (unit + integration on Postgres), `-W error`, ruff, mypy strict clean
- [x] Live qwen3:8b: graph 4/4 (RPO 5 min = 300 s verified; out-of-scope → insufficient_evidence); CLI RTO run answered + verified
- [~] Live loop: grounded 4/4, complete 2/4 (omitted the seconds conversion twice) → Day 3 eval case
- [x] Recorder agent: journals/interviews 02 (harness+tools), 03 (loop), 04 (graph), 05 (API)
- [x] Recorder reviews found 39 issues across M1–M4; the cheap/important ones were fixed with regression tests (see commit messages)

### Deferred (tracked)
- [ ] Router-internal retries and failed calls do not count tokens against the budget → Day 4
- [ ] History trimming can drop a turn whose S# is still citable → Day 4 context work
- [ ] Approval expiry/notification; claimed approval whose resume fails stays "approved" → Day 4 hardening
- [ ] Checkpoints copy the full ledger at every node and keep passage text / tool arguments; no cleanup after a run → Day 4
- [ ] An approved write that crashes before its checkpoint is replayed on recovery (no idempotency key) → Day 3 workspace tools
- [ ] Graph answer prompt has no total evidence cap (fits 8k today: ≤15 passages × 1.5 KB) → Day 4
- [ ] `insufficient_evidence` drafts return model text unverified; verifier output that fails to parse is reported as `unavailable`, not blocked → decide with Day 3 eval data
- [ ] Approval flow is unreachable in production until a write tool exists and tenants can be granted write permissions → Day 3 workspace tools
- [ ] Approve/reject resume synchronously (request lasts the rest of the run); move to 202 + poll/stream → Day 4
- [ ] Approval claimed but resume fails/crashes → approval `approved`, execution stuck `awaiting_approval`; paused save + approval insert are two transactions → Day 4
- [ ] SSE has no event ids / reconnect (a dropped connection cancels the run) → Day 5 UI
- [ ] Cancelled/failed results drop partial evidence from the execution row (the ledger keeps it) → Day 4
- [ ] Model calls are slow under memory pressure (other containers + 8b at 8k ctx) → Day 4 routing/caching

## Day 3 — Workspace tools + observability + evaluation (Fri 23 Oct)
## Day 4 — Optimization, drift, hardening (Sat 24 Oct)
## Day 5 — UI + end-to-end demo (Sun 25 Oct)
