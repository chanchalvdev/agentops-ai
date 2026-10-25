# Interview Prep 04 — LangGraph Workflow, Checkpointing, Human Approval (Day 2, M4)

Every answer is grounded in the code from commit `cd553ae`. Format: direct answer, then the
reasoning, then a code pointer. Companion: `docs/journal/04-langgraph.md`.

---

## Basic

### Q1. What is the LangGraph workflow, in one sentence?
**Answer:** A fixed ten-node `StateGraph` (classify → plan → retrieve → tools ⇄ approval → generate
→ validate → verify → revise → finalize) in which code decides the path and the model only fills
in each node, with every model and tool call going through the harness.
It is the "main agent workflow" of product.txt §7, built after the custom loop so both share
`Harness`, `EvidenceStore`, `AgentRunResult` and `ExecutionStore`.
Pointer: `app/agents/graph.py:build_graph`; `app/agents/nodes.py`.

### Q2. What is in `AgentState`, and what is deliberately not?
**Answer:** IDs, the question, intent, `needs_retrieval`/`needs_tools`, the plan (search queries),
`evidence` (`EvidenceStore.to_state()`), `pending_calls`, `draft`, `previous_output`, `findings`,
`feedback`, `verification`, `status`, `error_code`, and `budget` (`ExecutionContext.snapshot()`).
Not in it: the harness, LLM clients, the live `ExecutionContext`, secrets. State is what the
checkpointer persists, so it must be JSON-safe.
Pointer: `app/agents/state.py:AgentState`, module docstring.

### Q3. How do nodes get the harness if it is not in state?
**Answer:** Through LangGraph's runtime context: the graph is built with
`context_schema=GraphDeps`, and each `ainvoke`/`astream` passes `context=GraphDeps(harness, ctx,
max_output_tokens)`. Nodes receive `runtime: Runtime[GraphDeps]` and read `runtime.context`.
This keeps one compiled graph reusable across requests while the per-request context is never
checkpointed.
Pointer: `app/agents/graph.py:GraphRunner._deps`, `_invoke`; `app/agents/nodes.py:node`.

### Q4. What does the `node()` decorator do?
**Answer:** Four things for every node: skip it if `status` is already set (except finalize);
`harness.begin_step(ctx, name)` so each node is a budgeted step; convert any `AppError` into
`{"status": status_for_error(exc), "error_code": exc.code}`; attach `budget = ctx.snapshot()` to
the update so the checkpoint carries counters and the ledger.
Pointer: `app/agents/nodes.py:node` (lines 85-107).

### Q5. Which conditional edges exist and what do they decide?
**Answer:** `after_plan` (retrieval required vs not, then tools vs not), `after_retrieval` (tools
vs not), `after_tools` (pending approval → `human_approval`), `after_generate` (outcome already
decided → finalize), `after_validation` (findings → `retry_or_revise`, else verify),
`after_verification` (`unsupported` → revise), `after_retry` (corrections exhausted → finalize,
else regenerate). Each first checks `status` and routes terminal failures to
`finalize_execution`.
Pointer: `app/agents/nodes.py` routers (lines 420-459).

### Q6. What does `verify_answer` check that `validate_citations` does not?
**Answer:** `validate_citations` is structural: cited IDs exist, an answered draft cites
something, the answer is non-empty (`validate_final`). `verify_answer` is semantic: a model judges
whether every claim follows from the *cited* passages/tool results, returning
`supported/unsupported/unavailable/skipped`.
Pointer: `app/agents/nodes.py:validate_citations`, `verify_answer`, `VERIFY_PROMPT`.

### Q7. How does human approval work end to end?
**Answer:** `select_or_execute_tools` calls `harness.call_tool`; for a `requires_approval` tool
whose id is not in `ctx.approved_call_ids`, the harness records `approval/pending` and raises
`ApprovalRequiredError`; the node stores the call as `pending`. `after_tools` routes to
`human_approval`, which calls `interrupt({call_id, tool, arguments})` — the run stops and the
checkpoint is saved. `GraphRunner.resume(ctx, approved=…)` sends `Command(resume=…)`; the node
marks the call approved/rejected, and `select_or_execute_tools` executes approved calls without a
new model call.
Pointer: `app/agents/harness.py:call_tool` (223-230); `app/agents/nodes.py:human_approval`,
`select_or_execute_tools`; `app/agents/graph.py:resume`.

### Q8. What happens to a rejected call?
**Answer:** It never runs. `select_or_execute_tools` adds a tool result
`{"rejected_by_reviewer": true, "reason": …}` under a T# label, so the answer can tell the user
the action was not taken and cite that.
Pointer: `app/agents/nodes.py:234-238`;
`tests/unit/test_agent_graph.py::test_rejected_approval_performs_no_write`.

### Q9. Which checkpointer is used and how is it wired?
**Answer:** `AsyncPostgresSaver` over a psycopg 3 `AsyncConnectionPool` (max 5, autocommit,
`prepare_threshold=0`, `dict_row`) on the same database, created in `Services.start()`, which the
FastAPI lifespan awaits; `setup()` creates the saver's tables. `agent_checkpointer="memory"` uses
`InMemorySaver` for tests.
Pointer: `app/services.py:Services.start`; `app/core/config.py:agent_checkpointer`.

### Q10. What are `snapshot()` and `restore()` on `ExecutionContext`?
**Answer:** `snapshot()` dumps tenant, IDs, limits, allowed tools, permissions,
`approved_call_ids`, active elapsed time, counters and the ledger to JSON. `restore()` rebuilds an
`ExecutionContext` with `elapsed_before_s` set so `started_at = clock() - elapsed`, i.e. the
deadline continues from the saved active time.
Pointer: `app/core/execution_context.py:snapshot`, `restore`, `__post_init__`.

---

## Logical

### Q11. Why does `classify_request` fall back to `document_question` on invalid output?
**Answer:** The failure direction matters: a wrong `calculation` label sets
`needs_retrieval=False`, and the answer would be produced with no evidence. Defaulting to
retrieval costs a search; the opposite error costs a grounded answer.
Pointer: `app/agents/nodes.py:classify_request`;
`tests/unit/test_agent_graph.py::test_invalid_classifier_output_falls_back_to_retrieval`.

### Q12. Why is the original question always included in the plan?
**Answer:** Query rewriting by a small model can drift. Searching the question verbatim plus up
to two rewrites means rewriting can only add recall, never lose the baseline the Day 1 `Answerer`
already had.
Pointer: `app/agents/nodes.py:plan_workflow`;
`tests/unit/test_agent_graph.py::test_original_question_is_always_searched`.

### Q13. Why does `GroundedDraft` put `citations` before `answer`?
**Answer:** With JSON-schema constrained decoding the model writes fields in schema order. With
answer first and citations optional, live qwen3:8b wrote correct but uncited RPO answers 3 times
in a row. Putting `insufficient_evidence` then `citations` first makes it commit to whether it has
evidence and which IDs it uses before writing prose; afterwards it cited `S5` on the first try.
Pointer: `app/agents/nodes.py:GroundedDraft` docstring.

### Q14. Why replay the rejected draft as an assistant turn instead of appending feedback?
**Answer:** Live, retries that appended a note to the same prompt produced the *identical* draft
at temperature 0 and the run ended `failed_validation`. Showing the draft as the model's own
previous turn followed by "Rejected: … Valid IDs: […]" is a different input, so a deterministic
model can produce a different output.
Pointer: `app/agents/nodes.py:generate_answer` (lines 297-309).

### Q15. Why does `verify_answer` use the main model rather than `small`?
**Answer:** A probe on the v2 RPO passage with a verbatim, a derived (300 s) and a wrong (15 min)
answer: qwen3:1.7b returned `supported=false` for all three (0.3-2.4 s) — no signal; qwen3:8b judged
all three correctly (0.9-6.5 s). Before the switch, the 1.7b verifier revised a correct answer
into `insufficient_evidence`.
Pointer: `app/agents/nodes.py:verify_answer` comment (353-355).

### Q16. Why verify only against the cited sources?
**Answer:** The check is "does the answer follow from what it cites", not "is it true somewhere
in the evidence". An answer that is supported only by an uncited passage still has a citation
problem; verifying against everything would hide it. It also keeps the verifier prompt short.
Pointer: `app/agents/nodes.py:verify_answer` (builds `cited` from `cited_labels(draft)`).

### Q17. Why does a missing verdict become `unavailable` and not `failed`?
**Answer:** A flaky verifier should not block every answer; but silently treating it as passed
would lie. `unavailable` is returned in `AgentRunResult.verification` so the caller/UI can show
it. It is fail-open: a stricter tenant policy could map it to `failed_validation`.
Pointer: `app/agents/nodes.py:364-365`;
`tests/unit/test_agent_graph.py::test_verifier_garbage_is_reported_not_treated_as_passed`.

### Q18. What happens if there is no evidence at all?
**Answer:** `generate_answer` returns an `insufficient_evidence` draft and sets the status without
calling the model; `after_generate` goes straight to finalize, so verify is skipped too. Cheaper
and nothing to hallucinate from.
Pointer: `app/agents/nodes.py:generate_answer` (286-292);
`tests/unit/test_agent_graph.py::test_no_evidence_answers_insufficient_without_calling_the_answer_model`.

### Q19. Why does the resume path not ask the model again?
**Answer:** The human approved specific arguments. Re-asking could produce a different call (new
id, new arguments) that nobody approved, which would then pause again or, worse, be a different
action. `select_or_execute_tools` executes the stored `pending_calls` when any exist.
Pointer: `app/agents/nodes.py:198-200`.

### Q20. What breaks if `finalize_execution` also called `begin_step`?
**Answer:** A run that ended because the step budget was exhausted would fail again in finalize,
so no `final` ledger event and no derived status would be written. Finalize is exempt so
bookkeeping always runs.
Pointer: `app/agents/nodes.py:95-96`;
`tests/unit/test_agent_graph.py::test_graph_step_budget_stops_the_run`.

### Q21. Why does the graph get its own step limit?
**Answer:** The loop counts model turns (default 8); the graph counts nodes, about 6-8 for a happy
path and 4 more per revision. `max_graph_steps` (default 20) is copied into `max_steps` by
`GraphRunner.for_graph`, and `recursion_limit = max_graph_steps + 5` is only a backstop.
Pointer: `app/core/budgets.py:max_graph_steps`; `app/agents/graph.py:for_graph`, `_config`.

---

## Advanced

### Q22. Is `human_approval` idempotent on resume?
**Answer:** Only partly. LangGraph re-runs the node from the top on resume. Everything before
`interrupt()` in the function body is side-effect free, but the `node()` wrapper calls
`begin_step` *before* the body, which increments `steps` and records a ledger event. A probe showed
5 steps in the in-memory ctx at pause vs 4 in the checkpoint, and two `step/human_approval` events
after resuming with the original ctx. A restored ctx misses the first one, so the counts depend on
how you resume.
Pointer: `app/agents/nodes.py:95-96`, `258-264`.

### Q23. What happens if two resume requests for the same execution arrive at once?
**Answer:** Both pass the `state.interrupts` check and both invoke; there is no lock. A probe with
two concurrent `resume(approved=True)` ran `save_note` twice; approve + reject concurrently ended
`answered` twice with contradictory ledgers. The fix is to serialise resumes per execution (row
lock / advisory lock and an `awaiting_approval → resuming` state transition) and key decisions by
`(execution_id, call_id)` in the `approvals` table.
Pointer: `app/agents/graph.py:resume` (109-115).

### Q24. What does a checkpoint contain, and what is the cost?
**Answer:** The whole `AgentState`: question, plan, full passage text in `evidence`, pending
calls with arguments, draft, raw `previous_output`, and `budget` with the **entire ledger**,
rewritten on every node. A probe on one approved run: 18 checkpoints, 219 ledger events stored for
a 20-event ledger. No secrets or clients are stored, but user content and tool arguments are, and
nothing prunes finished threads.
Pointer: `app/agents/nodes.py:101`; `app/core/execution_context.py:snapshot`.

### Q25. How is the deadline handled across a pause?
**Answer:** `snapshot()` stores active elapsed time; `restore()` sets `elapsed_before_s`, so the
clock restarts from that value and human wait time is free. This only applies if you resume with
`restore_context()`. Resuming with the original in-memory ctx keeps its `started_at`; a probe that
advanced the clock 1000 s ended `deadline_exceeded` and the approval was lost.
Pointer: `app/core/execution_context.py:__post_init__`, `snapshot`, `restore`;
`app/agents/graph.py:restore_context`.

### Q26. What happens if the process crashes right after an approved write executes?
**Answer:** The write runs inside `select_or_execute_tools`; the next checkpoint is only written
when that node returns. A crash in between means a replay executes the write again
(at-least-once). Also, a thread that is not at an interrupt cannot be continued: `resume` raises
`ConflictError`. Write tools need an idempotency key (the call id is a natural one).
Pointer: `app/agents/nodes.py:239-242`; `app/agents/graph.py:113`;
`app/agents/harness.py:265` (non-idempotent tools are not retried).

### Q27. How do budgets survive a restart?
**Answer:** Counters (steps, model/tool calls, tokens, cost, corrections) are in the snapshot and
restored, so a resumed run continues from them instead of getting a fresh allowance.
`test_resume_in_a_new_runner_restores_budgets_from_the_checkpoint` asserts
`model_calls == used + 2` after resume (answer + verify).
Pointer: `app/core/execution_context.py:restore`; `tests/unit/test_agent_graph.py`.

### Q28. What failure modes are not mapped to a clean status?
**Answer:** `_invoke` catches only `GraphRecursionError`. A non-`AppError` bug inside a node, a
checkpointer database error, or `asyncio.CancelledError` escapes, and the `agent_executions` row
stays `running`. `stream()` handles neither recursion errors nor resume.
Pointer: `app/agents/graph.py:_invoke` (135-143), `stream` (117-133).

### Q29. How large can the answer prompt get?
**Answer:** `_evidence_block` renders all evidence: up to 3 queries × `top_k` 5 passages × 1500
chars (~22 KB), plus a replayed draft on revision. Unlike the loop, there is no `bound_history`,
and Ollama runs with `num_ctx=8192`, so on a large corpus the prompt can be silently truncated.
Pointer: `app/agents/nodes.py:_evidence_block`; `app/tools/document_search.py:27`.

---

## Scenario

### Q30. "Users say the assistant claims their docs don't cover a topic — but they do." What do you check?
**Answer:** First the ledger: `tool_call/document_search` events with `status=error`. A known
gap: `retrieve_evidence` turns non-terminal search errors into findings that `generate_answer`
clears; with no labels it returns "No relevant passages were found" as `insufficient_evidence`. A
probe with a failing search tool reproduced it. So a retrieval outage looks like a content gap.
Fix: if every search failed, set `status="failed"` with the error code. If searches succeeded,
compare `plan` queries and passages with the eval set.
Pointer: `app/agents/nodes.py:186-190`, `286-292`, `320`.

### Q31. "A reviewer approved a note, but the note was saved twice." Diagnose.
**Answer:** Two paths: (1) two resume requests (double click, retry) ran concurrently —
`resume` has no lock, both invoked, both executed; (2) the worker crashed after the write but
before the node's checkpoint and the thread was replayed. Look for two `tool_call/save_note ok`
events with the same `call_id`. Fix: per-execution lock + state transition, and an idempotency
key on the write tool.
Pointer: `app/agents/graph.py:resume`; `app/agents/nodes.py:239-242`.

### Q32. "Approved runs from last night all ended `deadline_exceeded`." Why?
**Answer:** Most likely the resume path used a cached in-memory `ExecutionContext` instead of
`restore_context()`, so the wait counted against `deadline_s` (180 s default). `begin_step` in
`human_approval` raised `DeadlineExceededError`, the node never applied the decision, and no write
happened. Always rebuild the context from the checkpoint before `resume`.
Pointer: `app/agents/graph.py:resume`, `restore_context`.

### Q33. "Correct answers are being turned into 'insufficient evidence' after revision." What happened?
**Answer:** That was observed live with qwen3:1.7b as the verifier: it labelled every answer
unsupported, `retry_or_revise` fed that back, and the model eventually said
`insufficient_evidence`. Check the ledger for `verify` calls and their model; probe the verifier
with known-good/known-bad answers. The fix was to use `main` for `verify_answer`.
Pointer: `app/agents/nodes.py:verify_answer`.

### Q34. At 10x traffic on the same M4, latency doubles. What is going on and what do you change?
**Answer:** Live runs already took 45-87 s for the graph; the graph alternates `small`
(classify, plan) and `main` (tools, answer, verify), which on 16 GB likely forces Ollama to swap
models, and concurrent runs queue on one GPU. Options: route easy questions to the `Answerer`;
use `main` for classify/plan too so one model stays resident; skip `select_or_execute_tools` when
`needs_tools` is false (already done); queue runs with a worker concurrency limit. Measure before
choosing — the swap explanation is a hypothesis.
Pointer: `app/agents/nodes.py:classify_request`, `plan_workflow`; journal 04 §4.

---

## Trade-off

### Q35. Fixed graph vs model-driven loop: which won live, and when does the other win?
**Answer:** Same question, same model, this session: graph 4/4 including "300 seconds" and
`verification=supported`; the loop got the value right but omitted "300 seconds" 2/2 (it had
passed 2/2 earlier). The graph's fixed verify+revise path made it more complete at similar
latency (45-87 s vs 48-108 s). The loop wins when the next search depends on what the first one
found (multi-hop), because the graph retrieves once, up front.
Pointer: `app/agents/graph.py` docstring; `tests/live/test_agent_live.py`.

### Q36. Answerer vs loop vs graph: how do you choose?
**Answer:** `Answerer`: single-hop lookups, cheapest and most predictable, the eval baseline.
Loop: unknown number/order of tool calls. Graph: fixed order required (verify before
returning), pause/resume for a human, or a different model/prompt per step. Approval-required
tools are graph-only: the loop removes them from `allowed_tools`.
Pointer: `app/agents/graph.py` module docstring; `app/agents/custom_loop.py:103-107`.

### Q37. Snapshot the whole ExecutionContext into state vs store only an execution id and reload from DB?
**Answer:** Chosen: snapshot into state. Pause/resume works with only the checkpointer, and a new
process restores exact counters. Cost: the full ledger is rewritten every node and duplicated
with `execution_events`. Storing only counters + last `seq` and streaming events to the DB wins
once runs get long or numerous.
Pointer: `app/agents/nodes.py:101`; `app/agents/store.py:ExecutionStore.save`.

### Q38. Postgres checkpointer vs in-memory saver?
**Answer:** Postgres in prod: paused runs survive a restart and any worker can resume
(integration test resumes in a fresh `GraphRunner`). In-memory for unit tests: fast, no DB, but
pending approvals vanish on restart. The cost of Postgres is a second driver (psycopg 3 next to
asyncpg) and a second pool.
Pointer: `app/services.py:Services.start`;
`tests/integration/test_agent_graph_db.py::test_graph_pauses_persists_and_resumes_after_restart`.

---

## Justification

### Q39. "Every node calling `begin_step` is overhead. LangGraph has a recursion limit."
**Answer:** The recursion limit counts super-steps, raises an exception, and knows nothing about
cancellation, deadlines, or the ledger. `begin_step` checks cancel and deadline, enforces
`max_steps`, and records a `step` event, so a node budget stop becomes a clean `budget_exhausted`
with a `final` event. §7 says budgets are enforced through the harness. The recursion limit stays
as a backstop.
Pointer: `app/agents/harness.py:begin_step`; `app/agents/graph.py:_config`.

### Q40. "Adding a verifier doubles the cost of a correct answer."
**Answer:** One extra `main` call (0.9-6.5 s in the probe) per answered run; insufficient-evidence
drafts skip it. In exchange, the graph rejects answers whose claims do not follow from their own
citations, which `validate_final` cannot see. The probe also showed the cost of a *bad* verifier
(1.7b): correct answers revised into refusals. So the verifier must be validated like any model.
Pointer: `app/agents/nodes.py:verify_answer`.

### Q41. "Why not let the model re-plan after approval? It knows more now."
**Answer:** Approval is consent to a specific action with specific arguments. Re-planning could
produce a different action that nobody approved. The resume path executes exactly the stored
calls; the model only sees the results in `generate_answer`.
Pointer: `app/agents/nodes.py:select_or_execute_tools` (198-200).

### Q42. "Why keep the custom loop at all now that the graph exists?"
**Answer:** It is the baseline and the better tool for open-ended multi-hop questions, and it
shares the harness, evidence store and result type, so the Day 3 eval can compare
`mode="loop"` vs `mode="graph"` on the same questions. The live parametrised test already does.
Pointer: `app/agents/result.py:AgentRunResult.mode`; `tests/live/test_agent_live.py`.

---

## System design

### Q43. How does the graph fit into the platform?
**Answer:** API → `ExecutionContext` → `ExecutionStore.start` → `services.graph.run` →
nodes call `Harness` (policy + ledger) → tools scoped by `ctx.tenant_id` → `EvidenceStore` →
checkpoint after each node in Postgres → `AgentRunResult` → `ExecutionStore.save` (status
`awaiting_approval` leaves `finished_at` empty). Approval: `pending_approval` → human →
`restore_context` + `resume` → `save` again (ledger insert-or-ignore by `seq`). Missing: HTTP
endpoints, writes to the `approvals` table, a tenant check before `restore_context`, and SSE via
`ctx.listeners`.
Pointer: `app/services.py`; `app/agents/graph.py`; `app/agents/store.py`.

### Q44. How would you evolve this for many tenants and concurrent reviewers?
**Answer:** Serialise each execution (row/advisory lock, explicit state machine
`running → awaiting_approval → resuming → done`); bind decisions to `call_id` with a unique row in
`approvals`; scope thread lookups by tenant; always resume from the checkpoint; pass idempotency
keys to write tools; slim checkpoints (counters + last ledger `seq`, evidence by chunk id) and
prune finished threads; run nodes on queue-backed workers sharing a GPU pool, routing simple
questions to the `Answerer`.
Pointer: journal 04 §8-9.
