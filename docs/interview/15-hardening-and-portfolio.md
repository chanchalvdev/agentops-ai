# Interview Prep 15 — Hardening and Portfolio: Durable Approvals and Indexing, Docker Sandbox, SSE Reconnect, Eval-Driven Quality Fixes

Every answer is grounded in commits `ae7c3c3` and `ed27635` on branch `hardening-and-portfolio`.
Format: direct answer, then the reasoning, then a code pointer. Companion:
`docs/journal/15-hardening-and-portfolio.md`.

---

## Basic

### Q1. What did this milestone add?
**Answer:** Approvals that return 202 and resume in the background, expire after a TTL, and are
reopened if their resume died; indexing that survives crashes, using the documents table as the
queue; an opt-in Docker sandbox for workspace commands; SSE streams that can be reconnected with
`Last-Event-ID`; stricter evaluators and a per-case drift signal; and four answer-quality fixes
found from live model traces. Plus `agentops reindex` and `docs/use-cases.md`.
Pointer: `app/agents/service.py`, `app/ingestion/pipeline.py`, `app/workspace/executor.py`,
`app/agents/nodes.py`.

### Q2. What does `POST /approvals/{id}/approve` return now?
**Answer:** `202 {"approval_id", "execution_id", "status": "resuming"}` with a `Location` header
pointing at the execution. The client polls that execution until it leaves `awaiting_approval`.
`?wait=true` keeps the old blocking behaviour and returns the full `AgentRunResult`.
Pointer: `app/api/agent.py:_decide`; `AgentService.decide_in_background`; `web/src/lib/api.ts:api.decide`.

### Q3. What happens to an approval nobody decides?
**Answer:** After `approval_ttl_s` (24 h default) `ApprovalStore.expire` sets it `expired` and its
execution `cancelled` with `error_code="approval_expired"` in one transaction; the run's
checkpoints are then deleted. A later approve gets 409.
Pointer: `app/agents/store.py:ApprovalStore.expire`; `AgentService.maintain`.

### Q4. What is the `searchable` column?
**Answer:** "This document has a complete index that retrieval may serve." It is separate from
`status`, so a document being reindexed (`PENDING`/`PROCESSING`) keeps serving its previous
chunks. Vector and keyword search filter on `searchable AND is_latest`.
Pointer: `app/db/models.py:Document.searchable`; `app/retrieval/vector_search.py:scoped_select`;
migration `0007_durable_indexing.py`.

### Q5. What does the Docker sandbox restrict?
**Answer:** Per command, a fresh container with `--network none`, `--cap-drop ALL`,
`no-new-privileges`, a read-only root with a 64 MB `/tmp` tmpfs, CPU/memory/PID limits, uid
10001, only the tenant's directory mounted at `/workspace`, and only a scrubbed environment. The
allowlist and path jail still run first.
Pointer: `app/workspace/executor.py:DockerSandbox.argv`; `docker/sandbox.Dockerfile`.

### Q6. How does an SSE client reconnect?
**Answer:** Every ledger event is sent with `id: <seq>`, closing events with `id: end`. The client
calls `GET /api/v1/executions/{id}/stream` with `Last-Event-ID`; the server replays what came after
it and continues live (same process) or replays the stored outcome.
Pointer: `app/api/agent.py:_sse_body`, `follow_execution`; `AgentService.follow`.

### Q7. What is `attack_exercised`?
**Answer:** An evaluator for adversarial cases with `must_attempt_any`: the case passes only if one
of those tools was actually attempted (executed, refused, or sent for approval). Without it, a model
that never tried to read `.env` "defended" the attack by doing nothing.
Pointer: `app/evaluation/evaluators.py:attack_exercised`; `datasets/golden_questions.jsonl`
(`adv-secret-file`, `adv-path-escape`).

### Q8. What does `newly_failing_cases` measure?
**Answer:** The ids of cases that passed in the baseline and fail now. It breaches above
`max_newly_failing_cases` (0). A single regression of 38 cases is 2.6 points, under the 5-point
pass-rate threshold, so the rate alone hides it.
Pointer: `app/monitoring/drift.py:_newly_failing_signal`; `datasets/drift_thresholds.json`.

### Q9. What does `agentops reindex` do?
**Answer:** For each document (optionally one tenant): `mark_for_reindex`, then
`process_document` inline, printing status, chunk count and error. Documents already being indexed
are skipped. Each keeps serving its old index until the new one commits.
Pointer: `app/cli.py:reindex`.

---

## Logical

### Q10. Why split approve into `claim` and resume?
**Answer:** The claim is a conditional `UPDATE … WHERE status='pending' RETURNING`, the only thing
that stops two approvers resuming twice. Restoring the checkpoint and checking the execution can
still fail after that; if they do, `release` puts the approval back to `pending` (under
`asyncio.shield`, so a cancelled request still releases). Only then does the resume start, in the
request (`decide`) or as a task (`decide_in_background`).
Pointer: `AgentService.claim`; `ApprovalStore.decide`, `release`;
`test_a_resume_that_cannot_start_releases_the_claim`.

### Q11. Why write the paused status and the approval in one transaction?
**Answer:** Previously `save` wrote `awaiting_approval`, then `approvals.create` wrote the row. A
crash between them left a paused run with nothing to approve, which could only expire. Now
`ExecutionStore.save(ctx, result, approval=pending)` does both, and the insert is an upsert on
`uq_approvals_execution_call` so re-saving the same pause does not duplicate it.
Pointer: `app/agents/store.py:ExecutionStore.save`, `_approval_insert`; `AgentService._execute`.

### Q12. How does `recover_stuck` decide a resume died, and why is reopening safe?
**Answer:** A decided approval whose execution is still `awaiting_approval` more than
`approval_resume_grace_s` (1200 s) after `decided_at`. A live resume saves a non-paused status
long before that (deadline max 900 s). It goes back to `pending` for a human; if approved again,
the write tool's receipt for (execution, call) makes the write idempotent.
Pointer: `ApprovalStore.recover_stuck`; `app/core/config.py` comment;
`test_a_decision_whose_resume_died_is_reopened`.

### Q13. Why keep the ledger only in some checkpoints?
**Answer:** A paused run is restored from the checkpoint written by the node that runs just before
`human_approval` interrupts, or by `human_approval` itself. Only those need the full ledger; the
rest record `ledger_len`. Journal 04 measured 219 stored ledger events for a 20-event run.
Pointer: `app/agents/nodes.py:_RESUMABLE_FROM`, `node()`; `ExecutionContext.snapshot(with_ledger=)`.

### Q14. Why does the indexing claim check the lease, and what does the heartbeat do?
**Answer:** `PROCESSING` alone cannot tell "being worked on" from "its worker died". `_claim`
takes a document that is `PENDING` or `PROCESSING` with `lease_until < now`. `_heartbeat` pushes
the lease out every `lease/3` while `_index` runs, so a slow embedding run is never stolen, and a
dead one is reclaimable within one lease.
Pointer: `IngestionService._claim`, `_heartbeat`; `test_a_document_whose_worker_died_is_reclaimed`.

### Q15. What stops a document that crashes the process from looping forever?
**Answer:** `attempts` is incremented by every claim and reset on success. If a claim makes it
exceed `indexing_max_attempts` (3), `process_document` fails the document with "Gave up after N
attempts". If it had a previous index, `_fail(keep_index=True)` keeps serving that.
Pointer: `IngestionService.process_document`, `_fail`;
`test_a_document_that_keeps_killing_its_worker_is_given_up`.

### Q16. What breaks if you remove `contextlib.aclosing` in `AgentService.stream`?
**Answer:** When Starlette closes the outer generator on disconnect, the inner `_follow` generator
stays suspended. Its `finally`, which removes the follower's waker and stops or reaps the run, runs
only at garbage collection. The run keeps going and the row stays `running`.
`test_client_disconnect_cancels_and_persists` caught exactly this when `_follow` was introduced.
Pointer: `AgentService.stream`, `follow`, `_follow`.

### Q17. Why does `_follow` clear the event before draining the ledger?
**Answer:** If it cleared after draining, an event appended between the last read and the clear
would set the flag, be cleared, and the follower would sleep until the heartbeat with an unsent
event. Clearing first means any append during the drain leaves the flag set.
Pointer: `AgentService._follow` (`wake.clear()` comment).

### Q18. Why does `must_not_contain` ignore negation when `must_contain` respects it?
**Answer:** `must_contain` asks "does the answer affirm X?", and "did not pass" does not affirm
"pass" (`_affirms`). `must_not_contain` guards leaks: "the secret is not do-not-leak-7f3a" still
leaks it, so any occurrence counts (`_mentions`).
Pointer: `app/evaluation/evaluators.py:_affirms`, `_mentions`, `answer_content`.

---

## Advanced

### Q19. Walk through the `multi-retention` diagnosis.
**Answer:** The case asks for the retention conflict between v1 (7 years) and v2 (5 years). The run
ended `insufficient_evidence` (`reports/check-two.json`). The trace showed two causes. First,
retrieval: v2's "Open Risks" section, which states the conflict, was a 265-char chunk ranked 6th,
because structured chunking starts a new chunk at a heading. Fixed by `_absorb_trailing_runt`
(merge a final chunk under `size/4` into the previous one if it fits). Second, the verifier saw
only the cited sources but said "the evidence does not mention v2's retention"; the reviser
believed it and gave up. Fixed by a quote-only `VERIFY_PROMPT`, feedback naming checked and
unchecked sources, and a revision message that says "find the source that states each claim".
Pointer: `app/ingestion/chunking.py:_absorb_trailing_runt`; `app/agents/nodes.py:verify_answer`,
`generate_answer`.

### Q20. And `multi-availability`?
**Answer:** The answer cited v1's overview `[S1]` for "99.9%", which is in v1's availability
passage. The verifier rejected it three times; at temperature 0 the model re-sent the same
citation, and when told the right label it put the wrong label on the other figure. Two fixes:
`locate_figures` adds `"99.9%" appears in S4` to the feedback; `complete_citations` (in
`validate_citations`) attaches the passage containing each uncited figure if one or two passages
contain it, inserting the marker next to the figure and logging `citation_completion`. The
verifier still checks the completed answer.
Pointer: `app/agents/nodes.py:locate_figures`, `complete_citations`, `validate_citations`.

### Q21. Can `complete_citations` make a wrong answer look supported?
**Answer:** It cannot add a claim, only a source for a figure the answer already states, and only
when the figure occurs in at most two passages. The result then goes through `validate_final` and
`verify_answer`, which checks entailment against the sources now cited. Worst case: a figure that
appears in an unrelated passage gets that passage attached; the verifier then rejects the claim,
and the revision loop or `failed_validation` follows.
Pointer: `complete_citations` docstring; `verify_answer`; `retry_or_revise`.

### Q22. Why is `workspace_outcome` injected into the prompt rather than replacing the answer?
**Answer:** The answer still has to describe the change in the user's terms and cite the tool
results. The block states facts computed from `workspace_write`/`workspace_edit` results (per-file
"changed (applied) [T3]", or "every write attempt failed"), so the model cannot report the first
failed attempt as the outcome. The answer is still citation-validated and verified.
Pointer: `app/agents/nodes.py:workspace_outcome`, `generate_answer`;
`test_workspace_outcome_states_that_a_retried_edit_was_made`.

### Q23. What failure modes remain in the SSE design?
**Answer:** The `_LiveRun` registry is per process: a reconnect routed to another replica gets the
stored replay, or `execution_not_live_here` while the run is going. If the client does not return
within `sse_reconnect_grace_s` (30 s) the run is cancelled. Background resumes are not in the
registry, so a resumed run is polled, not streamed. A process restart loses every live run (each is
saved `cancelled` by `aclose`, or stays `running` if the process is killed).
Pointer: `AgentService._live`, `stop_unless_followed`, `aclose`; `app/api/agent.py:_stored_items`.

### Q24. How is "one evaluation run at a time" enforced across processes?
**Answer:** `EvaluationStore.start_run` takes `pg_advisory_xact_lock(0x6576616C)` and then checks
for a `running` row before inserting. The lock lasts only for that transaction; it serialises the
check-then-insert, so two starters cannot both see "none running". The `running` row is the real
mutex, and runs abandoned by a dead process are failed after `AGENTOPS_EVAL_STALE_AFTER_S`.
Pointer: `app/evaluation/store.py:start_run`.

### Q25. Why does the Docker backend call `docker rm -f` on timeout?
**Answer:** `run_streaming` kills the process group of the `docker run` CLI, but the container is
owned by the daemon and keeps running. `DockerSandbox.remove(name)` removes it by its generated
name; `test_a_timed_out_command_leaves_no_container_behind` checks no `agentops-exec-*` is left.
Pointer: `app/workspace/executor.py:run_streaming`, `DockerSandbox.remove`.

---

## Scenario

### Q26. "I approved a write, the server restarted, and the run is still awaiting approval."
**Answer:** The claim committed, the resume task died with the process. On the next approvals list
or decision, `maintain` runs `recover_stuck`: once 1200 s have passed since the decision, the
approval returns to `pending` with "the resume after this decision did not complete; decide
again". Approve again; the resume restores the checkpoint (Postgres checkpointer) and the write's
receipt makes it idempotent. If nobody looks for 24 h it expires instead.
Pointer: `AgentService.maintain`; `ApprovalStore.recover_stuck`, `expire`.

### Q27. "The API crashed while indexing a 200-page PDF."
**Answer:** The document is `PROCESSING` with a lease up to 600 s in the future. After the lease
passes, the next server's worker (`start_indexing_worker`, every 15 s) finds it via
`pending_work`, `_claim` takes it (attempt 2), and indexing starts again. If it is the PDF that
kills the process, the fourth claim gives up with "Gave up after 3 attempts". If it was a reindex,
the old chunks served throughout.
Pointer: `IngestionService.pending_work`, `_claim`, `process_document`; `app/services.py`.

### Q28. "We changed the chunk size and answers got worse, but the eval says no regression."
**Answer:** Before this milestone the eval corpus kept the chunks it was first indexed with, so
the eval measured the old configuration. Now `EvaluationRunner` reindexes any corpus document
whose `chunking_config` differs from the server's. For the live corpus run `agentops reindex`. Then
compare runs: `newly_failing_cases` names any case that regressed even if the rate held.
Pointer: `app/evaluation/runner.py`; `app/cli.py:reindex`; `app/monitoring/drift.py`.

### Q29. "Someone runs `python -m pytest` on a malicious test file in their workspace."
**Answer:** It needs approval first (exec of workspace code). With the `host` backend it then runs
with the server's privileges: approval is the only control. With
`AGENTOPS_WORKSPACE_EXEC_BACKEND=docker` it runs as uid 10001 in a container with no network, no
capabilities, read-only root, CPU/memory/PID limits and only the tenant's directory; it can damage
that directory, nothing else. The security test runs exactly such a file and checks those claims.
Pointer: `tests/security/test_docker_sandbox.py`; `DockerSandbox`.

### Q30. "My phone dropped Wi-Fi for 10 s in the middle of an answer."
**Answer:** The server sees the disconnect; `_follow`'s `finally` schedules
`stop_unless_followed(30 s)`. `streamAgent` notices the stream ended without `final`, waits
(1 s, then 2 s, …), and calls `GET /executions/{id}/stream` with the last `id:`. `follow` replays
the missed events and continues live; the UI shows no gap and no duplicate.
Pointer: `web/src/lib/api.ts:streamAgent`; `AgentService.follow`;
`test_a_disconnected_stream_can_reattach_within_the_grace_period`.

---

## Trade-off

### Q31. Documents table as the queue vs a dedicated job system?
**Answer:** The table already holds the state a queue would duplicate; one `UPDATE … RETURNING`
is an atomic claim, and leases plus attempts give at-least-once processing with a poison cap. No new
service to run. A job system wins with many job types, high throughput, scheduling, or many
workers (it would also want `FOR UPDATE SKIP LOCKED` batch claims instead of per-id claims).
Pointer: `IngestionService._claim`, `pending_work`.

### Q32. 202 + polling vs keeping the request open vs push?
**Answer:** Blocking ties a request to a multi-minute local run and fails behind proxy timeouts.
Push (SSE/WebSocket) for resumes would need the live registry to cover resumes too. Polling a row
is simple and survives page reloads; the cost is latency of up to 4 s and extra GETs. `?wait=true`
remains for scripts.
Pointer: `app/api/agent.py:_decide`; `web/src/lib/api.ts:api.decide`.

### Q33. Fix in code vs fix in the prompt?
**Answer:** Code for what is deterministic and checkable: chunk boundaries, figure-to-passage
matching, per-file outcomes. Prompt for judgement: treating compared facts as an answer to a
"conflict" question, quoting claims verbatim. At temperature 0 a prompt that failed once fails the
same way again; code changes the input. The prompt route wins when the rule cannot be stated
precisely (entailment itself).
Pointer: `app/agents/nodes.py` (`complete_citations` vs `ANSWER_PROMPT`).

### Q34. Docker per command vs gVisor/Firecracker vs host?
**Answer:** Host is fastest and has no dependencies, but approved code has server privileges.
Docker per command adds container start-up to every command (not measured here), needs a daemon,
and shares the host kernel.
gVisor or Firecracker give a stronger boundary but are not available on a macOS dev machine. Docker
is opt-in; the compose container does not enable it because that would need the Docker socket.
Pointer: `app/workspace/executor.py` module docstring; README "Known limitations".

---

## Justification

### Q35. "Reopening a decided approval is dangerous: the user already said yes."
**Answer:** The yes was never acted on: the run is still paused. Reopening shows the same
request with a reason, instead of an approval marked "approved" that did nothing, which is the
misleading state. Auto-resuming could act on a decision made under conditions that changed while
the system was down. Writes are idempotent per (execution, call), so a second approval cannot
double-apply.
Pointer: `ApprovalStore.recover_stuck` docstring.

### Q36. "Your verifier is the same model family as the generator. Why trust it?"
**Answer:** It is a check on one narrow question, "is each claim stated in these cited passages",
and the 2026-10-25 probe recorded in the `verify_answer` comment showed qwen3:8b rejecting a wrong
figure (15 min) while accepting the verbatim and derived (300 s) answers; qwen3:1.7b had no signal. This milestone made it narrower: quote the unsupported claims, do not describe the sources.
Code-side fixes (`complete_citations`, `workspace_outcome`) do not bypass it; they all feed into
it. It is a safety net, not a guarantee, and the evaluation set is what measures it.
Pointer: `app/agents/nodes.py:VERIFY_PROMPT`, `verify_answer` comment.

### Q37. "Merging chunks to fix one golden case is overfitting."
**Answer:** The rule is general: a trailing chunk under a quarter of the chunk size has a weak
embedding regardless of corpus. It only applies to the last chunk, only when the merge fits in
`size`, and a long final section is untouched (`test_a_short_final_section_is_merged_into_the_previous_chunk`
checks both). The eval corpus is reindexed automatically so the change is measured, not assumed.
Pointer: `_absorb_trailing_runt`; `EvaluationRunner` reindex guard.

---

## System design

### Q38. How do approvals, checkpoints, receipts and the execution row fit together?
**Answer:** The execution row is the public state; the checkpoint is the private resume state; the
approval row is the human handshake; the receipt makes the tool side effect idempotent. Pause:
row + approval in one transaction, checkpoint already written by LangGraph. Decide: conditional
claim. Resume: restore checkpoint, run, save row. End: forget the checkpoint. Failure in any gap is
covered: release (before resume), `recover_stuck` (resume died), `expire` (nobody decided),
receipts (write repeated).
Pointer: `app/agents/{service,store,graph}.py`; `app/workspace/jail.py`.

### Q39. How would this evolve to several API replicas?
**Answer:** Already safe across processes: approval claims, indexing claims and leases, the eval
advisory lock, the stored-ledger replay. To change: rate limits and caches to Redis; live follow
via Postgres `LISTEN/NOTIFY` or Redis streams (or sticky routing); a scheduler for approval
housekeeping instead of lazy sweeps; batch indexing claims with `SKIP LOCKED` and several workers;
a sandbox service instead of a Docker socket; OIDC and roles instead of API keys.
Pointer: journal 15 §9-§10.

### Q40. Summarise this milestone in one minute.
**Answer:** I went through every "what if the process dies here" in the system. Approvals are
claimed atomically, resumed in the background with 202, expired after 24 h, and reopened if their
resume died; pause and approval are one transaction; checkpoints are cleaned up and smaller.
Indexing uses the documents table as a leased queue with a worker, and `searchable` keeps documents
online during reindex. Workspace code can run in a locked-down container. Streams reconnect with
`Last-Event-ID`, and a test caught a generator-cleanup bug. The evaluators stopped passing on
substrings and on attacks never attempted. Then I took the four failing golden cases to the raw
traces: two "model problems" were a mis-ranked chunk and citation mechanics, fixed in code with the
verifier still checking. Final live evaluation: 37/38 (97%) on the optimized profile with the stricter checks (was 36/38): multi_document 5/5, workspace 5/5, adversarial 7/7, p50 27.4 s, p95 58.8 s. One case still fails: `tool-rto-diff` (the calculator returns 45, but the verifier rejects the derived figure), which passed in an isolated probe and is the next fix. Report: `datasets/baselines/golden-v1-graph-qwen3-8b-hardening.json`.
Pointer: journal 15.
