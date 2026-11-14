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

## Day 4 — Optimization, drift, hardening (done Fri 13 Nov, branch `day-4-optimization-drift-hardening`)

### Optimization (M9)
- [x] Graph switches resolved once into `AgentTuning` (all default to Day 3 behaviour): query rewrite, search top_k, evidence cap, rule routing, small-model drafts
- [x] Rule routing (`policies.py`, written on 30 Oct, now wired): code decides `needs_tools`; calculator nudge when a calculation question got no calculator call
- [x] Total evidence cap in answer/tool prompts; only shown passages are citable (Day 2 deferral)
- [x] `insufficient_evidence` drafts return fixed text, never unverified model claims (Day 2/3 deferral, eval case `tool-rto-diff`)
- [x] Caches: query embeddings (tenant+model+text), tool results (tenant+permissions+args+corpus version+retrieval config), semantic answer cache (off; verified read-only graph answers only; threshold 0.97; scope includes corpus version + config fingerprint)
- [x] Corpus version read from the database, so a CLI ingest in another process invalidates the API's caches
- [x] Usage accounting from the ledger: retries, failed calls, embedding calls, cache hits, hosted-equivalent cost (only with configured reference prices, labelled hypothetical)
- [x] Eval aggregates: cost/request, cost/successful task, tokens/request and /success, retry rate, cache hits, judge quality score; per-case cost + usage persisted (migration 0005, Day 3 deferral)
- [x] Experiment profiles (`datasets/experiments.json`, allowlisted overrides only), runner, benchmark verdict, CLI + API
- [x] Relevance threshold measured, not adopted: top vector score overlaps (unanswerable up to 0.766, answerable down to 0.651)
- [x] Live benchmark ([docs/benchmarks/day4.md](benchmarks/day4.md)): **`optimized` accepted** — 36/38 vs 33/38, no newly failing case, tokens/request −21%, qwen3:8b calls/request 3.03 → 2.76, p50 43.1 → 27.5 s (−36%), p95 65.4 → 60.0 s (−8%), tool_use 2/5 → 5/5
- [x] Rejected by the gate and kept as evidence: query rewrite off (`optimized-no-rewrite`, multi_document 4/5 → 2/5); rule routing that also skipped comparisons (`multi-availability` 0/3 vs 3/3) → comparisons keep the tool-selection step
- [x] Temperature 0 makes quality deterministic (baseline re-run 23 h later: identical answers and tokens); latency is not (same baseline p50 60.6 s vs 43.1 s), so the compared runs ran back to back
- [x] Committed baselines: `golden-v1-graph-qwen3-8b-day4.json` (baseline), `-optimized.json`, `retrieval-golden-v1.json`; monitoring defaults point at them

### Drift and monitoring (M10)
- [x] Retrieval-only evaluation of the golden set: hit@k, recall@k, MRR, source distribution, and **answer coverage@k** (file-level metrics saturate on this 5-document corpus)
- [x] Deterministic detector over quality, retrieval, data, and operational signals; every breach labelled `measured` or `suspected`; no data = `insufficient_data`, never "ok"
- [x] `drift_checks` + `monitoring_alerts` tables (migration 0005), thresholds in `datasets/drift_thresholds.json`
- [x] API: `GET /api/v1/metrics`, `GET /monitoring/summary`, `GET /monitoring/drift`, `GET /monitoring/alerts`, `POST /monitoring/check`; CLI `agentops drift check` (exit 1 on measured drift), `agentops metrics`
- [x] Answer coverage counts only passages from the expected documents (a "15 minutes" match in the failover runbook was making `multi-rpo-change` look covered; found while explaining the rejected no-rewrite profile)
- [x] Mixed embedding/chunking signals are per tenant (the experiment's `eval_degraded_chunks` tenant legitimately uses another chunking config)
- [x] Live demo (`reports/drift-degraded.txt`): degraded-chunking vs the Day 4 baseline → **drift**, exit 1: pass rate 0.868 → 0.816 (measured), answer coverage 0.971 → 0.882 (measured, message names the changed chunk/retrieval config); recall@k and hit rate stay 1.0, as predicted
- [x] Operational window also flagged tokens/execution 751 → 3,516: the previous 24 h was the provider outage (83% failures, 52% provider errors), so this is recovery, not a regression — the detector reports what changed, a human reads why

### Hardening (M11)
- [x] Rate limiting: per-tenant token buckets per route class, 429 + `Retry-After` (in-process; documented)
- [x] Operator-only (admin tenants) evaluations, experiments, and monitoring (Day 3 deferral "any tenant can start a run")
- [x] Dockerfile (non-root, healthcheck, migrations at start) + compose `api` profile; CI adds a docker build job and gates the committed benchmark
- [x] Image verified locally: `docker build` OK (919 MB), `agentops --help` runs as uid 10001; `docker compose --profile app up -d api` runs migrations, `/health` ok, `/ready` reports database ok and Ollama unreachable when Ollama listens on loopback only (documented)
- [x] CI gates both committed live runs: Day 3 baseline self-check and `optimized` vs the Day 4 baseline (passes)

### Verification
- [x] Recorder review fixes: drift status `insufficient_data` when nothing could be measured (was `ok`); `/retrieval/search` rate limited (each query embeds)
- [x] 370 tests (`-W error`), ruff, ruff format, mypy strict clean
- [x] Live runs on qwen3:8b / qwen3:1.7b (Ollama on :11435; :11434 belongs to another project's container without these models)

### Deferred (tracked)
- [ ] Caches showed ~0 hits in the benchmark (each golden question runs once); their value is for repeated questions and is shown by unit/integration tests only, not measured live
- [ ] `multi-retention` (both profiles) and `ws-edit-approved` (false "could not" after a retried edit) still fail
- [ ] Loop-mode baseline still not run (graph only)
- [ ] Rate limits and caches are per process (Redis for multiple replicas); the WebSocket terminal is not rate limited
- [ ] Relevance floor stays off: the measured score ranges overlap
- [ ] Drift detector: no newly-failing-case rule (a one-case regression only shows in the benchmark/eval gate); alerts cannot be acknowledged yet; the default eval head can be an experiment run
- [ ] Compose `api` defaults Ollama to host :11434 (on this machine that is another project's Ollama); CI gates committed reports only, it never runs the models
- [ ] Benchmark latency is sensitive to machine state (swap reached 9 GB with another project's containers running); token and call counts are the machine-independent signal

## Day 4 — original plan (Sat 24 Oct)
## Day 5 — Web UI + end-to-end demo (branch `day-5-ui-demo`, uncommitted)

### API for the UI (M12)
- [x] `GET /api/v1/executions` (newest first, `limit`, `status`; tenant-scoped) for the Executions screen
- [x] `GET /api/v1/documents/{id}/chunks/{chunk_id}`: open a citation's source passage; 404 across tenants (same as missing)
- [x] Operators see every evaluation run (runs started from the CLI are recorded as `cli` and were invisible in the API); `requested_by` in the run list
- [x] 3 integration tests; 373 tests pass, ruff + mypy strict clean

### Web UI (M13)
- [x] React 18 + TypeScript (strict) + Vite 5 + Tailwind 3 in `web/`, no other runtime dependencies; Vite proxies `/api` (incl. WebSocket), so no CORS change
- [x] Chat: SSE over POST (fetch + incremental parser), live step timeline, answer with clickable `[S#]` citations (source chunk drawer) and `[T#]` tool results, graph/loop mode, link to Approvals when paused
- [x] Documents: upload with metadata, polling while indexing, reindex, delete
- [x] Workspace: lazy file tree, viewer/editor, terminal over the sandbox WebSocket (refusals in red), audit log
- [x] Approvals: diff preview against the current file (new file, edit, or "the tool will refuse"), approve/reject with reason, resumed answer
- [x] Executions: list + ledger timeline with latency bars, tokens, cache hits
- [x] Evaluations & Monitoring: runs, category/evaluator scores, failing cases, compare (regression gate), drift signals, alerts, run check now; non-operators get an explanation, not an error page
- [x] Unit tests (SSE parser, line diff, citation splitting), strict type-check, production build (193 KB JS, 60 KB gzip); CI `web` job
- [x] Driven end to end in Chrome against the live API and models: cited answer + source drawer; workspace edit rejected after the diff exposed a duplicated line; new file approved and written; terminal refusals (allowlist, path jail, no shell); run comparison 87% → 95%; drift check; tenant_b sees 403s
- [x] Bugs found that way and fixed: terminal effect crashed React ("destroy is not a function") → explicit effects + a per-screen error boundary; dev double-mount leaked a "closed" line → handlers detached on cleanup; token total read a Python-only property; operational stats read the wrong (nested) keys; operator run list empty (above)

### Demo + docs (M14)
- [x] `scripts/demo.sh`: the full flow through the public API (corpus, cited answer, source passage, trace, approval, sandbox refusals, tenant isolation, drift check)
- [x] `APPROVE=1 scripts/demo.sh` run live (`reports/demo-run.log`): cited 300 s answer, verified; source passage; 17-event trace; write approved and on disk; 3 sandbox refusals; tenant_b gets 404; drift check. Found that upload returns `{document, duplicate}` (script and UI type fixed; the UI now says "already uploaded")
- [x] `docs/demo.md`: the UI walkthrough; README: Web UI section, new endpoints, troubleshooting (port 8000 taken, Ollama killed under memory pressure), known limitations
- [x] Loop-vs-graph comparison measured at last (Day 3 deferral; [docs/benchmarks/loop-vs-graph.md](benchmarks/loop-vs-graph.md)): on the same 33 cases the loop passes 1/33 strictly, 24/33 without `structured_output` (graph 29/33 baseline, 32/33 optimized). qwen3:8b answers in prose before `final_answer` in 31/33 loop runs; the loop uses 44% more tokens than the graph baseline

### Deferred / known limitations
- [ ] An evaluation run killed with its process (machine sleep) stays `running`; only a graceful cancel marks it failed
- [ ] Drift check's default retrieval head is the latest report, which can be an experiment probe
- [ ] UI: API key in local storage is the only credential; terminal is one command at a time; no mobile layout
