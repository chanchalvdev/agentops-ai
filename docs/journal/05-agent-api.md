# Journal 05 — Agent API, SSE Streaming, Approvals, CLI (Day 2, M5) + Day 2 Retrospective

Milestone: fifth and last part of Day 2 in product.txt §18 — the HTTP surface for the agent
(§14: run, stream, executions, trace, approvals) and the `agentops run` CLI, plus the fixes for the
M4 Recorder findings (journal 04 §8). Commit `8f648aa` on `day-2-harness-loop-graph`. Status:
**227 tests green** (unit + integration on Postgres, `-W error`, 7 live tests deselected), 11 more
than M4. Re-run by the Recorder on the commit: `227 passed, 7 deselected in 15.46s`. One live CLI
graph run on the dev DB (§4). Written by the Recorder agent from the code and test output, not
from the plan.

Not in this milestone: SSE for the resume after an approval (approve/reject are plain
request/response), any production tool that requires approval (see §8 item 1), approval expiry,
a CLI approve command.

---

## 1. What was built, by subsystem

### Endpoints (`app/api/agent.py`, router prefix `/api/v1`, all behind `X-API-Key`)

| Method + path | Handler | What it does |
|---|---|---|
| `POST /agent/run` | `run_agent` | Body `AgentRunRequest{question (1-2000 chars), mode: graph\|loop = graph, limits: LimitOverrides, tools: list[str] \| None (≤ 20)}`. Builds the context, runs to completion, returns `AgentRunResult` (incl. ledger). |
| `POST /agent/run/stream` | `run_agent_stream` | Same body; `text/event-stream`. Events: `start` → ledger events as they happen (`step`, `model_call`, `tool_call`, `correction`, `approval`, `policy`) → one `citation` per cited source → `final` (result **without** `ledger`, already streamed). Headers `Cache-Control: no-cache`, `X-Accel-Buffering: no`. |
| `GET /executions/{id}` | `get_execution` | `ExecutionOut`: status, answer, citations, counters, tokens, cost, limits actually applied, timestamps. Tenant-scoped (other tenant → 404). |
| `GET /executions/{id}/trace` | `get_trace` | `list[EventOut]` ordered by `seq`: the persisted ledger. Ownership checked via `ExecutionStore.get` first. |
| `GET /approvals?status=pending&limit=50` | `list_approvals` | Tenant's approvals, newest first; `status ∈ pending/approved/rejected`, `limit ≤ 200`. |
| `POST /approvals/{id}/approve` · `/reject` | `approve` / `reject` | Optional body `{reason ≤ 1000}`. Claims the decision, restores the paused run from its checkpoint, resumes it **synchronously**, returns the new `AgentRunResult`. Already decided → 409; unknown or other tenant → 404. |

Context is built **before** the stream opens (`agent.py:112-115`), so an unknown tool or a bad
limit is a normal 422 JSON error, not a broken event stream.

SSE framing is one helper: `_sse(event, data) = "event: {event}\ndata: {json}\n\n"`
(`agent.py:87-88`). No `id:` lines, no retry hint, no heartbeat (§8).

### `AgentService` (`app/agents/service.py`) — the single entry point
- `new_context(tenant, request_id, overrides, allowed_tools)` (54-72): unknown tool names →
  `InvalidRequestError`; limits = `defaults.tightened(**overrides)`. `LimitOverrides` (29-37)
  validates each field (`ge=1`, `deadline_s > 0`); `tightened` keeps `min(default, requested)`.
  Proven by `test_request_limits_only_tighten_and_unknown_tools_rejected`: `max_steps=10_000` is
  stored as 8 (the server default).
- `run(ctx, mode, question)` (74-76): `ExecutionStore.start` (row `running`) then `_execute`.
- `stream(...)` (78-98): appends `queue.put_nowait` to `ctx.listeners` (the M4 hook), runs `run()`
  as a task, and multiplexes `queue.get()` against the task with `asyncio.wait(FIRST_COMPLETED)`,
  draining the queue after the task finishes. `finally`: if the task is still running, the
  consumer went away → `ctx.cancel("client disconnected")`, `task.cancel()`, await it.
- `decide(tenant, approval_id, approved, reason)` (100-114): `ApprovalStore.decide` (the claim)
  → `graph.restore_context(execution_id)` → tenant check → `_execute(graph.resume(..., call_id=
  approval.call_id))`.
- `_execute(ctx, mode, run)` (121-147): **every exit path persists**:
  - success → `save`;
  - `CancelledError` → `ctx.cancel`, a `final/error execution_cancelled` ledger event, result
    `cancelled`, `await asyncio.shield(save(...))`, then **re-raise** (cancellation must
    propagate);
  - `ConflictError` → re-raised, nothing saved (resume of a run that is not paused);
  - any other exception → logged with the execution id, `final/error internal_error`, result
    `failed`, saved.
  - Afterwards, if `status == awaiting_approval`, `graph.pending_approval(ctx)` →
    `ApprovalStore.create(...)`.

### `ApprovalStore` (`app/agents/store.py:102-168`)
- `create` (109-131): `INSERT … ON CONFLICT (uq_approvals_execution_call) DO UPDATE SET call_id =
  call_id RETURNING *` — re-saving the same paused state returns the existing row instead of a
  second approval. (`DO UPDATE` rather than `DO NOTHING` so `RETURNING` always yields a row.)
- `decide` (141-168): **one conditional UPDATE** `… WHERE id = :id AND tenant_id = :t AND status =
  'pending' RETURNING *`. Row returned → this caller won. No row → a second query distinguishes
  "not found" (404) from "already approved/rejected" (409). This is the compare-and-set that
  closes M4 finding 1 (concurrent resumes executed a write twice).
- `list` (133-139): tenant-scoped, optional status, `created_at DESC`.

### Decision bound to a call id (M4 finding 2)
`GraphRunner.resume(..., call_id=...)` (`graph.py:108-123`) passes `{"approved", "reason",
"call_id"}` to `Command(resume=...)`. `human_approval` (`nodes.py:270-273`): if `call_id` is set
and differs from the pending call it is showing, the decision becomes **a rejection** ("decision
did not match the pending call"). `None` still means "the first pending call" (unit tests).

### CLI (`app/cli.py:116-148`)
`agentops run "question" --mode graph|loop --tenant tenant_a --trace`. `_with_services` now awaits
`services.start()` (the checkpointer). Prints `[status] answer`, citations (`S#: file §section
pN`), tool references, a summary line (`execution, steps, model_calls, tool_calls, corrections,
verification, tokens, time`), and with `--trace` one line per ledger event. It calls
`AgentService.run`, so CLI runs are persisted like API runs (request id `"cli"`).

### Composition root (`app/services.py`)
`ApprovalStore` and `AgentService` added to `Services`. The service takes the graph as a
**factory** `lambda: services.graph` (`services.py:163`) because the `GraphRunner` exists only
after the async `start()`; the lambda closes over a variable assigned a few lines later.
`Services.start` now closes the pool and resets it if `AsyncPostgresSaver.setup()` fails (M4
finding 15).

### M4 review fixes (journal 04 §8)
| M4 finding | Fix in `8f648aa` |
|---|---|
| 1. Concurrent resumes ran the write twice | `ApprovalStore.decide` conditional UPDATE; only the winner resumes. Integration test: 4 parallel approves → `[200, 409, 409, 409]`, one write |
| 2. Decision not bound to a call | `call_id` carried through `resume`; mismatch → rejection (`nodes.py:271-273`) |
| 3/4. Resuming with the in-memory ctx | `AgentService.decide` always uses `restore_context`; `resume` docstring states the contract |
| 6. Retrieval outage → "insufficient evidence" | `retrieve_evidence`: if **every** planned search failed → `status=failed, error_code=retrieval_error` (`nodes.py:192-194`) |
| 7. No tenant check on restore | `decide` compares `ctx.tenant_id` (and the approval row is already tenant-scoped) |
| 9. Exceptions left rows `running` | `AgentService._execute` catches everything and persists |
| 11. `previous_output` cut at 4000 | now 8000 (`nodes.py:327`), covering a 6000-char answer + JSON |
| 13. Invalid JSON counted as a citation correction | new correction kind `invalid_output` when `draft is None` (`nodes.py:384-389`) |
| 14. `approvals` table never written | `_execute` creates the row on `awaiting_approval` |
| 15. Pool not closed on setup failure; config comment | fixed |
| (unused) `GraphRunner.stream` | removed; streaming is now ledger-based in `AgentService.stream` |

Three unit regressions in `tests/unit/test_agent_graph.py`: outage → `failed/retrieval_error`;
decision for another call id → no write, `rejected_by_reviewer`; unparseable answer →
`corrections == {"invalid_output": 1}`.

---

## 2. Files touched

```
app/agents/service.py, app/api/agent.py                                  (new)
app/agents/store.py (ApprovalStore), app/agents/graph.py, app/agents/nodes.py
app/services.py, app/main.py (router), app/cli.py (run), app/core/config.py (comment)
tests/integration/test_agent_api.py (new, 8 tests), tests/unit/test_agent_graph.py (+3)
README.md, .env.example, docs/day-checklist.md
```

---

## 3. Key design decisions (taken vs rejected)

| # | Decision taken | Rejected alternative | Why / cost accepted |
|---|---|---|---|
| 1 | **SSE** for progress | WebSocket; client polling of `/executions/{id}/trace` | One-way server→client is all a run needs; SSE is plain HTTP (auth header, proxies, curl), text framing, built-in browser `EventSource`. WebSocket adds a bidirectional protocol nobody uses here. Polling works and is kept as the fallback (trace endpoint), but adds latency and DB load. Cost: SSE ties the run to one connection (§8). |
| 2 | **Stream ledger events, not graph node updates** (`ctx.listeners`) | `graph.astream(stream_mode="updates")` (the removed `GraphRunner.stream`) | One mechanism for loop and graph; the stream shows exactly what the trace endpoint will show later; node updates contain state (evidence text, budget snapshots) that should not leave the server. |
| 3 | **Client disconnect cancels the run** | Detach and let it finish in the background | Nobody is listening, local GPU time is the scarcest resource, and the run is persisted as `cancelled` with `finished_at`. Cost: a network blip kills a long run (no reconnect). |
| 4 | **Persist on every exit path** in one wrapper (`_execute`) | Persist in each caller / rely on the happy path | An execution row stuck in `running` is a lie the dashboard will show forever. `asyncio.shield` lets the save finish while the task is being cancelled; the `CancelledError` is re-raised so the caller's cancellation semantics are kept. |
| 5 | **Approval claim = one conditional UPDATE** (`status='pending'` in the WHERE) | `SELECT … FOR UPDATE` + check + update; advisory lock; in-process lock | Atomic in one round trip, works across processes, no lock held during the (slow) resume. Cost: the claim is committed before the resume, so a resume that fails leaves the approval `approved` but unexecuted (§8 item 3, deferred). |
| 6 | **Decision carries `call_id`; mismatch = rejection** | Apply the decision to whatever is pending | Approval is consent to a specific call the reviewer saw. Failing closed (reject) is safe: the tool never runs. |
| 7 | **Approve/reject resume synchronously** and return the result | 202 + background resume + poll/stream | Simplest correct thing on one box; the reviewer sees the outcome. Cost: the HTTP request lasts as long as the rest of the run (up to the remaining deadline). |
| 8 | **Request limits only tighten**; unknown tools 422 | Let callers raise limits | §5: the server owns the ceiling. A client may ask for a cheaper run, never a more expensive one. |
| 9 | **Validate before streaming** | Validate inside the generator | Once `200 text/event-stream` is sent, an error can only be an in-band event; config errors should be ordinary 4xx. |
| 10 | **`final` SSE event drops `ledger`** | Send the full result | Every ledger event was already streamed; resending doubles the payload. |
| 11 | **Graph as a factory** (`lambda: services.graph`) | Construct `AgentService` in `start()` | Keeps `build_services` synchronous and `AgentService` constructible in tests; the factory fails loudly if `start()` was not awaited. |
| 12 | **CLI goes through `AgentService`** | Call loop/graph directly | Same persistence and approvals path as the API; a CLI run is inspectable via `/executions/{id}`. |

---

## 4. Live run (qwen3:8b, Apple M4 16 GB, dev DB)

```
agentops run "What is the RTO in the latest payments architecture?" --mode graph --trace
```
- **Answer:** "15 minutes with automated failover", cited `payments-architecture-v2.md`,
  `verification=supported`.
- **One citation correction** (the first draft failed `validate_citations`; the replayed-draft
  correction from M4 fixed it on the next try).
- **6 model calls, 153.5 s.** (6 is consistent with classify, plan, select_tools, two
  `generate_answer` drafts, verify; the per-call breakdown was not recorded here.)
- **Memory pressure:** at the time the Mac had **~15.5 GB of 16 GB swap in use** (other Docker
  stacks plus qwen3:8b at an 8k context). `select_tools` alone took **79 s, vs ~15 s earlier** in
  the day. The answer path was correct; the latency was the machine, not the graph.

Lessons: (1) the M4 fixes held up on a new question (RTO rather than RPO) — v2 chosen, citation
repaired by one correction, verified; (2) wall-clock latency on a 16 GB laptop is dominated by
memory, so latency numbers are only comparable when the machine state is recorded; (3) a 79 s
silent gap between SSE events is longer than common proxy idle timeouts (§8 item 2).

---

## 5. How it was verified

- **227 passed, 7 deselected** (unit + integration on Postgres, `-W error`; ruff + mypy strict
  clean per the checklist).
- `tests/integration/test_agent_api.py` (fake model, real Postgres, real ingestion):
  - `test_loop_run_execution_and_trace` — run → execution row → trace ends with `final`; other
    tenant's key → 404.
  - `test_graph_run` — default mode is graph, `verification=supported`.
  - `test_request_limits_only_tighten_and_unknown_tools_rejected` — `max_steps=1` →
    `budget_exhausted`; `10_000` stored as 8; `tools=["rm_rf"]` → 422 `invalid_request`.
  - `test_stream_emits_progress_citations_and_final` — first event `start`, last `final`;
    contains `step, model_call, tool_call, citation`; `tool_call < citation < final`; no `ledger`
    in `final`; row persisted `answered`.
  - `test_client_disconnect_cancels_and_persists` — model call sleeps 5 s; take the first event,
    `aclose()` the generator (what Starlette does on disconnect) → row `cancelled`,
    `finished_at` set. Note: it drives `AgentService.stream` directly, not the HTTP layer.
  - `test_approve_resumes_and_double_approve_conflicts` — paused run, approval lists the
    arguments; other tenant → 404; approve → `answered`, one write; approve again → 409, still one
    write; pending list empty; decided list has the reason; execution `answered`.
  - `test_concurrent_approvals_resume_once` — **4 parallel approves → exactly one 200, three
    409, one write**.
  - `test_reject_performs_no_write` — no write, `rejected_by_reviewer` tool reference.
  - These use `OpenSaveNote` (a test tool with no permission requirement) registered at runtime.
- Live: §4.

---

## 6. Commands

```bash
AGENTOPS_DB_PORT=5435 docker compose up -d db
export AGENTOPS_TEST_ADMIN_DSN=postgresql://agentops:agentops@localhost:5435/agentops
export AGENTOPS_TEST_DATABASE_URL=postgresql+asyncpg://agentops:agentops@localhost:5435/agentops_test
uv run pytest -m "not live" -W error                 # 227 passed, 7 deselected
uv run pytest tests/integration/test_agent_api.py -v

uv run agentops run "What is the RTO in the latest payments architecture?" --mode graph --trace
uv run agentops run "What is the RPO in v2, in seconds?" --mode loop

curl -N -H "X-API-Key: $KEY" -H 'content-type: application/json' \
  -d '{"question":"What is the payments RPO?","mode":"graph"}' \
  localhost:8000/api/v1/agent/run/stream
curl -H "X-API-Key: $KEY" localhost:8000/api/v1/executions/<id>/trace
curl -H "X-API-Key: $KEY" localhost:8000/api/v1/approvals
curl -X POST -H "X-API-Key: $KEY" -d '{"reason":"ok"}' -H 'content-type: application/json' \
  localhost:8000/api/v1/approvals/<id>/approve
```

---

## 7. Day 2 retrospective (M1 → M5)

### What the Recorder reviews caught
The checklist counts **39 issues across M1–M4**; the commit messages list the ones fixed.
Grouped by kind:

| Kind | Examples (milestone found → fixed in) |
|---|---|
| **Correctness under concurrency** | `mark_for_reindex` check-then-update (Day 1 → M3); concurrent graph resumes ran a write twice (M4 → M5, conditional UPDATE on approvals) |
| **Identity / binding** | non-unique Ollama tool-call IDs, and approvals are keyed by them (M1-M2 → M3); decision not bound to a call id (M4 → M5) |
| **Budget / policy holes** | tool budget checked after permission/arguments; `max_output_tokens` not capped to the remaining token budget; non-positive cost caps accepted; deadline/cancel recorded twice (M1-M2 → M3) |
| **Wrong status for the user** | retrieval outage reported as "insufficient evidence" (M4 → M5); exhausted citation corrections with inconsistent codes (M3 → M4); invalid JSON counted as a citation correction (M4 → M5) |
| **Prompt-injection surface** | `<tool_result>`/`<tool_error>` fences breakable, tool error text unescaped (M3 → M4) |
| **Dead ends** | the loop offered approval-required tools it could never resume (M3 → M4); a failed tool call could not be retried (M3 → M4) |
| **Evidence integrity** | v1/v2 shared paragraphs collapsed into one source (M3 → M4) |
| **Robustness** | fenced JSON / empty `<think>` rejected as non-final (M3 → M4); `null` tool arguments; calculator overflow and `0**-1` (M1-M2 → M3); pool not closed on setup failure (M4 → M5) |
| **Durability** | execution rows left `running` on exceptions (M4 → M5); `approvals` table never written (M4 → M5) |
| **Audit** | `prompt_version` not persisted; pending approvals without the arguments a reviewer needs (→ M3/M4) |

Pattern: the reviews were most valuable where a **probe** reproduced the problem (concurrent
resumes → two writes; outage → "your docs don't say"; 1000 s pause → `deadline_exceeded`). Each
fix landed with a regression test in the next milestone.

### Deferred (from `docs/day-checklist.md`, Day 2)
- Router-internal retries and failed calls do not count tokens against the budget → Day 4.
- History trimming can drop a turn whose S# is still citable → Day 4.
- Approval expiry/notification; **a claimed approval whose resume fails stays `approved`** → Day 4.
- Checkpoints copy the full ledger at every node and keep passage text / tool arguments; no cleanup
  → Day 4.
- An approved write that crashes before its checkpoint is replayed on recovery (no idempotency
  key) → Day 3 workspace tools.
- Graph answer prompt has no total evidence cap (fits 8k today: ≤ 15 passages × 1.5 KB) → Day 4.
- `insufficient_evidence` drafts return model text unverified; unparseable verifier output is
  `unavailable`, not blocked → decide with Day 3 eval data.
- Model calls slow under memory pressure → Day 4 routing/caching.
- Live loop omitted the "300 seconds" conversion 2/4 → Day 3 eval case.

### Most important lessons from live runs
1. **The model will take the cheapest path the schema allows.** `top_k=1` missed v2 (→ floor 3);
   answer-first schema produced uncited answers 3/3 (→ citations before answer).
2. **A deterministic model needs a different input to give a different output.** Appending a note
   reproduced the identical draft; replaying the draft as an assistant turn fixed it.
3. **Validate the validator.** qwen3:1.7b as verifier said "unsupported" to everything and turned
   correct answers into refusals; 8b judged 3/3.
4. **Fixed structure beat model freedom on completeness:** graph 4/4 vs loop omitting the seconds
   conversion 2/4 at similar latency.
5. **On 16 GB, memory is the latency budget.** Same node: ~15 s vs 79 s depending on swap; two
   models (`small` + `main`) may evict each other. Record machine state with every timing.
6. **Set `num_ctx` explicitly** — Ollama truncates silently otherwise.

---

## 8. Known limitations and open findings (Recorder review of `8f648aa`)

Not fixed in this milestone:

1. **The approval flow is unreachable in production today.** The registry has `document_search`,
   `calculator`, `document_metadata` — none requires approval — and `new_context` never sets
   permissions, so every API/CLI run has `DEFAULT_PERMISSIONS = {"documents:read"}`
   (`execution_context.py:23`). The integration tests reach it only through `OpenSaveNote`.
   Expected to change with Day 3 workspace tools; the permission source (per API key?) is open.
2. **No SSE heartbeat.** Events are recorded *after* a model call finishes; the live
   `select_tools` took 79 s with nothing on the wire. nginx's default `proxy_read_timeout` is 60 s,
   so behind a proxy the connection drops → treated as a disconnect → the run is cancelled.
   Fix: a `: keep-alive\n\n` comment every ~15 s from `AgentService.stream` / the route.
3. **Claim before resume, no recovery.** `decide` commits `approved` (`service.py:103`) before
   `restore_context`/`resume`. If restore returns `None`, `resume` raises `ConflictError`
   (`service.py:134-135` re-raises without saving), or the process dies mid-resume, the approval is
   `approved`, the execution row stays `awaiting_approval` (it is never moved to `running`), and
   there is no pending approval to retry. Deferred in the checklist.
4. **Execution row and approval row are two transactions.** `_execute` saves `awaiting_approval`
   (`service.py:142`) and then creates the approval (`service.py:146`). A failure between them
   leaves a paused run with no approval row → unresumable via the API.
5. **Errors after the stream starts are not reported in-band.** If `run()` raises something
   `_execute` does not swallow (e.g. `ExecutionStore.start` or `save` fails — DB down),
   `task.result()` (`service.py:93`) raises inside the generator; the client gets a truncated
   `text/event-stream` with no `error`/`final` event. The `start` event has already announced an
   `execution_id` that may not exist.
6. **Disconnect test bypasses HTTP.** `test_client_disconnect_cancels_and_persists` calls
   `aclose()` on `AgentService.stream`. Through Starlette (and the `@app.middleware("http")`
   `BaseHTTPMiddleware` in `main.py:73`), a disconnect cancels the response task; if that lands
   while the outer `events()` generator is suspended at a `yield`, the inner generator is only
   closed when garbage-collected. Not covered by a test.
7. **Approve/reject is synchronous.** The request lasts for the rest of the run (up to the
   remaining deadline; the live run was 153 s end to end). A client/proxy timeout → the client
   retries → 409 while the first resume is still running; the reviewer never sees the result
   except via `/executions/{id}`. No SSE for resumed runs.
8. **Cancel-path result loses state.** `_execute` builds the cancelled/failed result from an
   empty `EvidenceStore` (`service.py:129-131, 141`): partial evidence and any draft are dropped
   from the row; the ledger still has them.
9. **No reconnect.** SSE events have no `id:`, so `Last-Event-ID` cannot resume; combined with
   "disconnect cancels", any network blip ends the run. The trace endpoint is the only recovery.
10. **`invalid_output` adds a third correction pool.** With kinds `citations`, `verification`,
    `invalid_output` (2 each), a graph run can now generate up to 7 drafts, bounded only by
    `max_model_calls`/`max_graph_steps`.
11. `GET /approvals` cannot list all statuses: `status` defaults to `pending` and a query string
    cannot express `None` (`agent.py:156`).
12. Nits: CLI uses the constant request id `"cli"` for every run; a CLI run that pauses prints
    `[awaiting_approval] None` with no way to approve from the CLI.

---

## 9. What I'd do differently at 100x scale

- **Decouple runs from connections.** `POST /agent/run` returns `202 {execution_id}`; runs execute
  on a queue-backed worker pool; `GET /executions/{id}/events` is an SSE stream fed from the
  persisted ledger (or Postgres `LISTEN/NOTIFY` / Redis streams) with `id: <seq>` so clients
  resume with `Last-Event-ID`. Disconnect no longer cancels; an explicit
  `POST /executions/{id}/cancel` does.
- **Heartbeats** every 15 s and a documented proxy timeout.
- **An explicit execution state machine** in one row: `running → awaiting_approval → resuming →
  terminal`, with the approval claim and the `awaiting_approval → resuming` transition in the
  same transaction; a sweeper re-queues `resuming` rows whose worker died (from the checkpoint).
- **Write the approval in the same transaction** as the `awaiting_approval` save (or derive the
  pending list from the checkpoint and keep only decisions in the table).
- **Async approvals:** approve returns 202; the result arrives on the execution's event stream.
- **Idempotency keys** on `POST /agent/run` (client-supplied) and on write tools (the call id).
- **Permissions from the principal** (scopes per API key), not a process-wide default.
- **Backpressure:** per-tenant concurrency limits and a bounded run queue, since one 8B model on
  one GPU serves runs one at a time in practice.
