# Five-Day Learning Journal — AgentOps AI

product.txt §21 deliverable 14: "a five-day learning journal listing concepts implemented,
experiments run, and trade-offs learned". Synthesized by the Recorder agent from
`docs/journal/01`–`14` and `docs/day-checklist.md`. Every number below comes from a test run, a
stored report or a log named in the milestone journal it links to; nothing here is new
measurement.

Setup for every live number: Apple M4, 16 GB, Ollama 0.40.7, `qwen3:8b` (main), `qwen3:1.7b`
(small), `nomic-embed-text` (768-dim), Postgres 17 + pgvector 0.8.1, temperature 0.

## The calendar, honestly

The plan (product.txt §18) was five consecutive days, Wed 21 → Sun 25 Oct. The work kept the five
**days** as five scopes, but the calendar ran longer:

| Plan | Scope | Actually done | Code commit |
|---|---|---|---|
| Day 1 — Wed 21 | Foundation, LLM layer, RAG | 21–22 Oct | `dab7354` … `73c2e67` (22 Oct, PR #1) |
| Day 2 — Thu 22 | Harness, tools, custom loop, LangGraph, agent API | finished Sun 25 Oct | `a92de84` … `8f648aa`, `b497479` |
| Day 3 — Fri 23 | Workspace tools, observability, evaluation | Mon 26 Oct | `468a3c2` (26 Oct) |
| Day 4 — Sat 24 | Optimization, drift, hardening | finished Fri 13 Nov | `4e28591` (13 Nov) |
| Day 5 — Sun 25 | Web UI, demo, final docs | 13–14 Nov | `cff28ed` (14 Nov) |

Day 4 took the longest in wall-clock time: the live benchmark and drift runs on one shared 16 GB
machine needed about two days on their own (sleep killed two runs, and another project's Ollama on
the default port failed every call). Day 3 and Day 4 were first one uncommitted working tree; they
were committed afterwards as separate, back-dated commits, Day 3 reconstructed by stripping the
Day 4 changes and verified at its own commit (323 tests, ruff, mypy, the eval gate).

Tests at the end of each day: **107 → 230 → 323 → 370 → 373** (unit, security and integration on
real Postgres, `-W error`), plus 6 web unit tests on Day 5.

---

## Day 1 — Foundation, LLM layer, RAG ([journal 01](01-foundation-rag.md))

### Concepts implemented
- **Typed settings that fail closed in prod** (no API keys or DB URL → refuse to start); typed
  error hierarchy with a `transient` flag that drives retries; structured JSON logs with redaction.
- **API-key auth → tenant**, hashed keys, constant-time compare; tenant isolation in every query,
  with `tenant_id` on both documents and chunks; 404 (not 403) across tenants.
- **Provider-neutral LLM layer**: Ollama, an OpenAI-compatible fallback (mock-tested only) and a
  fake provider; a router with bounded jittered retry and fallback only on transient errors; cost
  `local` = $0 and unknown hosted cost = `unavailable`, never invented.
- **Ingestion**: validation (size, extension, PDF magic bytes, binary), normalization (NFKC,
  zero-width stripped), idempotency by tenant + sha256, filename versioning, structured and fixed
  chunkers, deterministic chunk ids (uuid5), atomic indexing.
- **Hybrid retrieval**: HNSW cosine vector search, GIN `tsvector` keyword search, RRF (k = 60).
- **Grounded answers**: fenced untrusted evidence, JSON-schema output, citation validation with one
  correction, no model call when there is no evidence.

### Experiments run
- Model speed on this machine: qwen3:8b ≈ 15.7 tok/s, qwen3:1.7b ≈ 52.5 tok/s, both on GPU.
- "RPO in v2 vs v1": correct, both documents cited, ~17 s; out-of-scope question →
  `insufficient_evidence`; a PDF answer cited its page; tenant B search on tenant A's content → 0.
- **Keyword search returned 0 candidates** for natural questions (seen in live diagnostics):
  `websearch_to_tsquery` ANDs every term. Rewritten to OR.
- Probes: qwen3:8b emitted `calculator("17% of 2340")` — tool arguments must be validated by code.

### Trade-offs learned
- RRF over score blending: ranks need no common scale; blending needs calibration data.
- pgvector over a dedicated vector DB: one database, transactions with the metadata, enough at this
  scale.
- Deterministic chunk ids cost one hash and keep citations and eval ground truth stable.
- Skipping the model on empty evidence is a guarantee, not an optimization — but it only fires on
  truly empty retrieval (the relevance floor was later measured and rejected, Day 4).

---

## Day 2 — Harness, tools, custom loop, LangGraph, agent API ([journals 02](02-harness-tools.md)–[05](05-agent-api.md))

### Concepts implemented
- **The harness**: every model and tool call passes one policy gate — cancellation → deadline →
  budget → permission → arguments — with a hard deadline, output tokens capped to the remaining
  budget, unknown cost failing closed under a cost cap, bounded corrections per failure kind, and
  an append-only **ledger** of every attempt.
- **Tools as data**: schemas, permissions, timeout, idempotency, approval flag; a registry; a
  calculator built on an AST allowlist (no `eval`).
- **A custom agent loop**: final answer or tool calls, never prose; ≤ 8 steps; repeated identical
  calls refused; evidence with stable `S#`/`T#` labels.
- **LangGraph workflow**: ten nodes with code-decided edges, small model for classify/plan, main for
  tools/answer/verify, a semantic **`verify_answer`** step, Postgres checkpointer, `interrupt`-based
  **human approval**.
- **Agent API**: run, SSE stream (with keep-alive), executions, trace, approvals; every exit path
  persists the execution; concurrent approvals resume exactly once.

### Experiments run
- **`top_k = 1`** from the model missed v2 → floor of 3.
- **Schema field order**: answer-first produced correct but uncited answers **3/3**; at temperature 0
  the retry reproduced the identical draft. Citations-first + replaying the rejected draft as an
  assistant turn → cited on the first try.
- **Verifier probe**: qwen3:1.7b judged verbatim, derived and wrong answers all "unsupported" and
  turned correct answers into refusals; qwen3:8b judged 3/3. Verify moved to the main model.
- **Loop vs graph (first look)**: graph 4/4 verified; loop grounded 4/4 but complete 2/4 (omitted
  "300 seconds"); latency graph 45–87 s, loop 48–108 s, varying with swap.
- **Concurrency probe**: two simultaneous resumes ran a write twice → conditional UPDATE on the
  approvals row; four parallel approves → `[200, 409, 409, 409]`.
- A 79 s model call → SSE keep-alive comment every 15 s.
- Recorder reviews found 39 issues across M1–M4; the important ones were fixed with tests.

### Trade-offs learned
- Count a model call when attempted, not when it succeeds — failures must consume budget.
- Fixed graph vs model-driven loop: structure won on completeness; the loop fits multi-hop.
- Snapshot the execution context into the checkpoint (simple, durable) vs reload from DB (smaller).
- SSE (one-way, plain HTTP, works with headers and curl) vs WebSocket vs polling; cancel on
  disconnect (simple, no orphan runs) vs keep running (needs a durable queue).
- A conditional UPDATE beats a Python lock: it works across processes.

---

## Day 3 — Workspace tools, observability, evaluation ([journals 06](06-workspace-tools.md)–[08](08-evaluation.md))

### Concepts implemented
- **Sandboxed workspace**: per-tenant path jail (relative only, `realpath` before containment,
  deny-list for secrets), six tools (writes always approved; exec approved unless read-only),
  argv-only executor with per-command **option** allowlists, scrubbed env, timeouts, output caps;
  write-ahead **idempotency receipts**; audit table; REST API and a WebSocket terminal on the same
  policy.
- **Observability from the ledger**: traces built after the run (spans, generations, tool spans with
  embed/vector/keyword children), execution id as correlation id, deterministic observation ids,
  local log sink, Langfuse and eval-only LangSmith adapters (mock-verified), redaction.
- **Evaluation framework**: a 38-case versioned golden set, 11 deterministic evaluators, an advisory
  LLM judge, a runner through the real service, persisted runs, CLI and API, and a regression gate.

### Experiments run
- **Live baseline**: 33/38 (87%); answerable 11/11, unanswerable 5/5, adversarial 7/7,
  multi-document 4/5, workspace 4/5, tool use 2/5; all safety evaluators 100%; p50 28 s, p95 49 s.
  The same five failures in two consecutive runs.
- **Run 1 (32/38) found a bug in the eval harness**: a `.env` fixture was seeded through the agent's
  own jail and refused.
- **Citations as an enum** of shown ids, after field descriptions didn't stop invalid ids.
- **Edit re-proposed after approval** (append edits keep their anchor) → a duplicate-call guard,
  later narrowed to the last side-effecting call.
- **Judge vs deterministic**: 5/5 to a false "could not complete" report; groundedness 0 to a correct
  answer it saw only in 300-char snippets.

### Trade-offs learned
- Jail + allowlist + human approval vs a container sandbox: cheap, testable, precise errors — but not
  an OS boundary (an approved `pytest` runs code as the server).
- Ledger-derived traces vs live instrumentation: one source of truth and zero risk to the run, at the
  cost of no in-flight view and reconstructed start times.
- Deterministic evaluators decide; the judge advises.
- Make invalid output unrepresentable (enums, forced flags) rather than asking for it in the prompt.

---

## Day 4 — Optimization, drift, hardening ([journals 09](09-optimization.md)–[11](11-hardening.md))

### Concepts implemented
- **Usage accounting** from the ledger (calls by model, retries, embeddings, cache hits,
  hypothetical hosted cost); five tuning switches defaulting to Day 3 behaviour; **rule routing** and a
  **calculator nudge**; an evidence cap; fixed `insufficient_evidence` text; three caches with
  versioned, permission-aware keys; **experiment profiles** with an allowlist; a benchmark verdict that
  reuses the eval gate.
- **Drift monitoring**: a model-free retrieval eval (hit@k, recall@k, MRR, **answer coverage**); a
  detector over quality, retrieval, data and operations; every breach `measured` or `suspected`;
  persisted checks and alerts.
- **Hardening**: per-tenant token buckets per route class with 429 + `Retry-After`; operator-only
  eval/experiment/monitoring APIs, fail closed in prod; a non-root Docker image with migrations at
  start; CI gates on the committed benchmark and a docker build job.

### Experiments run
- **Accepted**: `optimized` vs baseline, back to back — 36/38 vs 33/38, tokens/request −21%, qwen3:8b
  calls/request 3.03 → 2.76, p50 43.1 → 27.5 s, p95 65.4 → 60.0 s, tool use 2/5 → 5/5.
- **Rejected by the gate**: query rewrite off (multi-document 4/5 → 2/5), even though a retrieval
  probe said coverage 1.0 — and the probe itself was wrong (it credited "15 minutes" from the
  failover runbook).
- **Rejected and fixed**: routing that also skipped comparisons broke `multi-availability` (0/3 vs
  3/3 in repeated runs): the skipped call had been fetching version metadata.
- **Determinism**: the baseline re-run 23 h later gave identical answers and tokens, but p50 60.6 s vs
  43.1 s (swap 9–11 GB).
- **Relevance floor** measured and not adopted: unanswerable top scores up to 0.766, answerable down to
  0.651.
- **Drift demo**: degraded chunking → `drift`, exit 1; pass rate 0.868 → 0.816 and coverage
  0.971 → 0.882 measured; recall@k and hit rate stayed 1.0; the judge went *up* (0.969 → 0.998).
- **Docker**: image built (919 MB), `/ready` correctly said Ollama unreachable while Ollama listened
  on loopback.

### Trade-offs learned
- A speedup is only a win if the quality gate agrees; two of three candidates were rejected.
- Tokens and call counts are the machine-independent cost signal; latency only back to back.
- Regex routing (free, auditable) vs a model router (flexible, costs a call).
- Pick the metric that can see the failure: file-level retrieval metrics saturate on a small corpus.
- In-process rate limits and caches, documented, vs Redis: right for one process, with the interface
  ready for replicas.
- CI that can't run the model gates the recorded evidence, and should say so.

---

## Day 5 — Web UI, demo, loop vs graph ([journals 12](12-ui-api.md)–[14](14-demo-loop-vs-graph.md))

### Concepts implemented
- **API first for the client**: `GET /executions`, `GET /documents/{id}/chunks/{chunk_id}` (404 across
  tenants), operators see every evaluation run.
- **A thin React client**: React + TypeScript (strict) + Vite + Tailwind, two runtime dependencies,
  Vite proxy instead of CORS, the API key as the only credential; **SSE over POST** with `fetch` and an
  incremental parser; a citation drawer; an **approval diff preview**; a sandbox terminal; operator
  screens that explain a 403; a per-screen error boundary.
- **An end-to-end demo script** through public endpoints only, and a UI walkthrough.
- **The loop-vs-graph comparison** owed since Day 3.

### Experiments run
- **Driven in Chrome against the live models**: a cited answer and its source; a bad agent edit (would
  have duplicated a `return` line) caught by the diff and rejected; the resumed run failed with
  `provider_unavailable` because macOS had killed Ollama under memory pressure; a new-file write
  approved and written; terminal refusals; compare 87% → 95%; tenant B's 403/4403 explanations. Five
  UI/API bugs found and fixed this way.
- **Demo script live**: "5 minutes = 300 seconds", verified, 5 model calls, 38.2 s, 18.7 s of it in
  the `select_tools` call that the accepted optimization removes but the server defaults keep; the
  write approved and on disk; three refusals; tenant B 404. It found that upload returns
  `{document, duplicate}`, which two clients had got wrong.
- **Loop vs graph, same 33 cases**: loop 1/33 strict, 24/33 without `structured_output`; graph 29/33
  and 32/33. qwen3:8b answered in prose before `final_answer` in 31/33 loop runs. Loop 3.64 calls but
  6,207 tokens/request vs 4.76 and 4,304 (+44%) and 3,328 optimized (+87%).

### Trade-offs learned
- Constrained decoding is why the graph is reliable on an 8B model; free-form turns cost a correction
  on almost every answer.
- Fewer calls is not cheaper when every call resends the history.
- A client-side diff is cheap and caught a real bad edit, but re-implements the tool's rule; a
  server-side dry run would be exact.
- API key in `localStorage` is acceptable for a local tool, not for a hosted one.
- Report strict and lenient numbers together.

---

## Experiment log (all five days)

| Day | Experiment | Result | What changed |
|---|---|---|---|
| 1 | Live RAG questions | keyword search 0 candidates | AND → OR tsquery |
| 1 | Zero-width-only file | accepted as non-empty | strip invisible characters |
| 2 | Model-chosen `top_k` | `top_k=1` missed v2 | floor of 3 |
| 2 | Answer-first schema | uncited 3/3, identical retries | citations first; replay draft |
| 2 | Verifier 1.7b vs 8b | 1.7b "no" to everything | verify on 8b |
| 2 | Concurrent resumes | write ran twice | conditional UPDATE, 409 |
| 2 | Loop vs graph (small) | graph 4/4, loop complete 2/4 | eval case for the conversion |
| 3 | Eval run 1 | 32/38, fixture refused by the jail | runner seeds as operator |
| 3 | Eval baseline | 33/38, safety 100% | committed baseline, CI floor |
| 3 | Judge vs evaluators | judge 5/5 on a false report | judge advisory only |
| 4 | Query rewrite off | multi-doc 4/5 → 2/5 | rejected; rewrite stays |
| 4 | Routing skips comparisons | 0/3 vs 3/3 | comparisons keep tool selection |
| 4 | Optimized vs baseline | 36/38 vs 33/38, −21% tokens, p50 −36% | profile accepted |
| 4 | Same run 23 h later | identical answers, p50 60.6 vs 43.1 s | latency only back to back |
| 4 | Relevance floor | score ranges overlap | not adopted |
| 4 | Degraded chunking | drift measured; recall 1.0 | answer coverage metric |
| 5 | UI in Chrome | bad edit caught; 5 bugs | diff preview; fixes + test |
| 5 | Demo script | upload shape wrong in 2 clients | fixed |
| 5 | Loop vs graph (33 cases) | 1/33 strict, 24/33 vs 29–32/33; +44% tokens | graph stays default |

---

## Patterns across the five days

1. **Every bug that mattered was found by a live run or a probe disagreeing with an expectation** —
   then explained before it was fixed. Unit tests kept the fixes fixed.
2. **At temperature 0 a disagreement is behaviour, not noise.** That made one run per profile
   meaningful for quality, and made "identical retry → identical failure" visible on Day 2.
3. **Make the wrong thing unrepresentable in code** (schemas, enums, forced flags, allowlists,
   budgets) instead of asking the model in the prompt. Every time a prompt rule was tried first, it
   didn't hold.
4. **Validate the validators**: the verifier (Day 2), the eval runner (Day 3), the coverage metric
   (Day 4) and the judge (Days 3–4) were each wrong once.
5. **The machine is part of the system**: swap, sleep, a port owned by another project, and the OS
   killing the model server each produced a production-shaped incident and a README entry.
6. **Document the limit you chose not to build**: per-process limits, no OS sandbox, CI without
   replay — each written down with its upgrade path.

## Still open after five days

- Two golden cases fail in the graph (`multi-retention`, `ws-edit-approved`); the accepted
  `optimized` profile is not the server default.
- No durable job queue (an eval killed with its process stays `running`); approval expiry and
  synchronous approve; SSE without reconnect.
- Workspace is not an OS sandbox; rate limits and caches are per process; the terminal is not rate
  limited; caches never measured live.
- CI gates committed reports, never a fresh model run; no browser tests; the demo has no assertions.
- Langfuse and LangSmith verified with mocks only; no live hosted LLM call verified.
- Drift: the default retrieval head can be an experiment probe; alerts can't be acknowledged; no
  scheduled checks.

The consolidated interview guide is [docs/interview/00-consolidated-guide.md](../interview/00-consolidated-guide.md).
