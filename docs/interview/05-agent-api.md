# Interview Prep 05 — Agent API, SSE Streaming, Approvals, CLI (Day 2, M5)

Every answer is grounded in the code from commit `8f648aa`. Format: direct answer, then the
reasoning, then a code pointer. Companion: `docs/journal/05-agent-api.md`.

---

## Basic

### Q1. What does the agent API expose?
**Answer:** `POST /api/v1/agent/run` (run to completion), `POST /agent/run/stream` (same run as
Server-Sent Events), `GET /executions/{id}` and `/executions/{id}/trace` (persisted outcome and
ledger), `GET /approvals`, `POST /approvals/{id}/approve|reject`. All require `X-API-Key` and are
scoped to the key's tenant.
Pointer: `app/api/agent.py`.

### Q2. What is in an `AgentRunRequest`?
**Answer:** `question` (1-2000 chars), `mode` (`graph` default, or `loop`), `limits`
(`LimitOverrides`: steps, graph steps, model calls, tool calls, total tokens, deadline — each
optional) and `tools` (an optional allowlist, ≤ 20 names).
Pointer: `app/api/agent.py:21-27`; `app/agents/service.py:29-37`.

### Q3. Which events does the SSE stream send, in which order?
**Answer:** `start` (execution id, mode) → ledger events as they are recorded (`step`,
`model_call`, `tool_call`, `correction`, `approval`, `policy`) → one `citation` per cited source →
`final` with the full result minus `ledger`. The integration test asserts `start` first, `final`
last, and `tool_call < citation < final`.
Pointer: `app/api/agent.py:117-127`;
`tests/integration/test_agent_api.py::test_stream_emits_progress_citations_and_final`.

### Q4. How do progress events get from the agent to the HTTP response?
**Answer:** `ExecutionContext.record()` calls every function in `ctx.listeners`.
`AgentService.stream` appends `queue.put_nowait`, runs `run()` as a task, and waits on
`queue.get()` and the task with `asyncio.wait(FIRST_COMPLETED)`, yielding each event; after the
task finishes it drains the queue and yields the result.
Pointer: `app/core/execution_context.py:80-85`; `app/agents/service.py:78-98`.

### Q5. What is `AgentService` for?
**Answer:** It is the single entry point for executions from the API and the CLI: build the
context (limits, tool allowlist), create the execution row, run loop or graph, persist on every
exit path, create approval rows when the graph pauses, and resume after a decision.
Pointer: `app/agents/service.py`.

### Q6. What does the trace endpoint return?
**Answer:** The persisted ledger for the execution, ordered by `seq`: every step, model call,
tool call, correction, approval and the `final` event, with status, error code, latency, tokens
and data. It first checks the execution belongs to the caller's tenant.
Pointer: `app/api/agent.py:143-149`; `app/agents/store.py:ExecutionStore.events`.

### Q7. What does the CLI `run` command do?
**Answer:** `agentops run "question" --mode graph|loop --tenant … --trace` starts the services
(including the checkpointer), calls `AgentService.run`, and prints the status and answer,
citations, tool references, a counters line, and with `--trace` one line per ledger event. Because
it goes through the service, CLI runs are persisted like API runs.
Pointer: `app/cli.py:116-148`.

### Q8. How are approvals listed and decided?
**Answer:** `GET /approvals?status=pending` lists the tenant's approvals with tool name and
arguments. `POST /approvals/{id}/approve|reject` (optional `reason`) claims the decision,
restores the paused run from its checkpoint and resumes it, returning the new result. Already
decided → 409; unknown/other tenant → 404.
Pointer: `app/api/agent.py:152-193`; `app/agents/service.py:100-114`.

---

## Logical

### Q9. Why can request limits only tighten?
**Answer:** The server owns the ceiling (§5): `tightened` keeps `min(default, requested)` for each
key and rejects unknown keys. A client may ask for a cheaper run, never a more expensive one. The
test sends `max_steps=10_000` and the stored limit is 8.
Pointer: `app/core/budgets.py:40-51`;
`tests/integration/test_agent_api.py::test_request_limits_only_tighten_and_unknown_tools_rejected`.

### Q10. Why is the context built before the stream starts?
**Answer:** Once `200 text/event-stream` has been sent, an error can only be an in-band event.
Unknown tools or invalid limits are configuration errors and should be ordinary 4xx JSON
responses, so `new_context` runs in the route before `StreamingResponse` is returned.
Pointer: `app/api/agent.py:112-115`.

### Q11. Why does the `final` SSE event drop `ledger`?
**Answer:** Every ledger event was already streamed one by one; resending the full ledger would
double the payload. The full ledger is still available from `/executions/{id}/trace`.
Pointer: `app/api/agent.py:126`.

### Q12. Why stream ledger events instead of LangGraph node updates?
**Answer:** One mechanism serves both loop and graph; the stream shows exactly what the trace will
show later; and node updates contain internal state (evidence text, budget snapshots) that should
not be sent to clients. The node-update `GraphRunner.stream` was removed.
Pointer: `app/agents/service.py:78-98`; commit message `8f648aa`.

### Q13. Why does `_execute` re-raise `CancelledError` after saving?
**Answer:** Swallowing cancellation breaks asyncio: the canceller (the stream's `finally`, or
Starlette) expects the task to end cancelled. So `_execute` records the outcome, saves it under
`asyncio.shield` so the save itself is not cancelled, then re-raises.
Pointer: `app/agents/service.py:126-133`.

### Q14. Why is `ConflictError` re-raised without saving?
**Answer:** It means "this run is not waiting for approval": nothing ran, so the execution's
stored state is still correct and must not be overwritten with a failure. The API maps it to 409.
Pointer: `app/agents/service.py:134-135`; `app/agents/graph.py:119-121`.

### Q15. What happens when a decision's `call_id` does not match the pending call?
**Answer:** `human_approval` turns it into a rejection ("decision did not match the pending
call"): the tool never runs and the answer gets a `rejected_by_reviewer` tool result. Approval is
consent to a specific call; anything else fails closed.
Pointer: `app/agents/nodes.py:270-273`;
`tests/unit/test_agent_graph.py::test_decision_for_a_different_call_is_not_an_approval`.

### Q16. Why does `AgentService` take the graph as a factory?
**Answer:** The `GraphRunner` needs the checkpointer, which is opened in the async
`Services.start()`, but `build_services` is synchronous. `lambda: services.graph` defers the
lookup to call time and raises if `start()` was not awaited.
Pointer: `app/services.py:161-169`.

---

## Advanced

### Q17. How do concurrent approvals resume exactly once?
**Answer:** The claim is one conditional UPDATE: `UPDATE approvals SET status=… WHERE id=:id AND
tenant_id=:t AND status='pending' RETURNING *`. Postgres row locking serialises the updates; the
first commits, the others re-check the WHERE against the new row version, match nothing, and get
no row → 409. Only the winner calls `resume`. Test: 4 parallel approves → `[200, 409, 409, 409]`,
one write.
Pointer: `app/agents/store.py:141-168`;
`tests/integration/test_agent_api.py::test_concurrent_approvals_resume_once`.

### Q18. Why a conditional UPDATE rather than `SELECT … FOR UPDATE`?
**Answer:** It is atomic in one round trip and holds the row lock only for the update, not for the
multi-second resume. `FOR UPDATE` held across the resume would block other reviewers and tie a DB
transaction to model latency. Cost: the claim commits before the resume, so a failed resume leaves
the approval `approved` with nothing executed (deferred).
Pointer: `app/agents/store.py:141-168`; `docs/day-checklist.md` Day 2 deferred.

### Q19. How does the code distinguish 404 from 409 when the UPDATE matches nothing?
**Answer:** A second query reads the approval's status by id and tenant: no row → `NotFound` (also
for another tenant's id, which does not leak existence); a row → `ConflictError("Approval already
approved|rejected")`.
Pointer: `app/agents/store.py:160-168`.

### Q20. Why is approval creation an upsert?
**Answer:** A paused state can be saved more than once; `ON CONFLICT (execution_id, call_id) DO
UPDATE SET call_id = call_id RETURNING *` returns the existing row instead of a second approval.
`DO UPDATE` instead of `DO NOTHING` because `DO NOTHING` returns no row on conflict.
Pointer: `app/agents/store.py:109-131`.

### Q21. How does a client disconnect become a persisted `cancelled` execution?
**Answer:** When the consumer stops (Starlette cancels or closes the generator), the `finally` in
`AgentService.stream` sees the run task still running, calls `ctx.cancel("client disconnected")`
and `task.cancel()`, and awaits it. Inside the task, `_execute` catches `CancelledError`, records
a `final/error execution_cancelled` event, saves status `cancelled` with `finished_at`, then
re-raises. Tested by closing the generator mid-model-call.
Pointer: `app/agents/service.py:94-98, 126-133`;
`tests/integration/test_agent_api.py::test_client_disconnect_cancels_and_persists`.

### Q22. What does that disconnect test not cover?
**Answer:** It calls `aclose()` on `AgentService.stream` directly, not through Starlette and the
`BaseHTTPMiddleware` request middleware. Through HTTP, a disconnect cancels the response task;
if the outer `events()` generator is parked at a `yield`, the inner generator is only closed when
garbage-collected. Also untested: cancellation landing inside `ExecutionStore.start`, before
`_execute`, which would leave the row `running`.
Pointer: `app/api/agent.py:117-127`; `app/main.py:73`.

### Q23. What happens to the execution row during a resume?
**Answer:** Nothing until the resume ends: it stays `awaiting_approval` (it is not moved to
`running`), then `_execute` saves the new outcome; the ledger insert skips events already stored
by `(execution_id, seq)`. If the process dies mid-resume, the row stays `awaiting_approval` with
the approval already `approved` — a stuck run.
Pointer: `app/agents/service.py:100-147`; `app/agents/store.py:43-78`.

### Q24. Can an SSE client reconnect and continue?
**Answer:** No. Events carry no `id:`, so there is no `Last-Event-ID`, and a disconnect cancels the
run. Recovery is reading `/executions/{id}` and `/trace` afterwards.
Pointer: `app/api/agent.py:87-88`.

---

## Scenario

### Q25. "Behind our nginx, streams die after about a minute and the runs show `cancelled`." Why?
**Answer:** No heartbeat. Ledger events are recorded after a model call finishes; live,
`select_tools` took 79 s under memory pressure with nothing on the wire. nginx's default
`proxy_read_timeout` is 60 s, it closes the upstream, Starlette sees a disconnect, and the run is
cancelled by design. Fix: an SSE comment (`: keep-alive`) every ~15 s, plus a longer proxy timeout;
longer term, decouple runs from connections.
Pointer: `app/agents/service.py:78-98`; journal 05 §4, §8 item 2.

### Q26. "A reviewer clicked approve, the request timed out, they clicked again and got 409. Was the note saved?"
**Answer:** The first request claimed the approval and is (or was) running the resume
synchronously; the 409 is the claim working. Check `GET /executions/{id}` and its trace for the
`approval/ok` and `tool_call` events. If the first resume died, the approval is `approved`, the
execution `awaiting_approval`, and nothing ran — the known deferred gap.
Pointer: `app/agents/service.py:100-114`; `docs/day-checklist.md` deferred.

### Q27. "The dashboard shows executions stuck in `running`." Where can that still come from?
**Answer:** `_execute` persists every outcome of the run itself, so look outside it: a process
crash/OOM-kill (nothing runs), cancellation during `ExecutionStore.start`, or a failing `save`
(DB down). On the resume path, a crash leaves `awaiting_approval`, not `running`. Fix: a sweeper
that marks rows past their deadline as `failed`/`deadline_exceeded`.
Pointer: `app/agents/service.py:74-76, 121-147`.

### Q28. "Users report the stream just stops, no `final` event." What do you check?
**Answer:** An exception that `_execute` does not convert into a result — e.g. `ExecutionStore`
failing (DB down) — surfaces as `task.result()` raising inside the generator; the response is
truncated with no in-band error. Check server logs for the exception; add an `error` SSE event in
the route's generator.
Pointer: `app/agents/service.py:93`; `app/api/agent.py:117-127`.

### Q29. "The live CLI run took 153 s for one question. Is the graph too slow?"
**Answer:** Not from that run. It made 6 model calls with one citation correction, and the Mac had
~15.5 GB of 16 GB swap in use (other Docker stacks + qwen3:8b at 8k context); `select_tools`
alone took 79 s vs ~15 s earlier. The answer was correct and verified. Re-measure on a quiet
machine and record memory state with every timing. Also worth asking why `select_tools` ran at
all: `after_retrieval` only routes there when the classifier set `needs_tools`, and a pure lookup
should not need it.
Pointer: journal 05 §4.

---

## Trade-off

### Q30. SSE vs WebSocket vs polling for agent progress?
**Answer:** **SSE**: server→client only (all a run needs), plain HTTP so API-key headers, proxies
and `curl -N` work, text framing, browser `EventSource` with auto-reconnect. Weakness: one-way,
needs heartbeats, and (here) no event ids. **WebSocket**: bidirectional — useful only if the
client must send mid-run messages (cancel, approve in-band); costs a protocol upgrade, custom
framing, harder auth and proxy config. **Polling** `/executions/{id}/trace`: simplest, survives
disconnects, works everywhere, but adds latency and DB load per client. Chosen: SSE for live
progress, the trace endpoint as the durable fallback.
Pointer: `app/api/agent.py:101-133, 143-149`.

### Q31. Cancel on disconnect vs keep running in the background?
**Answer:** Cancel: nobody is listening, local GPU time is scarce, and the outcome is persisted as
`cancelled`. Keep running: a network blip does not waste completed work, and a client could fetch
the result later. With no reconnect and no queue, cancel is the safer default on one box; at scale
(runs on workers, resumable streams) keep-running plus an explicit cancel endpoint is better.
Pointer: `app/agents/service.py:94-98`.

### Q32. Synchronous approve vs 202 + async resume?
**Answer:** Synchronous: the reviewer sees the outcome, no extra machinery. Cost: the request lasts
for the rest of the run (minutes on this hardware), timeouts cause retries that hit 409, and there
is no stream for the resumed part. Async (202 + event stream) is the scale answer.
Pointer: `app/api/agent.py:162-193`.

### Q33. One wrapper that persists everything vs persisting in each caller?
**Answer:** One wrapper (`_execute`) used by `run`, `stream` (via `run`) and `decide` means no
entry point can forget the cancel/exception paths. Cost: it must choose the semantics for
everyone (re-raise cancel, re-raise conflict, swallow and record other exceptions).
Pointer: `app/agents/service.py:121-147`.

---

## Justification

### Q34. "Why not just use a Python lock around resume?"
**Answer:** It only works in one process. Two API workers (or a CLI and the API) would both pass.
The conditional UPDATE is enforced by Postgres for every process; the database is already the
source of truth for approvals.
Pointer: `app/agents/store.py:141-168`.

### Q35. "Treating a mismatched call id as a rejection is surprising. Why not 409?"
**Answer:** By the time `human_approval` sees the decision, the approval row is claimed and the run
is resuming; the node can only decide what to do with this pending call. Rejection is the safe
reading of "this consent was for something else": nothing runs and the outcome is visible in the
answer and the ledger. A future pre-check against `pending_approval()` could return 409 earlier.
Pointer: `app/agents/nodes.py:270-273`.

### Q36. "`asyncio.shield` on the save — isn't that hiding cancellation?"
**Answer:** No: the `CancelledError` is re-raised right after. `shield` only protects the inner
save from a second cancellation while it writes, so the row does not stay `running` because the
cleanup itself was cancelled.
Pointer: `app/agents/service.py:132-133`.

### Q37. "The approvals API exists but nothing can trigger it in production. Why ship it?"
**Answer:** The registered tools (`document_search`, `calculator`, `document_metadata`) are
read-only and contexts get only `documents:read`; approval-gated write tools arrive with the Day 3
workspace tools. Shipping the claim/resume path now, tested with a test-only tool and a 4-way race,
means the risky concurrency part is done before a real write exists. Open item: where per-key
permissions come from.
Pointer: `app/core/execution_context.py:23`; `tests/integration/test_agent_api.py:23-24`.

---

## System design

### Q38. Walk through a streamed graph run that needs approval.
**Answer:** `POST /agent/run/stream` → `new_context` (limits tightened, tools checked) → `start`
event → `AgentService.stream` registers a listener and starts `run` → `ExecutionStore.start`
(`running`) → graph nodes record ledger events, each streamed → `select_or_execute_tools` gets
`ApprovalRequiredError` → `human_approval` interrupts, checkpoint saved → `_execute` saves
`awaiting_approval` and creates the approval row → `final` event (status `awaiting_approval`).
Later: `GET /approvals` → `POST …/approve` → conditional UPDATE claim → `restore_context` (budget
and ledger from the checkpoint, wait time excluded) → `resume(call_id=…)` → tool runs → answer →
`_execute` saves `answered`.
Pointer: `app/api/agent.py`; `app/agents/service.py`; `app/agents/graph.py`.

### Q39. How would you redesign this for 100x traffic?
**Answer:** `POST /agent/run` returns 202 with an execution id; runs execute on queue-backed
workers; `/executions/{id}/events` is an SSE stream from the persisted ledger (or
`LISTEN/NOTIFY`/Redis streams) with `id: <seq>` so clients resume via `Last-Event-ID`; heartbeats;
explicit cancel endpoint; one-row state machine `running → awaiting_approval → resuming →
terminal` with the approval claim and the state transition in one transaction and a sweeper for
dead workers; async approve; idempotency keys on run requests and write tools; scopes per API key;
per-tenant concurrency limits in front of the shared GPU.
Pointer: journal 05 §9.

### Q40. What are the Day 2 lessons you would bring to any agent project?
**Answer:** Put every model/tool call behind one policy gate with a ledger; make "who decides the
path" explicit (code vs model); treat schema field order and retry inputs as prompt design; test
the verifier like any other model; bind human approvals to a specific call and claim them
atomically; persist every exit path; and record machine state with every latency number.
Pointer: journal 05 §7.
