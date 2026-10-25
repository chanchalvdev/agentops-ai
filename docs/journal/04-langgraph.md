# Journal 04 — LangGraph Workflow, Checkpointing, Human Approval (Day 2, M4)

Milestone: fourth and last part of Day 2 in product.txt §18 / §7 (the main agent workflow in
LangGraph, with a Postgres checkpointer and human-in-the-loop approval), plus the fixes for the M3
Recorder findings (journal 03 §7). Commit `cd553ae` on `day-2-harness-loop-graph`. Status:
**216 tests green** (unit + integration on Postgres, 7 live tests deselected), 22 more than M3.
Live `qwen3:8b` on an Apple M4 / 16 GB: **graph 4/4** across two sessions. Written by the Recorder
agent from the code and test output, not from the plan.

Not in this milestone: an HTTP endpoint for the graph or for approvals, SSE, writes to the
`approvals` table. The graph is reachable as `services.graph.run(ctx, question)` /
`services.graph.resume(ctx, approved=...)` after `await services.start()`.

---

## 1. What was built, by subsystem

### Graph topology (`app/agents/graph.py:build_graph`)
`StateGraph(AgentState, context_schema=GraphDeps)` with the ten §7 nodes:

```
START → classify_request → plan_workflow ─┬─► retrieve_evidence ─┬─► select_or_execute_tools ⇄ human_approval
                                          │                      │            │
                                          ├──────────────────────┴────────────┴─► generate_answer
                                          └─► select_or_execute_tools                   │
generate_answer → validate_citations → verify_answer → finalize_execution → END
                        │                   │
                        └──► retry_or_revise ◄┘ ──► generate_answer (bounded) | finalize_execution
```

Conditional edges (all in `app/agents/nodes.py`, bottom of file):

| Router | Decides |
|---|---|
| `after_plan` | retrieval required (`needs_retrieval`) → `retrieve_evidence`; else tools required → `select_or_execute_tools`; else `generate_answer` |
| `after_retrieval` | tools required vs not |
| `after_tools` | any `pending_calls[*].status == "pending"` → `human_approval` (approval required vs automatic continuation) |
| `after_generate` | `status` already set (e.g. no evidence) → `finalize_execution` |
| `after_validation` | `findings` → `retry_or_revise`; else `verify_answer` |
| `after_verification` | `verification == "unsupported"` → `retry_or_revise` (bounded correction); else finalize |
| `after_retry` | correction allowance spent (`status="failed_validation"`) → finalize; else `generate_answer` |

Every router first checks `state["status"]`: once any node sets a status (terminal failure:
budget, deadline, cancel, failed), the next hop is `finalize_execution`. That is the
"recoverable vs terminal failure" edge. `human_approval → select_or_execute_tools` is a plain edge.

### The `node()` wrapper (`nodes.py:node`)
Every node function is `(state, deps) -> update` wrapped by `@node(name)`, which:
1. returns `{}` if `state["status"]` is already set (except for `finalize_execution`), so nothing
   runs after the outcome is decided;
2. calls `harness.begin_step(ctx, name)` — **every node is a harness step** (finalize is exempt so
   bookkeeping runs even with no budget left);
3. turns any `AppError` into `{"status": status_for_error(exc), "error_code": exc.code}`;
4. adds `update["budget"] = ctx.snapshot()` — the ExecutionContext rides along in every checkpoint.

### State and dependencies (`app/agents/state.py`)
- `AgentState(TypedDict, total=False)`: `execution_id`, `request_id`, `question`, `intent`,
  `needs_retrieval`, `needs_tools`, `plan` (search queries), `evidence` (`EvidenceStore.to_state()`),
  `pending_calls` (`PendingCall{call, status: queued|pending|approved|rejected, reason}`), `draft`
  (`FinalAnswer` dump), `previous_output`, `findings`, `feedback`, `verification`, `status`,
  `error_code`, `budget` (`ExecutionContext.snapshot()`). All JSON-safe; no reducers, so each key is
  last-write-wins.
- `GraphDeps(harness, ctx, max_output_tokens)` is passed per invocation as LangGraph's runtime
  `context=` (`Runtime[GraphDeps]`), never stored in state — clients and live objects are not
  checkpointed.

### Nodes (`app/agents/nodes.py`)
| Node | Model / tool calls | What it does |
|---|---|---|
| `classify_request` | `small` model, `Classification` JSON schema | intent ∈ `document_question/calculation/metadata_lookup/other`, `needs_tools`. Invalid output → **fails toward retrieval** (`document_question`). `needs_retrieval = intent != "calculation"`. |
| `plan_workflow` | `small` model, `Plan` (1-3 queries) | Query list is **always prefixed with the original question**, deduped, capped at 3: rewriting may only add recall. |
| `retrieve_evidence` | `document_search` per query through `harness.call_tool` | Adds passages to the `EvidenceStore`; a non-terminal search error is noted in `findings` and skipped; terminal errors re-raise. |
| `select_or_execute_tools` | `main` model with tool specs (minus `document_search`) | If `pending_calls` is non-empty (**resume path**) it executes them without asking the model again; otherwise asks the model. Approved → id added to `ctx.approved_call_ids`, then `call_tool`; `ApprovalRequiredError` → item becomes `pending`; rejected → a `{"rejected_by_reviewer": true, "reason"}` tool result (T#) is added, the tool never runs; non-terminal errors become a tool result. |
| `human_approval` | none | `interrupt({call_id, tool, arguments})` for the first pending call; on resume marks it approved/rejected, records an `approval` ledger event. |
| `generate_answer` | `main` model, `GroundedDraft` JSON schema | No labels → `insufficient_evidence` **without a model call**. With feedback, replays `previous_output` as an **assistant turn**, then a user "Rejected: … Valid IDs: […]" message. |
| `validate_citations` | none | `validate_final(draft, store)` (from M3). |
| `verify_answer` | **`main`** model, `Verification` schema | Entailment check over **only the cited** passages/tool results. `supported` / `unsupported` (+ claims as findings) / `unavailable` (unparseable) / `skipped` (insufficient-evidence drafts). |
| `retry_or_revise` | none | `consume_correction(kind)` with kind `verification` or `citations`; exhausted → `failed_validation`. Moves `findings` → `feedback`. |
| `finalize_execution` | none | Derives `answered`/`insufficient_evidence` if no status yet; records `final` with `status`, `verification`, `prompt_version="agent-graph-v1"`. |

Schemas, field order deliberate: `GroundedDraft = insufficient_evidence → citations → answer`
(see §4). `_structured()` is the shared helper for schema-constrained calls; it returns `None`
and records `invalid_model_output` instead of raising.

### Runner (`app/agents/graph.py:GraphRunner`)
- `thread_id = str(ctx.execution_id)`; `recursion_limit = max_graph_steps + 5` as a backstop only.
- `for_graph(ctx)` sets `limits.max_steps = limits.max_graph_steps` (new `ExecutionLimits` field,
  default 20, `Settings.agent_max_graph_steps` ≤ 100) — the graph has more, smaller steps than the
  loop's 8 turns.
- `run`, `resume(ctx, approved, reason)` (raises `ConflictError` if the thread has no pending
  interrupt; sends `Command(resume={...})`), `stream` (yields `(node, update)` minus `budget`, then
  the result), `result` (reads the checkpoint: `awaiting_approval` if interrupted, else
  `state.status`), `pending_approval`, `restore_context(execution_id)`.
- `GraphRecursionError` → `budget_exhausted` result with a `final` ledger event.

### Snapshot / restore (`app/core/execution_context.py`)
- `snapshot()` → tenant, request/execution IDs, limits, allowed tools, permissions,
  `approved_call_ids`, **`elapsed_s` = active time so far**, counters (steps, model/tool calls,
  tokens, cost as string, `cost_known`, corrections), full ledger.
- `restore(snap, clock)` rebuilds the context with `elapsed_before_s = elapsed_s`;
  `__post_init__` sets `started_at = clock() - elapsed_before_s`, so **time spent waiting for a
  human does not count against the deadline** — the clock restarts from the saved active time.
- New `listeners: list[Callable[[LedgerEvent], None]]` — `record()` calls each one per event (for
  SSE later). Not snapshotted (live objects).

### Checkpointer wiring (`app/services.py:Services.start`, `app/main.py`)
`Settings.agent_checkpointer: "postgres" | "memory"` (default postgres). `start()` (idempotent,
awaited in the FastAPI lifespan) opens a psycopg 3 `AsyncConnectionPool` (`max_size=5`,
`autocommit=True`, `prepare_threshold=0`, `dict_row`) on the same database URL with the
`+asyncpg` driver stripped, builds `AsyncPostgresSaver`, runs `setup()` (its own tables and
migrations), and creates the `GraphRunner`. `services.graph` raises if `start()` was not awaited.
`aclose()` closes the pool. Dependencies: `langgraph 1.2.17`, `langgraph-checkpoint-postgres 3.2.0`.

### M3 review fixes (journal 03 §7)
| M3 finding | Fix in `cd553ae` |
|---|---|
| 1. `<tool_result>` fence breakable | `_FENCE_BREAKOUT` in `app/rag/answer.py:25` now matches `source|evidence|tool_result|tool_error` |
| 2. v1/v2 shared text collapsed | `_text_key` (`evidence.py:40`) = `document_id:` + normalised text — dedupe is per document; a label is never both new and seen |
| 3. Failed call could not be retried | `custom_loop._execute` adds the signature to `seen` only after a successful call |
| 5. Inconsistent codes | exhausted citation corrections → `status=failed_validation`, `error_code=failed_validation` |
| 9. Approval dead end in the loop | the loop now removes approval-required tools from `ctx.allowed_tools` (`custom_loop.py:103-107`): approvals are graph-only |
| 10. `PROMPT_VERSION` not persisted | `prompt_version` is in every `final` ledger event (loop and graph) |
| 11. Fenced JSON / `<think>` rejected | `_strip_wrapping` (`evidence.py:167`) strips a leading `<think>…</think>` and a ```` ```json ```` fence |
| 12. Tool error text unescaped | `<tool_error>` message goes through `escape_untrusted` |

`EvidenceStore` gained `to_state()` / `from_state()` so evidence can live in the checkpoint.
`AgentRunResult.verification` was added (graph only).

---

## 2. Files touched

```
app/agents/graph.py, app/agents/nodes.py, app/agents/state.py          (new)
app/agents/{custom_loop,evidence,result}.py
app/core/{budgets,config,execution_context}.py, app/rag/answer.py
app/services.py (start/aclose, checkpointer), app/main.py (await services.start())
pyproject.toml, uv.lock (langgraph, langgraph-checkpoint-postgres, psycopg pool)
tests/unit/test_agent_graph.py, tests/integration/test_agent_graph_db.py    (new)
tests/unit/test_agent_loop.py, tests/live/test_agent_live.py (parametrised loop|graph), tests/conftest.py
```

---

## 3. Key design decisions (taken vs rejected)

| # | Decision taken | Rejected alternative | Why / cost accepted |
|---|---|---|---|
| 1 | **Fixed path in code, model fills each step** | A ReAct-style prebuilt agent inside LangGraph | §7 asks for explicit nodes and edges; verification must happen before answering, which a model-driven loop can skip. Cost: less flexible on multi-hop questions (retrieval happens once, up front). |
| 2 | **Every node is a harness step**, inside a shared `node()` wrapper | Rely on LangGraph's `recursion_limit` | Budgets are enforced by the harness (§7 "not just graph prompts"); `recursion_limit` (`max_graph_steps + 5`) is only a backstop. One wrapper means no node can forget the check. |
| 3 | **Terminal failure = a `status` in state**, routed to `finalize_execution` | Let exceptions escape `ainvoke` | Every run ends in `finalize_execution`, so the ledger always gets one `final` event and the checkpoint holds the outcome. |
| 4 | **JSON-only state; live deps via runtime `context`** | Put the harness/ctx in state, or in closures | The checkpointer serialises state; clients and secrets must not be persisted (§7). Closures would tie a compiled graph to one request. |
| 5 | **ExecutionContext snapshotted into every checkpoint** | Rebuild a fresh context on resume | A fresh context would reset budgets (a paused run could double its model-call allowance) and lose the ledger. Cost: full ledger copied into every checkpoint (§8 item 8). |
| 6 | **Deadline counts active time only** (`elapsed_s` → `elapsed_before_s`) | Wall-clock deadline from the first request | A human may answer hours later; with wall-clock deadlines every approved run would end `deadline_exceeded`. |
| 7 | **`interrupt()` in a dedicated node; resume executes stored calls** without a new model call | Re-ask the model after approval | The human approved *these* arguments; asking again could produce different ones. Rejected calls never run and are reported as a T# result so the answer can say so. |
| 8 | **Approval keyed by call id in `ctx.approved_call_ids`**, enforced by `Harness.call_tool` | The graph decides on its own whether to run the tool | The harness stays the single gate; the graph only adds an id after a human decision. |
| 9 | **Postgres checkpointer on the same DB, psycopg pool** | Redis/SQLite, or the in-memory saver in prod | Postgres is already there; a paused run survives a restart (integration test). `memory` exists for tests and loses pending approvals. |
| 10 | **`GroundedDraft`: insufficient_evidence → citations → answer** | `FinalAnswer` order (answer first, citations optional) | Live: correct but uncited answers 3/3 with the old order (§4). Constrained decoding writes fields in order, so the model commits to IDs before prose. |
| 11 | **Corrections replay the rejected draft as an assistant turn** | Append feedback to the same prompt | Live: at temperature 0 the appended-note prompt reproduced the identical draft. |
| 12 | **`verify_answer` on the main model, cited sources only** | `small` model; or verify against all evidence | Probe: qwen3:1.7b said unsupported for all 3 answers (no signal) and caused a false-negative revision; qwen3:8b judged 3/3. Cited-only keeps the check about the answer's own citations. |
| 13 | **Verifier failure → `verification="unavailable"`, answer still returned** | Treat as failed / as passed silently | Not silently passed: the caller sees `unavailable`. Not failed: a flaky verifier should not block every answer. This is fail-open and is a policy choice (§8 item 12). |
| 14 | **No evidence → `insufficient_evidence` without a model call** | Ask the model anyway | Cheaper and nothing to hallucinate from. |
| 15 | **Original question always searched** in `plan_workflow` | Use only the rewritten queries | A bad rewrite can only add recall, never remove it. |
| 16 | **Loop never offers approval-required tools** | Let the loop return `awaiting_approval` | The loop has no resume path; offering a write tool it cannot finish is a dead end. |

---

## 4. Live experiments (qwen3:8b / qwen3:1.7b, Apple M4 16 GB)

**Experiment 1 — answer schema field order.** With the original `FinalAnswer` schema (answer
first, citations optional), qwen3:8b produced a *correct but uncited* RPO answer **3 times in a
row**. Each retry appended feedback to the same prompt and, at temperature 0, produced the
**identical draft** → `failed_validation`. Fix: `GroundedDraft` field order
`insufficient_evidence → citations → answer`, and corrections replay the rejected draft as an
assistant turn (`generate_answer`). After the fix the draft cited `S5` on the first try.
Lesson: with constrained decoding, schema field order is part of the prompt; and a deterministic
model given the same prompt gives the same output, so a retry must change the input.

**Experiment 2 — verifier model probe** on the v2 RPO passage with three answers: verbatim
"5 minutes", derived "5 minutes = 300 seconds", wrong "15 minutes".
| Model | Verbatim | Derived | Wrong | Latency |
|---|---|---|---|---|
| qwen3:1.7b | supported=false | supported=false | supported=false | 0.3-2.4 s |
| qwen3:8b | correct | correct | correct (rejected) | 0.9-6.5 s |

Before switching, the 1.7b verifier caused a false negative: the graph *revised a correct answer
into `insufficient_evidence`*. A verifier that says "no" to everything is worse than none: it
converts good answers into refusals. `verify_answer` now uses `"main"` (comment at `nodes.py:353`).

**Final live runs (two sessions, `tests/live/test_agent_live.py`, parametrised `loop|graph`):**
- **Graph 4/4.** RPO question answered "5 minutes = 300 seconds", `verification=supported`;
  the out-of-scope question ("CEO's favourite colour") → `insufficient_evidence`.
- **Loop:** grounding correct, but it **omitted "300 seconds" 2/2** in this session (it had passed
  2/2 in an earlier session). Recorded for the Day 3 eval set.
- Latency this session: **graph 45-87 s, loop 48-108 s**. The spread likely comes from Ollama
  swapping models (the graph uses `small` for classify/plan and `main` for the rest; 16 GB holds
  both only barely alongside Docker). Not measured directly.

---

## 5. When to use which: Answerer vs loop vs graph

(Mirrors the module docstring of `app/agents/graph.py`.)

| | `Answerer` (`app/rag/answer.py`) | `AgentLoop` (`custom_loop.py`) | Graph (`graph.py`) |
|---|---|---|---|
| Who decides the path | Code (fixed 2 steps) | The model, turn by turn | Code (fixed DAG); model fills each node |
| Retrieval | Question verbatim, once | Model-written queries, any number | Question + ≤ 2 rewrites, once, up front |
| Tools | None | All allowed, read-only | Read tools + approval-gated write tools |
| Verification | Citation check | Citation check | Citation check **+ entailment (`verify_answer`)** |
| Pause/resume | No | No | Yes (checkpoint + `interrupt`) |
| Per-step model choice | n/a | One model | `small` for classify/plan, `main` for tools/answer/verify |
| Live cost (RPO question) | 1-2 calls | 48-108 s this session | 45-87 s this session |
| Live quality (this session) | (not run) | correct value, omitted "300 seconds" 2/2 | 4/4 incl. "300 seconds", verified |

- **Answerer:** plain single-hop lookups where the question is a good search query; cheapest,
  most predictable, easiest to evaluate. The baseline the others must beat.
- **Loop:** the number/order of tool calls is unknown up front (multi-hop: "find the latest
  version, then look up X in it"); exploratory questions. Its path is the model's choice, so it
  can stop early (observed: it skipped the seconds conversion).
- **Graph:** steps must happen in a known order (verify before returning), a run must pause for a
  human and resume later (any write or externally consequential tool), or each step needs its own
  model/prompt/test. Observed trade-off: the fixed `verify_answer` + revision path made the graph
  more complete than the loop on the same question, at similar latency; but the graph retrieves
  only once, so a question needing a second, evidence-dependent search is better served by the loop.

---

## 6. Commands

```bash
AGENTOPS_DB_PORT=5435 docker compose up -d db
export AGENTOPS_TEST_ADMIN_DSN=postgresql://agentops:agentops@localhost:5435/agentops
export AGENTOPS_TEST_DATABASE_URL=postgresql+asyncpg://agentops:agentops@localhost:5435/agentops_test
uv sync                                   # langgraph + langgraph-checkpoint-postgres
uv run pytest -m "not live"               # 216 passed, 7 deselected
uv run pytest tests/unit/test_agent_graph.py tests/integration/test_agent_graph_db.py
uv run pytest -m live tests/live/test_agent_live.py -s -k graph   # needs Ollama, qwen3:8b + 1.7b
AGENTOPS_AGENT_CHECKPOINTER=memory uv run ...   # in-memory saver; paused runs are lost on restart
```
The Postgres checkpointer creates its own tables on `Services.start()` (`AsyncPostgresSaver.setup`).

---

## 7. How it was verified

- **216 passed, 7 deselected (live)** on Postgres (re-run by the Recorder on a clean export of
  `cd553ae`: 216 passed in 5.7 s).
- `tests/unit/test_agent_graph.py` (15 tests, scripted `GraphModel` that answers by system prompt,
  `InMemorySaver`): happy path node order and model calls (`classify, plan, answer, verify`);
  original question always searched; calculation skips retrieval and cites `T1`; unknown citation
  revised once; unsupported answer → `failed_validation` after 2 corrections with the draft hidden;
  verifier garbage → `unavailable`; no evidence → no answer/verify call; classifier garbage → still
  retrieves; `max_graph_steps=3` stops after 3 nodes with a `final` event; cancellation mid-graph;
  write tool pauses and runs only after approval; rejection performs no write and yields a
  `rejected_by_reviewer` reference; **resume in a new runner** restores counters
  (`model_calls == used + 2`); resume without a pending interrupt → `ConflictError`; write tool
  without `notes:write` never reaches approval.
- `tests/integration/test_agent_graph_db.py`: real ingestion + Postgres checkpointer; the run
  pauses, the execution row is `awaiting_approval` with `finished_at IS NULL`; a **fresh
  `GraphRunner` over the same checkpointer** restores the context, resumes, writes once, answers
  with `verification=supported` citing `dr.md`; persisted events have contiguous `seq` and an
  `approval/ok` event.
- `tests/unit/test_agent_loop.py` gained 3 regression tests for the M3 fixes: a failed call can be
  retried with identical arguments; exhausted citation corrections report `failed_validation`;
  fenced JSON content is accepted as final.
- Live: §4.

---

## 8. Known limitations and open findings

Found by the Recorder while reading the code and probing it with the unit-test fixtures
(not fixed in this milestone):

1. **Concurrent resumes execute a write twice.** `GraphRunner.resume` (`graph.py:109-115`) checks
   `state.interrupts` and then invokes, with no lock or compare-and-set. Probe: two
   `asyncio.gather`ed `resume(approved=True)` on the same paused run → both `answered`, and
   `save_note` ran **twice**. Approve + reject concurrently → both `answered` with contradictory
   ledgers.
2. **A decision is not bound to a call id.** `resume(approved, reason)` has no `call_id`;
   `human_approval` applies it to *the first* pending call (`nodes.py:262`). With two pending calls,
   a reviewer looking at a stale `pending_approval()` can approve a different call than they saw.
3. **Resuming with the original in-memory context counts the wait against the deadline.** Only
   `restore_context` rebuilds the clock; `resume` accepts any ctx. Probe: pause, advance the
   clock 1000 s, `resume(ctx, approved=True)` with the same ctx → `deadline_exceeded`, approval
   lost, no write. The unit happy-path test resumes with the original ctx and passes only because
   it is fast.
4. **`human_approval` counts a step twice in-process and once after restore.** `begin_step` runs
   before `interrupt()` (`nodes.py:95-96`), and the node re-runs from the top on resume. Probe:
   original ctx had 5 steps at pause vs 4 in the checkpoint; resuming with the original ctx left
   two `step/human_approval` ledger events. The step is not side-effect free, contrary to the
   comment at `nodes.py:260`.
5. **Crash after an approved write re-runs it.** The approved call executes inside
   `select_or_execute_tools` (`nodes.py:239-242`); if the process dies before that node's
   checkpoint, replay executes it again (at-least-once, no idempotency key). And a crashed,
   non-interrupted thread cannot be continued: `resume` raises `ConflictError` (`graph.py:113`).
6. **Retrieval outage is reported as "insufficient evidence".** `retrieve_evidence` turns
   non-terminal search errors into `findings` (`nodes.py:186-190`); `generate_answer` then clears
   `findings` and, with no labels, answers "No relevant passages were found"
   (`nodes.py:286-292`). Probe with a search tool that raises → `insufficient_evidence`. Users are
   told their documents lack the answer when the database is down.
7. **No tenant check on restore.** `restore_context(execution_id)` (`graph.py:168-174`) loads any
   thread by id; thread ids are not tenant-scoped. The future approvals API must check the tenant
   (e.g. via `ExecutionStore.get`) before calling it.
8. **Checkpoint growth and contents.** `node()` writes the full `ctx.snapshot()` (whole ledger,
   including tool arguments such as the `save_note` text) on every node (`nodes.py:101`).
   Probe: 18 checkpoints for one approved run, 219 ledger events stored in total for a 20-event
   ledger. `evidence` stores full passage text, and `previous_output` stores raw model output.
   Nothing prunes checkpoints after a run finishes.
9. **Only `GraphRecursionError` is caught** in `_invoke` (`graph.py:136-142`); a non-`AppError`
   bug, a checkpointer `OperationalError`, or `CancelledError` escapes and the execution row stays
   `running`. `stream()` (`graph.py:117-133`) handles neither recursion errors nor resume.
10. **Unbounded prompt size in the graph.** `_evidence_block` renders all evidence
    (`nodes.py:131-136`): up to 3 queries × `top_k` 5 × 1500 chars, plus a replayed draft on
    revision — no `bound_history` equivalent, while `num_ctx` is 8192; Ollama truncates silently.
11. **`previous_output` truncated to 4000 chars** (`nodes.py:320`) while `answer` may be 6000, so a
    long rejected draft is replayed as broken JSON.
12. **Insufficient-evidence drafts are not verified** (`nodes.py:346-347`) and their model text is
    returned uncited (M3 finding 4, still open). Verifier `unavailable` is fail-open.
13. Invalid JSON from `generate_answer` is counted as a `citations` correction
    (`nodes.py:377`); with two kinds × 2 corrections a run can generate up to 5 drafts (bounded by
    `max_model_calls` and `max_graph_steps`).
14. The `approvals` table is still never written; `pending_approval` returns only
    `interrupts[0]`. `AgentState` has no reducers, so re-invoking an existing `thread_id` with a
    new question inherits the old `status` and finalises immediately (`nodes.py:92`).
15. Nits: the `agent_max_context_chars` comment now sits above `agent_checkpointer`
    (`config.py:67`); `Services.start` does not close the pool if `setup()` fails.

---

## 9. What I'd do differently at 100x scale

- **Serialise resumes per execution**: `SELECT … FOR UPDATE` on the `agent_executions` row (or an
  advisory lock keyed by `execution_id`) around `resume`, a state check
  `awaiting_approval → resuming`, and a decision keyed by `(execution_id, call_id)` written to the
  `approvals` table with a unique constraint. Pass an idempotency key (the call id) to write tools.
- **Always resume from the checkpoint** (`restore_context` inside `resume`), never from a cached
  context; make `begin_step` in `human_approval` happen after the interrupt.
- **Keep checkpoints small**: counters and the last ledger `seq` in state, ledger events streamed
  to `execution_events` as they happen; prune or TTL checkpoints of finished threads; store
  evidence as chunk ids and re-fetch.
- **Workers**: graph steps on a queue-backed worker pool sharing a GPU pool; the checkpointer makes
  any worker able to continue any thread. Route single-hop questions to the `Answerer`.
- **Distinguish "nothing found" from "search failed"** and surface retrieval outages as `failed`.
- **Measure the model split** (`small` vs `main`) against model swap cost on one GPU: on a 16 GB
  machine, a single resident model may beat two that evict each other.
