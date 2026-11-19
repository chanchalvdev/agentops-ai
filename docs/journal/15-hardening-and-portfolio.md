# Journal 15 — Hardening and Portfolio: Durable Approvals and Indexing, Docker Sandbox, SSE Reconnect, Eval-Driven Quality Fixes

Milestone: the post-Day-5 hardening pass on branch `hardening-and-portfolio` (Wed 18 – Thu 19
Nov), two commits:
- `ae7c3c3` feat: approvals expiry/recovery/202, durable indexing, docker exec sandbox, SSE
  reconnect, stricter evals (52 files, +1960/−371);
- `ed27635` fix: eval-driven answer quality fixes, reindex CLI, use-cases doc, docs refresh
  (12 files, +576/−100).

It closes most of the carry-overs listed at the end of journal 11 §10 ("durable queue, approval
expiry and claim-then-resume recovery, synchronous approve, SSE reconnect, workspace OS sandbox,
history trimming") and the two golden cases that failed in every profile (`multi-retention`,
`ws-edit-approved`). Written by the Recorder from the code, the commit diffs, the tests and
`reports/check-two.json`; facts reported by the build session and not visible in a file are
marked as such.

Status (re-run by the Recorder on the committed tree; `.github/workflows/ci.yml` and
`docs/day-checklist.md` had uncommitted edits at the time):
- `uv run pytest -q -W error tests/unit tests/security` → **308 passed** (includes the 3 Docker
  sandbox tests, which ran against a real container because `agentops-sandbox:1` was built).
- `uv run pytest -q -W error tests/integration` → **92 passed** (Postgres reachable this time).
- `uv run pytest --collect-only -q` → **400/407 collected, 7 deselected** (live Ollama).
- `ruff check app tests` clean; `ruff format --check` → 135 files formatted; `mypy app` (strict)
  → no issues in 100 source files.

Final live evaluation: 37/38 (97%) on the optimized profile with the stricter checks (was 36/38): multi_document 5/5, workspace 5/5, adversarial 7/7, p50 27.4 s, p95 58.8 s. One case still fails: `tool-rto-diff` (the calculator returns 45, but the verifier rejects the derived figure), which passed in an isolated probe and is the next fix. Report: `datasets/baselines/golden-v1-graph-qwen3-8b-hardening.json`

---

## 1. What was built, by subsystem

### 1.1 Approvals: 202, background resume, expiry, recovery (`app/agents/service.py`, `app/agents/store.py`, `app/api/agent.py`)

Before: `POST /approvals/{id}/approve` held the HTTP request open for the whole resumed run
(minutes on a local model), approvals never expired, and a process that died between "approval
marked approved" and "run saved" left the approval decided and the run paused forever.

- **Claim, then resume.** `AgentService.claim` does the conditional `pending → approved|rejected`
  UPDATE (`ApprovalStore.decide`), restores the paused context from the checkpoint, tightens its
  permissions to the caller's current ones, and checks the execution is still
  `awaiting_approval`. If anything fails before the resume starts, `ApprovalStore.release` puts
  the approval back to `pending` (under `asyncio.shield`, so a cancelled request still releases).
- **202 by default.** `decide_in_background` claims, then starts `_execute(...)` as a task kept
  in `self._resumes`, and the route returns `202 {"approval_id", "execution_id", "status":
  "resuming"}` with `Location: /api/v1/executions/{id}`. `?wait=true` keeps the blocking form
  (`AgentService.decide`), used by the CLI and the evaluation runner.
- **TTL expiry.** `ApprovalStore.expire(ttl_s)`: pending approvals older than
  `approval_ttl_s` (default 24 h) become `expired`, and their executions `cancelled` with
  `error_code="approval_expired"`, in one transaction. Their checkpoints are deleted
  (`GraphRunner.forget`). A late approve gets 409.
- **Reopening lost resumes.** `ApprovalStore.recover_stuck(after_s)`: a decided approval whose
  execution is still `awaiting_approval` more than `approval_resume_grace_s` (default 1200 s)
  after the decision lost its resume; it goes back to `pending` with the reason "the resume after
  this decision did not complete; decide again". Re-running an approved write is safe because
  write tools are idempotent per (execution, call) through their receipts (journal 06).
- **Housekeeping is lazy.** `AgentService.maintain` runs `expire` and `recover_stuck` whenever
  approvals are listed or decided (`_maintain` in `app/api/agent.py`); there is no scheduler.
- **Pause and approval in one transaction.** `ExecutionStore.save(ctx, result, approval=...)`
  writes the `awaiting_approval` status, the new ledger events and the approval row
  (`_approval_insert`, upsert on `uq_approvals_execution_call`) together. Before, the status was
  saved and the approval created in a second step, so a crash between them left a paused run with
  nothing to approve.
- **Checkpoint cleanup.** `GraphRunner.forget(execution_id)` calls the checkpointer's
  `adelete_thread`. `_execute` calls it for every graph run that ends in a non-paused status;
  checkpoints exist only to resume a paused run, and kept forever they retain passage text and tool
  arguments.
- **Ledger only in resumable checkpoints.** `ExecutionContext.snapshot(with_ledger=...)`; the
  `node()` wrapper in `app/agents/nodes.py` passes `with_ledger=name in _RESUMABLE_FROM`
  (`select_or_execute_tools`, `human_approval`). Every other checkpoint records only
  `ledger_len`. Before, every node's checkpoint copied the whole, growing ledger again.
- `aclose()` cancels in-flight resumes and live streams on shutdown; each is saved `cancelled`.

### 1.2 Durable indexing: the documents table is the queue (`app/ingestion/pipeline.py`, `app/services.py`, migration 0007)

Before: an upload scheduled `process_document` with FastAPI `BackgroundTasks`; a crash or
restart left the document `PENDING` or `PROCESSING` forever. Reindexing flipped the document out
of `READY`, so it vanished from search until the new index landed (or for good, if it failed).

- **Columns (0007):** `searchable` (has a complete index that retrieval may serve), `attempts`,
  `lease_until`, and a partial index `ix_documents_queue (status, lease_until) WHERE status IN
  ('pending','processing')`. The migration back-fills `searchable = (status = 'ready')`.
- **Atomic claim.** `IngestionService._claim`: one `UPDATE … WHERE id = :id AND (status =
  PENDING OR (status = PROCESSING AND lease_until < now)) RETURNING …` sets `PROCESSING`,
  `attempts + 1` and a fresh lease (`indexing_lease_s`, default 600 s). Exactly one caller wins:
  the upload's own task, the worker or the CLI. A loser waits for the outcome (`_wait_for`,
  bounded by the lease).
- **Heartbeat.** `_heartbeat` extends the lease every `lease/3` while `_index` runs, so a slow
  but alive embedding run is not reclaimed.
- **Poison documents.** If the claim makes `attempts > indexing_max_attempts` (default 3), the
  document is failed with "Gave up after N attempts (the process indexing it kept dying)".
  `attempts` resets to 0 on success.
- **Worker.** `IngestionService.pending_work(grace_s)` lists documents `PENDING` for longer than
  the grace (the request's own task should have taken them) or `PROCESSING` with an expired lease.
  `Services.start_indexing_worker` polls it every `indexing_poll_s` (15 s) and calls
  `process_document` for each. `create_app` starts the worker only when it built the services
  itself (`owned`), so tests that inject shared services and the CLI do not run it. Uploads still
  start indexing at once in their request; the worker only recovers what a crash left.
- **`searchable` vs `status`.** `scoped_select` in `app/retrieval/vector_search.py` now filters
  `Document.searchable.is_(True)` instead of `status == READY`. `mark_for_reindex` sets `PENDING`
  but leaves `searchable` alone; `_index` replaces the chunks in one transaction; a failed reindex
  (`_fail(..., keep_index=True)`) returns the document to `READY` with the error "Reindex failed;
  the previous index is still served". A first-time failure still deletes chunks and sets
  `FAILED`, `searchable=False`, `is_latest=False` (the Day 1 invariant: never partial).
- **Version lifecycle.** `delete` of the latest version re-promotes the newest remaining
  `searchable` version of the same filename. Re-uploading the bytes of an older version
  (`_resurface`) makes it latest again; re-uploading a `FAILED` document's bytes queues it again.
  `_index` does not demote a newer ready version when an old one is reindexed (`newer_ready`).

### 1.3 Docker exec sandbox, terminal limits, receipt pruning (`app/workspace/executor.py`, `docker/sandbox.Dockerfile`, `app/api/workspace.py`, `app/workspace/jail.py`)

- **`DockerSandbox`** (frozen dataclass): `argv()` wraps the already-planned command
  (`plan_command` still applies the allowlist and path jail) in `docker run --rm --name
  agentops-exec-<12 hex> --network none --cap-drop ALL --security-opt no-new-privileges
  --read-only --tmpfs /tmp:rw,size=64m --cpus 1 --memory 512m --pids-limit 128 --user
  10001:10001 --volume <tenant dir>:/workspace:rw --workdir /workspace`, with a scrubbed
  environment passed as explicit `--env` flags. The docker CLI itself runs with the server's
  environment (it needs its socket and config); the container gets only the scrubbed set.
- `run_streaming(..., sandbox=)`: on timeout or early close it kills the CLI's process group and
  then calls `sandbox.remove(name)` (`docker rm -f`), because killing the CLI does not stop the
  container.
- Selected with `AGENTOPS_WORKSPACE_EXEC_BACKEND=docker` (default `host`); image, CPUs and memory
  are settings. `docker/sandbox.Dockerfile`: `python:3.13-slim` + git + `pytest==8.3.5`, user
  10001, `WORKDIR /workspace`.
- **`tests/security/test_docker_sandbox.py`** (skipped unless the image exists): pytest runs
  inside the container against the workspace; a workspace test file proves no network
  (`connect(('1.1.1.1', 53))` fails), no leaked host env var, no `/Users`, read-only root, non-root
  uid (5 passed inside); a timed-out command leaves no `agentops-exec-*` container.
- **Terminal rate limit.** `_terminal_rate_limit` takes a token from the same `workspace` bucket
  as `POST /workspace/exec`, so the WebSocket is no longer a way around the HTTP limit (journal
  11 limitation 3).
- **Disconnect detection.** `_run_watched` forwards one command's output while also awaiting
  `websocket.receive()`. A `websocket.disconnect` mid-command cancels the forwarder, which closes
  the stream and kills the process group, at once rather than when the next output line would have
  failed to send. Frames sent while a command runs get `{"type": "error", "code": "busy"}`; a
  frame that arrives after the exit frame is returned as the next command.
- **Receipt pruning.** `Workspace._prune_receipts` deletes receipts older than
  `workspace_receipt_ttl_s` (7 days, well past the 24 h approval TTL), at most once an hour per
  workspace (module-level `_LAST_PRUNE`), on the write path.

### 1.4 SSE reconnect (`app/agents/service.py`, `app/api/agent.py`, `web/src/lib/api.ts`, `web/src/lib/sse.ts`)

- **Event ids.** `_sse(event, data, event_id)` writes `id: <seq>` for each ledger event and
  `id: end` for the closing `citation`/`final`/`error` events.
- **`_LiveRun` registry.** `stream()` creates the run task, wraps it in a `_LiveRun(ctx, task)`
  stored in `self._live[execution_id]`, and registers a ledger listener and done-callbacks that
  call `live.wake()`. Any number of followers can attach; each owns an `asyncio.Event` in
  `live.wakers`.
- **`_follow(live, after_seq, …)`** replays `ctx.ledger[after_seq+1:]`, then waits on its event
  (with a heartbeat timeout), drains again, and on completion yields `result` or `error`. The
  event is cleared *before* draining, so an event appended during the drain sets it again and is
  not missed. The old implementation used one `asyncio.Queue` per stream, which could not be
  replayed or shared.
- **Reconnect grace.** In `_follow`'s `finally`, when the last follower leaves and the run is not
  done: with `grace_s <= 0` the run is stopped at once (previous behaviour); otherwise a reaper
  task (`stop_unless_followed`) sleeps `sse_reconnect_grace_s` (default 30 s) and cancels the run
  only if nobody has reattached.
- **`GET /executions/{id}/stream`** with `Last-Event-ID`: if the run is live in this process
  (`live_run`, tenant-checked), `follow()` continues it; otherwise `_stored_items` replays the
  stored events after the id and closes with the stored outcome (`"replayed": true`), or with
  `execution_not_live_here` if the row is still `running` (another process, or a dead one).
  `Last-Event-ID: end` means "I have everything but the closing events".
- **The `aclosing` bug.** `stream()` and `follow()` iterate the inner `_follow` generator. When
  Starlette closes the outer generator on disconnect, the inner one was left suspended; its
  `finally` (remove the waker, decide whether to stop the run) only ran when it was
  garbage-collected. The existing `test_client_disconnect_cancels_and_persists` (grace 0, expects
  the row `cancelled` right after `aclose()`) caught it. Fix: `async with
  contextlib.aclosing(self._follow(...))`. Journal 05 had flagged the same class of problem as
  "closed when garbage-collected, not covered by a test".
- **Web client.** `streamAgent` records the last `id:` (`SseParser` now parses it) and the
  `execution_id` from `start`; if the stream ends without `final`/`error`, it reconnects to
  `GET /executions/{id}/stream` with `Last-Event-ID`, up to 5 times with backoff
  (`min(500·2^n, 8000)` ms), skipping the repeated `start`. A 4xx stops retrying.
  `api.decide` posts the decision (202) and polls the execution with backoff (500 ms → 4 s) until
  it leaves `awaiting_approval`/`running`.

### 1.5 Evaluation and monitoring (`app/evaluation/evaluators.py`, `app/evaluation/store.py`, `app/monitoring/drift.py`, `app/monitoring/alerts.py`, migration 0006)

- **Token-aware content checks.** `_pattern(needle)` adds a left boundary `(?<![\w.,])` when the
  needle starts alphanumerically and `(?![\d])` when it ends in a digit: "5 minutes" no longer
  matches "15 minutes", "300" no longer matches "3000"; a needle ending in a letter may still be a
  prefix ("pass" matches "passed").
- **Negation-aware.** `_affirms` requires at least one occurrence not preceded (within 40 chars)
  by `not|no|never|n't|cannot|failed to|unable to` plus up to two words. `must_contain` uses
  `_affirms`; `must_not_contain` uses `_mentions` (any occurrence, negated or not: forbidden
  content is forbidden in every form).
- **`attack_exercised`.** New `EvalCase.must_attempt_any`; the evaluator passes only if a
  non-`model_call` ledger event names one of those tools (executed, refused or sent for
  approval). Added to `adv-secret-file` and `adv-path-escape` (`workspace_read`): a model that
  never tries the attack no longer passes the case vacuously.
- **Cross-process run lock.** `EvaluationStore.start_run` takes `pg_advisory_xact_lock(0x6576616C)`
  ("eval") and refuses (409) while any run is `running`. The advisory lock serialises the
  check-then-insert across API, CLI and replicas; the `running` row is the actual mutex; stale
  runs are released by the post-Day-5 `AGENTOPS_EVAL_STALE_AFTER_S` rule.
- **`newly_failing_cases` drift signal.** `_newly_failing_signal` compares per-case `passed`
  between baseline and head, names the regressed case ids, and breaches when their number exceeds
  `max_newly_failing_cases` (0 in `datasets/drift_thresholds.json`). It is `enough=True` even
  with few samples: one named case is concrete and checkable, while the pass rate hides it (1 of 38
  is 2.6 points, under the 5-point threshold).
- **Alert acknowledgement.** Migration 0006 adds `acknowledged_by`, `acknowledged_at`,
  `ack_note`. `MonitoringService.acknowledge` (row lock, 404/409) and `POST
  /monitoring/alerts/{id}/acknowledge` (admin). The alert stays on record; it leaves the open list.
- Telemetry traces now carry `ledger_events` and `observations_truncated`.

### 1.6 Smaller reliability fixes in `ae7c3c3`
- **Stopped runs keep their sources.** `ctx.evidence` holds the running loop/graph's latest
  `EvidenceStore`; `build_result` for `STOPPED_STATUSES` (`budget_exhausted`,
  `deadline_exceeded`, `cancelled`, `failed`) lists every gathered passage as `citations`.
- **Trimmed history keeps citable evidence.** `bound_history(..., recap=store.recap)` reserves a
  quarter of the budget (`RECAP_SHARE`) for one fenced `<recap id="S2" …>` line per label that
  appeared in dropped turns, so an `[S2]` shown five turns ago stays citable.
- **Trace on failed save.** `_execute` exports the trace in a `finally` around the save.

---

## 2. Eval-driven answer quality fixes (`ed27635`)

### The method
Each of the four failing golden cases was handled the same way:
1. **Reproduce** the single case live (`reports/check-two.json` is one such targeted two-case run:
   `multi-retention` still ending `insufficient_evidence`).
2. **Trace the raw model I/O**: the ledger and the exact prompts and outputs of every node, not
   the evaluator's verdict.
3. **Find the real cause.** Twice, what looked like "the model is not good enough" was retrieval
   or citation mechanics.
4. **Fix in code where code is reliable** (string matching, chunk boundaries, per-file outcomes),
   and in the prompt only where judgement is needed.
5. **Keep the verifier as the safety net**: nothing added by code bypasses `verify_answer`.
Prompt version bumped `agent-graph-v4` → `agent-graph-v5`.

### `multi-retention` (v1 vs v2 retention conflict)
- **Cause 1, retrieval.** The deciding sentence was v2's two-sentence "Open Risks" section, a
  265-char chunk of its own, ranked 6th. Structured chunking starts a new chunk at a heading once
  the current one is half full, so a short final section became a tiny chunk with a weak
  embedding. **Fix:** `_absorb_trailing_runt` in `app/ingestion/chunking.py` merges a final span
  under `size/4` into the previous one when the result still fits in `size` and the spans are
  contiguous within the overlap; section labels are joined with " | ".
- **Cause 2, the verifier's wording.** The draft was right but cited the wrong passage. The
  verifier, which only sees the *cited* sources, phrased its verdict as if about all evidence
  ("the evidence does not mention v2's retention"); the reviser believed it and gave up with
  `insufficient_evidence`. **Fixes:** `VERIFY_PROMPT` now asks to quote each unsupported claim
  word for word and not to describe the sources; `verify_answer`'s finding names the sources
  that were checked and the uncited ones the checker did not see; the revision message says a
  claim is often stated in a different source and to set `insufficient_evidence` only if no
  source states it.
- **Cause 3, the question's word.** `ANSWER_PROMPT` now says that for "conflict / difference /
  change" questions, stating the compared facts side by side with citations is an answer even if
  no source uses the word "conflict".

### `multi-availability` (v1 99.9% vs v2 99.95%)
- **Cause, citation mechanics.** The answer cited v1's overview `[S1]` for "99.9%", which is in
  v1's availability passage `[S4]`. The verifier rejected it three times; at temperature 0 the
  model re-sent the same citation each time, and when told the right label it moved the wrong
  label onto the other figure.
- **Fix 1 (hint):** `locate_figures(claims, store, cited)` extracts figures from the rejected
  claims (`_FIGURE` regex: numbers with optional %, ms, s, minutes, hours, days, years,
  million) and appends `"99.9%" appears in S4` to the feedback, uncited passages only.
- **Fix 2 (code does it):** `complete_citations(draft, store)` runs in `validate_citations`. For
  each figure in the answer that no cited source contains, it finds the passages that do; if
  there are one or two (a figure found everywhere identifies nothing), it adds them to
  `citations` and inserts the marker next to the figure (first occurrence), recording a `policy /
  citation_completion` ledger event. It can only add a source, never a claim, and the completed
  answer is still entailment-checked by `verify_answer`.

### `tool-rto-diff`
- **Cause:** a correct, calculator-backed "45 minutes [T1]" came back with
  `insufficient_evidence: true`. **Fix** in `generate_answer`: if the flag is set, there is no
  feedback yet, and the answer contains a citation marker, return no draft and one finding
  ("contradictory output …") so `retry_or_revise` asks once. A second contradictory draft is taken
  as insufficient; it is never returned unverified.

### `ws-edit-approved`
- **Cause:** an edit failed once ("old text not found"), was retried and applied, and qwen3:8b
  reported "could not be completed" from the first error. **Fix:** `workspace_outcome(store)`
  computes the per-file outcome from `workspace_write`/`workspace_edit` tool results and appends
  "Verified workspace outcome (computed from the tool results)" to the answer prompt for
  `workspace_task` intents: each changed path with its label, plus "Earlier attempts … failed, but
  a later retry succeeded: the change WAS made" or "No change was made".

### Reindex tooling
- **`agentops reindex [--tenant t]`** (`app/cli.py`): for every document, `mark_for_reindex`
  then `process_document` inline, printing status, chunk count and any error; documents already
  being indexed are skipped with a warning. Each keeps serving its previous index until the new
  one commits.
- **Eval corpus guard** (`EvaluationRunner`, `app/evaluation/runner.py`): if an existing corpus
  document's `chunking_config` differs from the server's `chunking_config.key`, it is reindexed
  before the run. Without it, the chunking fix above would have been evaluated against the old
  chunks.
- **`docs/use-cases.md`**: ten use cases (verifiable Q&A, version conflicts, on-call runbook with
  calculator, approved writes, sandboxed tests, injection resistance, tenant isolation, eval
  gates, drift, audit) with UI/CLI/API commands and the guarantee each relies on, plus "What it is
  not (yet)".

---

## 3. Files touched

New: `app/db/migrations/versions/0006_alert_acknowledgement.py`,
`app/db/migrations/versions/0007_durable_indexing.py`, `docker/sandbox.Dockerfile`,
`tests/security/test_docker_sandbox.py`, `docs/use-cases.md`.

Modified (main ones): `app/agents/{service,store,graph,nodes,context,evidence,result,custom_loop}.py`,
`app/core/{config,execution_context}.py`, `app/api/{agent,workspace,monitoring}.py`,
`app/ingestion/{pipeline,chunking}.py`, `app/retrieval/vector_search.py`, `app/db/models.py`,
`app/services.py`, `app/main.py`, `app/cli.py`, `app/workspace/{executor,jail}.py`,
`app/evaluation/{dataset,evaluators,store,runner}.py`, `app/monitoring/{drift,alerts}.py`,
`app/observability/telemetry.py`, `datasets/{drift_thresholds.json,golden_questions.jsonl}`,
`web/src/lib/{api,sse,types}.ts`, `web/src/screens/{Approvals,Chat,Evaluations,Executions,...}.tsx`,
`README.md`, `docs/{demo,day-checklist}.md`, `.env.example`, `scripts/demo.sh`.

New settings: `AGENTOPS_INDEXING_{WORKER_ENABLED,POLL_S,LEASE_S,MAX_ATTEMPTS}`,
`AGENTOPS_SSE_RECONNECT_GRACE_S`, `AGENTOPS_APPROVAL_TTL_S`, `AGENTOPS_APPROVAL_RESUME_GRACE_S`,
`AGENTOPS_WORKSPACE_EXEC_BACKEND`, `AGENTOPS_WORKSPACE_SANDBOX_{IMAGE,CPUS,MEMORY}`,
`AGENTOPS_WORKSPACE_RECEIPT_TTL_S`.

---

## 4. Key design decisions (taken vs rejected)

| Taken | Rejected | Why |
|---|---|---|
| 202 + poll; `?wait=true` kept | Blocking approve only; WebSocket push | A resumed local run takes minutes, longer than proxy timeouts; polling a row is trivial and survives reconnects. The CLI and evaluator still want the blocking form. |
| Claim (DB) then resume (task), release on failure to start | Resume first, mark decided after | The conditional UPDATE is the only thing that stops two approvers resuming twice; releasing on failure keeps "approved but still paused" from being a normal state. |
| Reopen stuck decisions to `pending` | Auto-resume them in the recovering process | A human decided under conditions that may have changed; asking again is safer, and receipts make a repeat write idempotent anyway. |
| Lazy housekeeping on list/decide | A scheduler | No scheduler exists in the app; the sweep is cheap and runs exactly when someone looks. Cost: an unlisted approval expires late. |
| Pause + approval row in one transaction | Two writes | A crash between them left a paused run with nothing to approve. |
| Documents table as the queue (claim UPDATE, lease, attempts) | Redis/Celery/a jobs table | One source of truth, no new service; the claim is one statement; Postgres already holds the state the queue would mirror. |
| Lease + heartbeat | Fixed timeout only | A long embedding run is not mistaken for a dead one; a dead one is reclaimed within one lease. |
| `searchable` separate from `status` | Status only | "Being reindexed" and "has a usable index" are independent facts; conflating them took documents offline during every reindex. |
| Re-promote on delete; restore on re-upload | Leave the file absent from search | Deleting v3 should not make the file disappear when v2 is indexed. |
| Docker per command, opt-in | gVisor/Firecracker; always-on Docker | Strong enough isolation for approved `pytest`, with tools present on a dev machine; opt-in because the compose container would need the Docker socket. |
| Same rate bucket for WebSocket and HTTP exec | Separate limit | Otherwise the WebSocket is a bypass. |
| In-process live registry + DB replay | Redis pub/sub for events | One API process deployed; a finished or remote run still replays from the stored ledger. |
| `aclosing` around the inner generator | Rely on GC | Disconnect semantics (cancel or reap) must happen at disconnect time. |
| Fix citations in code (`complete_citations`) | More prompt wording | At temperature 0 the same prompt produced the same wrong citation three times; exact-figure matching is deterministic, and the verifier still checks the result. |
| Merge trailing runts at chunk time | Retrieval re-ranking; larger top_k | The cause was the chunk boundary; fixing it costs nothing at query time. |
| `workspace_outcome` from tool results | Trust the model's summary | What changed on disk is a fact the code already has. |
| Advisory xact lock + running-row check | In-process `eval_task` check only | The CLI and a second replica could start concurrent runs on the one local model. |

---

## 5. Bugs found and how

| # | Bug | Found by | Fix |
|---|---|---|---|
| 1 | Inner `_follow` not closed on disconnect; run not stopped until GC | `test_client_disconnect_cancels_and_persists` | `contextlib.aclosing` in `stream`/`follow` |
| 2 | Paused status and approval row written separately | crash-window review¹ | `ExecutionStore.save(approval=...)` |
| 3 | Every checkpoint copied the whole ledger | checkpoint-size review¹ | `snapshot(with_ledger=…)`, `_RESUMABLE_FROM` |
| 4 | Reindex took a document out of search; a failed reindex lost it | hardening review¹ (now pinned by `test_failed_reindex_keeps_the_previous_index_serving`) | `searchable`, `_fail(keep_index=True)` |
| 5 | "5 minutes" matched "15 minutes"; "did not pass" matched "pass" | evaluator review¹ | `_pattern`, `_affirms` |
| 6 | Adversarial cases passed when the model never attempted the attack | evaluator review¹ | `attack_exercised` |
| 7 | v2 "Open Risks" chunk ranked 6th | live trace of `multi-retention` | trailing-runt merge |
| 8 | Reviser gave up after a mis-worded verifier verdict | live trace of `multi-retention` | quote-only verifier, scoped feedback |
| 9 | Same wrong citation re-sent at temperature 0 | live trace of `multi-availability` | `locate_figures`, `complete_citations` |
| 10 | "insufficient" flag with a cited, correct answer | live trace of `tool-rto-diff` | one contradiction correction |
| 11 | First failed edit outweighed the successful retry | live trace of `ws-edit-approved` | `workspace_outcome` |
| 12 | Eval corpus kept old chunks after a chunking change | the chunking fix itself | reindex on `chunking_config` mismatch |

¹ Not failures seen in this milestone's live runs: they close findings earlier journals left open
(checkpoint growth: journal 04 §8 item 8, 219 ledger events stored for a 20-event run; loose
substrings and vacuous adversarial passes: journal 08 §8 items 6-7; the crash windows: journals 05
and 11). How rows 2 and 4 were first noticed is not recorded in the commits.

---

## 6. How it was verified

- Approvals (`tests/integration/test_agent_api.py`): `test_approve_returns_202_and_the_run_finishes_in_the_background`,
  `test_unanswered_approvals_expire_and_cancel_their_run`, `test_a_decision_whose_resume_died_is_reopened`,
  `test_a_resume_that_cannot_start_releases_the_claim`, `test_concurrent_approvals_resume_once`
  (now `[202, 409, 409, 409]`).
- SSE: `test_sse_events_carry_ids_and_a_finished_run_can_be_replayed` (ids ascending from 0, last
  `id: end`, replay from `Last-Event-ID: 2` starts at seq 3, other tenant 404),
  `test_a_disconnected_stream_can_reattach_within_the_grace_period` (tenant-scoped, nothing
  replayed twice), `test_client_disconnect_cancels_and_persists`.
- Indexing (`tests/integration/test_ingestion.py`): failed reindex keeps serving, delete
  re-promotes, re-upload restores latest, re-upload of a failed doc requeues, dead worker
  reclaimed, poison document given up, three concurrent `process_document` index once.
- Sandbox: 3 tests in `tests/security/test_docker_sandbox.py` (passed in the Recorder's run).
- Terminal: `tests/integration/test_workspace_api.py` (second command over the WebSocket gets
  `too_many_requests` with a bucket of 1).
- Evaluation/monitoring: `test_content_checks_match_tokens_and_ignore_negations`,
  `test_attack_exercised_requires_an_attempt`, `test_one_newly_failing_case_is_named_even_when_the_pass_rate_holds`,
  alert acknowledge (200, removed from open list, 409 on repeat).
- Quality fixes: `test_a_short_final_section_is_merged_into_the_previous_chunk`,
  `test_locate_figures_points_at_the_passage_that_states_a_rejected_figure`,
  `test_complete_citations_attaches_the_passage_that_states_a_figure`,
  `test_workspace_outcome_states_that_a_retried_edit_was_made`.
- Others: `test_trimmed_turns_leave_a_recap_of_their_citable_sources`,
  `test_a_stopped_run_keeps_the_sources_it_had_gathered`.

---

## 7. Commands

```bash
# Approvals: decide and poll (or block with ?wait=true)
curl -X POST -H "X-API-Key: dev-key-tenant-a" localhost:8000/api/v1/approvals/<id>/approve -i
curl -H "X-API-Key: dev-key-tenant-a" localhost:8000/api/v1/executions/<execution-id>
# Reconnect to a stream
curl -N -H "X-API-Key: dev-key-tenant-a" -H "Last-Event-ID: 4" \
  localhost:8000/api/v1/executions/<execution-id>/stream
# Docker sandbox
docker build -t agentops-sandbox:1 -f docker/sandbox.Dockerfile docker
AGENTOPS_WORKSPACE_EXEC_BACKEND=docker uv run uvicorn --factory app.main:create_app
uv run pytest -q tests/security/test_docker_sandbox.py
# Reindex after a chunking or embedding change
uv run alembic upgrade head            # 0006, 0007
uv run agentops reindex [--tenant tenant_a]
# Acknowledge an alert (admin)
curl -X POST -H "X-API-Key: dev-key-tenant-a" -H "Content-Type: application/json" \
  -d '{"note":"known probe"}' localhost:8000/api/v1/monitoring/alerts/<id>/acknowledge
# Gates
uv run ruff check app tests && uv run ruff format --check app tests && uv run mypy app
uv run pytest -q -W error
```

---

## 8. What this milestone taught

1. **"The model got it wrong" is a hypothesis, not a diagnosis.** In `multi-retention` the
   evidence was ranked 6th; in `multi-availability` the citation mechanics, not the reasoning,
   failed. Only the raw traces showed that.
2. **At temperature 0, repeating a prompt is not a retry.** The same input gave the same wrong
   citation three times. A retry has to change the input (a located figure) or move the job to
   code.
3. **Let code do what code does reliably, and keep the model checking it.** Figure matching,
   per-file outcomes and chunk boundaries are deterministic; entailment is not, so the verifier
   stays in the loop after every code fix.
4. **Feedback must say what was checked.** A verifier that saw two sources but spoke about "the
   evidence" made the reviser abandon a correct answer.
5. **A state column that means two things will bite.** `status` meant both "queue position" and
   "may be served"; splitting out `searchable` removed a class of outages.
6. **Async generator cleanup is not automatic.** Nested async generators need `aclosing`, and
   only a test that asserts on the state right after `aclose()` catches it.
7. **Crash windows are found by asking "what if the process dies here?" at each await.** That
   question produced the one-transaction pause, the claim release, `recover_stuck` and the
   indexing lease.

---

## 9. Known limitations

1. **In-process state.** Rate-limit buckets, caches, the `_LiveRun` registry, background resumes
   and the indexing worker loop live in one process. Replicas each get their own buckets and
   caches; the claim UPDATEs and the advisory lock are the only cross-process coordination.
2. **Live follow only in the starting process.** Reconnecting to another process replays the
   stored outcome once finished; while it runs there it returns `execution_not_live_here`.
3. **A resumed run is not followable live.** Background resumes are not registered in `_live`;
   the UI polls the execution row instead.
4. **API-key auth only.** No login, no roles; the web UI keeps the key in local storage; admin is a
   tenant list.
5. **Docker backend not inside the compose container.** It needs a Docker daemon next to the API;
   enabling it in `docker compose --profile app` would mean mounting the Docker socket, which
   gives the API root-equivalent access to the host. Host backend remains the default.
6. **Lazy approval housekeeping.** Expiry and recovery run only when approvals are listed or
   decided. `approval_resume_grace_s` must exceed the longest resume (deadline max 900 s vs grace
   1200 s); if lowered below that, a still-running resume could be reopened and decided twice
   (receipts make the write idempotent, but the run would execute twice).
7. **`recover_stuck` and `expire` are system-wide**, triggered by any tenant's request (scoped
   updates by status and time, not by tenant). Fine for correctness; noted for audit.
8. **Indexing worker is sequential** per process (one document at a time) and polls every 15 s.
9. **Heuristics.** `_affirms`' negation window (40 chars, up to two words) can misread "not only 5
   minutes but …"; `complete_citations` matches figures by substring and edits only the first
   occurrence; `_absorb_trailing_runt` only merges the final chunk.
10. **Untested paths:** `_run_watched` (busy frames, kill on disconnect), receipt pruning, and the
    worker loop in `start_indexing_worker` have no direct test (only `pending_work` and
    `process_document` do).
11. **Receipt pruning runs on writes only** and its once-an-hour guard is a process-level dict.
12. **CI**: the docker job builds the sandbox image and runs the sandbox security tests; the
    indexing worker loop and receipt pruning got direct tests after this entry was drafted.

---

## 10. What I'd do differently at 100x scale

- A real job system (or `SELECT … FOR UPDATE SKIP LOCKED` batch claims with N workers) for
  indexing, resumes and evals, with metrics on queue depth and age.
- Shared event fan-out (Postgres `LISTEN/NOTIFY` or Redis streams) so any replica can serve a
  live follow; sticky routing until then.
- A scheduler for approval expiry and recovery, and notifications to approvers.
- gVisor or Firecracker for exec, with a sandbox service instead of the Docker socket.
- OIDC login, roles and scoped keys, with an audit log of operator actions.
- A citation-grounding step as a first-class node (figure and entity alignment) with its own
  evaluator, rather than inside `validate_citations`.
