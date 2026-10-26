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

## Day 3 — Workspace tools + observability + evaluation (done Mon 26 Oct, branch `day-3-workspace-observability-eval`)

### Workspace tools (M6)
- [x] Per-tenant path jail: relative only, no `..`, realpath before containment (symlink escape refused), deny-list (`.env*`, keys, `.ssh/`, `.git/config`; `.git/` read-only)
- [x] Six tools: `workspace_list/read/search` (read-only), `workspace_write/edit` (always approval), `workspace_exec` (approval unless read-only command)
- [x] Executor: argv only, per-command option allowlist, path args jailed, scrubbed env, timeout kills process group, output cap, git cannot escape to an enclosing repo
- [x] `pytest --collect-only` treated as exec (collection runs code) — deliberate deviation from §9.1
- [x] Write-ahead idempotency receipts (approved write replayed after a crash is not applied twice) — closes the Day 2 deferral
- [x] Graph: `workspace_task` intent, bounded tool rounds (4), repeat of the last side-effecting call refused
- [x] API: tree/file/search/PUT file/exec/audit + WS terminal (same executor and policy); tenant permissions from config (dev: tenant_a)
- [x] `workspace_audit` table (migration 0003): every operation incl. API/terminal refusals; agent calls also in `execution_events`
- [x] CLI: `agentops approvals | approve | reject` — closes "approval flow unreachable" deferral
- [x] Live (qwen3:8b): read → answered + verified; edit paused, approved from a new process, applied once; identical re-proposal refused by the guard
- [x] Live fixes: citations constrained to shown IDs (enum schema); workspace reports never `insufficient_evidence`; write results state `outcome`

### Observability (M7)
- [x] Trace built from the ledger: node/turn spans, generations (model, tokens, latency), tool spans with embed/vector/keyword child spans, events
- [x] Correlation id = execution id; deterministic observation ids (re-export after resume upserts)
- [x] Sinks: local log (always), Langfuse (ingestion API), LangSmith (runs/feedback; eval-only by default); fire-and-forget with timeout and backpressure
- [x] Redaction + `telemetry_capture_io=false`; passage text never exported
- [~] Langfuse and LangSmith verified with mocked HTTP only — **no live project/credentials**

### Evaluation (M8)
- [x] `datasets/golden_questions.jsonl` golden-v1: 38 cases (11 answerable, 5 multi-document, 5 unanswerable, 5 tool use, 7 adversarial, 5 workspace) + prompt-injection corpus doc + seeded eval workspace
- [x] 11 deterministic evaluators (status, citations, sources, required citations, structured output, tool args/results, forbidden tools, approval gate, steps, answer content, workspace effects)
- [x] Optional LLM judge (correctness, groundedness, relevance, completeness), advisory only
- [x] Runner as the `eval` tenant through AgentService; plays the human for approvals; scores pushed to telemetry
- [x] Migration 0004: evaluation_datasets/cases/runs/results; API `POST /evaluations/run` (202), runs, run detail, compare
- [x] CLI `agentops eval run | compare | check`; thresholds in `datasets/eval_thresholds.json`; CI gate on the committed baseline
- [x] Live baseline (qwen3:8b, graph, judge on): **33/38 = 87%** — `datasets/baselines/golden-v1-graph-qwen3-8b.json`, run `4ec91948`.
  answerable 11/11, unanswerable 5/5, adversarial 7/7, multi-document 4/5, workspace 4/5, tool use 2/5;
  citation validity, source retrieval, structured output, unauthorized tools, workspace effects all 100%;
  p50 28 s, p95 49 s per case; judge correctness 0.95, groundedness 0.91 (advisory)
- [x] Same 5 failures in two consecutive full runs: calculator skipped (model does arithmetic itself) ×3,
  `multi-retention` → insufficient_evidence, `ws-edit-approved` misreported as failed
- [x] Run 1 (32/38) exposed a runner bug: `adv-secret-file` fixture `.env` was seeded through the agent's jail and refused → fixed + test
- [x] Gate: `agentops eval check` passes on the baseline; exits 1 on a hand-edited copy where one injection case executed a write (`unauthorized_tools` −16.7%, newly failing `adv-injected-doc`) — a synthetic demo, not a real model regression
- [x] After the M8 Recorder review the gate also refuses runs with a different mode or case filter, treats any pass→fail flip (`max_newly_failing: 0`) and any crashed case as a breach
- [~] CI step only applies the absolute floor to the committed baseline (no model in CI); a new live run must be gated locally with `--baseline`

### Verification
- [x] 323 tests (unit + security + integration on Postgres incl. migrations 0003/0004 down/up), `-W error`, ruff, mypy strict clean
- [x] Recorder: journals/interviews 06 (workspace), 07 (observability), 08 (evaluation + Day 3 retrospective); 06/07 have "fixed after review" sections
- [x] Recorder reviews found real defects, fixed with regression tests: edit replay crash window, unaudited API refusals, audit failure surfacing as tool error, guard refusing reads/re-runs, GNU `grep -R`, terminal non-JSON frames, permissions frozen in checkpoints, span order, capture_io leaks, resumed traces without input

### Deferred (tracked)
- [ ] After a failed first attempt + successful retry, the model may report a workspace action as failed (eval case `ws-edit-approved`) → Day 4
- [ ] Workspace: not an OS sandbox (container for exec), per-tenant all-or-nothing permissions, receipt cleanup, terminal disconnect detection during silent commands → Day 4 hardening
- [ ] Observability: `_trace` skipped on ConflictError / save failure; 500-event cap unmarked; `GET /api/v1/metrics` → Day 4
- [ ] Relevance threshold and model routing tuned against the eval set → Day 4
- [ ] Tool-use 2/5: graph does arithmetic without the calculator; `tool-rto-diff`/`multi-retention` say insufficient evidence → Day 4 (eval-driven)
- [ ] Loop-mode baseline not run yet (graph only) → Day 4 loop-vs-graph comparison
- [ ] Day 2 deferral confirmed by eval data: `insufficient_evidence` drafts skip verification (`tool-rto-diff` returned an unverified "45 minutes" claim) → Day 4
- [ ] Eval: substring checks are loose ("pass" matches "did not pass"); two adversarial cases pass vacuously with 0 tool calls; per-case cost not persisted; RAG metrics (recall@k, MRR) missing; run lock is per process; any tenant can start a run → Day 4
- [ ] Judge sees 300-char snippets only and cannot see workspace state (5/5 on the false `ws-edit-approved` report) → advisory only, Day 4
- [ ] `datasets/corpus` now includes the prompt-injection doc, so `agentops ingest datasets/corpus` loads it for tenant_a too (intended as an adversarial fixture)

## Day 4 — original plan (Sat 24 Oct)
## Day 5 — UI + end-to-end demo (Sun 25 Oct)
