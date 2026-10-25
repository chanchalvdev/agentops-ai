# Interview Prep 03 — Custom Agent Loop + Execution Ledger (Day 2, M3)

Every answer is grounded in the code from commit `3ef6226`. Format: direct answer, then the
reasoning, then a code pointer. Companion: `docs/journal/03-agent-loop.md`.

---

## Basic

### Q1. What is the custom agent loop, in one sentence?
**Answer:** A hand-written, bounded loop that asks the model for either a final answer or tool
calls, executes allowed tools through the harness, feeds fenced results back, and stops on a
validated answer or a policy limit.
It exists before LangGraph so the mechanics (state, limits, corrections) are understood without a
framework, and so the graph can later be compared against it on the same harness.
Pointer: `app/agents/custom_loop.py:AgentLoop.run`, `_run`; product.txt §6.

### Q2. What are the two response outcomes and how is a response classified?
**Answer:** FINAL and TOOL_CALLS. If any tool call is `final_answer`, or there are no tool calls
at all, it is FINAL (`_finish`); otherwise TOOL_CALLS (`_execute` per call).
A no-tool-call response is only accepted if its content parses as `FinalAnswer` JSON; prose is
rejected with a correction, never executed.
Pointer: `app/agents/custom_loop.py:_run` (`final_calls or not response.tool_calls`).

### Q3. What is `final_answer` and why is it a tool?
**Answer:** A pseudo-tool whose JSON schema is `FinalAnswer` (`answer`, `citations`,
`insufficient_evidence`); the loop handles it itself, it is not in the `ToolRegistry`.
Making "finish" a tool means the provider's tool-calling API delivers the answer as structured,
schema-shaped arguments, instead of the loop guessing whether free text is an answer.
Pointer: `app/agents/custom_loop.py:_final_spec`; `app/agents/evidence.py:FinalAnswer`.

### Q4. What does `EvidenceStore` do?
**Answer:** Turns raw tool outputs into what the model sees and what the user gets: run-wide
labels (S# for passages, T# for other tool results), duplicate removal, fenced rendering, and
resolution of cited labels to `Citation`s / `ToolReference`s.
Pointer: `app/agents/evidence.py:EvidenceStore` (`add_passages`, `render_passages`,
`add_tool_result`, `render_tool_result`, `citations`, `references`).

### Q5. What does `validate_final` check?
**Answer:** That every cited label (citations list ∪ inline `[S#]`/`[T#]`) was shown in this run,
that an `answered` reply cites at least one label, and that the answer is not empty.
It returns a list of problems; empty means well-formed. It does not check that the cited text
supports the claim.
Pointer: `app/agents/evidence.py:validate_final`, `cited_labels`.

### Q6. What does `bound_history` do?
**Answer:** Keeps the system prompt and the question, then keeps the newest whole turns that fit
`max_chars`, dropping older turns and inserting "[N earlier turn(s) omitted…]".
A turn is an assistant message plus the `tool` messages that follow it, so a tool call is never
separated from its result.
Pointer: `app/agents/context.py:bound_history`, `_groups`;
`tests/unit/test_agent_loop.py::test_bound_history_keeps_head_and_tool_groups_intact`.

### Q7. What statuses can a run end with?
**Answer:** `answered`, `insufficient_evidence`, `failed_validation`, `awaiting_approval`,
`budget_exhausted`, `deadline_exceeded`, `cancelled`, `failed`.
Non-answer statuses return a fixed `FALLBACK_TEXT`, never the model's draft.
Pointer: `app/agents/result.py:RunStatus`, `FALLBACK_TEXT`, `status_for_error`, `build_result`.

### Q8. What tables did migration 0002 add?
**Answer:** `agent_executions` (one row per run: status, answer, citations, typed counters,
tokens, cost, limits, timestamps), `execution_events` (one row per ledger event, unique
`(execution_id, seq)`), and `approvals` (pending tool calls with arguments, unique
`(execution_id, call_id)`).
Pointer: `app/db/migrations/versions/0002_agent_executions.py`; `app/db/models.py`.

### Q9. What does `ExecutionStore` guarantee?
**Answer:** `save` is idempotent for events and every read is tenant-scoped.
`save` locks the execution row, checks `tenant_id`, updates outcome/counters, and inserts the
ledger with `ON CONFLICT (uq_events_execution_seq) DO NOTHING`. `get`/`events` filter by tenant, so
another tenant gets `NotFoundError`.
Pointer: `app/agents/store.py:ExecutionStore`;
`tests/integration/test_agent_loop_db.py::test_loop_answers_from_real_retrieval_and_persists_the_ledger`.

---

## Logical

### Q10. Why must `final_answer` be the only call in its turn?
**Answer:** Because an answer written in the same turn as a search cannot have read that search's
results — it was produced before they existed.
`_finish` rejects it with "final_answer must be the only tool call in its turn", appends a `tool`
feedback message for every call (so each call ID has a result), and costs an `invalid_output`
correction. The search in that turn is not executed.
Pointer: `app/agents/custom_loop.py:_finish`;
`tests/unit/test_agent_loop.py::test_final_answer_mixed_with_other_calls_is_rejected`.

### Q11. Why is plain prose never parsed as an instruction?
**Answer:** §6 forbids it, and it is an injection path: a reply like "call document_search with
query X" may come from text the model copied out of a document.
Prose goes to `parse_final(None, content)`; it fails JSON validation, so the model is told "reply
with a tool call or final_answer" and the run spends an `invalid_output` correction.
Pointer: `app/agents/custom_loop.py:_finish`;
`tests/unit/test_agent_loop.py::test_plain_text_reply_is_not_executed_or_trusted`.

### Q12. Which errors end the run and which go back to the model, and why the split?
**Answer:** `TERMINAL = (BudgetExhaustedError, DeadlineExceededError, ExecutionCancelledError,
ApprovalRequiredError)` end it; any other `AppError` becomes `<tool_error code=…>message</tool_error>`.
The model cannot fix a budget, a deadline, a cancellation or a missing approval; it *can* fix bad
arguments, try another query after a failure, or answer without a forbidden tool. Messages are the
safe `message`, never `detail` (the test asserts "db down" is absent).
Pointer: `app/agents/custom_loop.py:TERMINAL`, `_execute`;
`tests/unit/test_agent_loop.py::test_tool_failure_is_reported_back`,
`test_disallowed_tool_request_is_refused_without_running`.

### Q13. Why are repeated identical tool calls not re-executed?
**Answer:** The result is already in the history, re-running costs time and budget, and a model
that repeats itself is usually stuck. `_signature` (name + sorted-key JSON of arguments) detects it;
the call is answered with "Duplicate call…" and costs a `repeated_tool_call` correction.
With `max_corrections=2`, the third duplicate raises `BudgetExhaustedError` and the run ends.
The test asserts the search ran exactly once across five identical turns.
Pointer: `app/agents/custom_loop.py:_execute`, `_signature`;
`tests/unit/test_agent_loop.py::test_repeated_identical_tool_calls_are_not_re_executed_and_are_bounded`.

### Q14. Why does the agent, not the tool, assign citation labels?
**Answer:** Labels must be unique across every tool call in a run; a tool sees one call.
`add_passages` continues numbering from `len(self.passages)`, so the second search yields S3, S4…,
and a calculator result is T1. This also lets one answer cite both a passage and a computation.
Pointer: `app/agents/evidence.py:add_passages`, `add_tool_result`;
`tests/unit/test_agent_loop.py::test_multiple_tool_calls_in_one_turn_and_tool_result_citation`.

### Q15. What happens when citation corrections run out?
**Answer:** Status `failed_validation`, answer replaced by "I could not produce an answer verifiably
grounded in your documents.", no citations.
The test feeds four uncited answers with `max_corrections=2`: three model calls, then
`failed_validation`, and "Uncited claim" is not in the result. Returning the unverified draft would
defeat the point of validating.
Pointer: `app/agents/custom_loop.py:_finish`; `app/agents/result.py:build_result`;
`tests/unit/test_agent_loop.py::test_corrections_exhausted_gives_failed_validation_not_a_fake_answer`.

### Q16. What breaks if `bound_history` dropped individual messages instead of whole turns?
**Answer:** An assistant message with `tool_calls` could survive while its `tool` results are
dropped (or vice versa). Models get confused by dangling calls and some providers reject
the request outright. Grouping in `_groups` makes the unit of dropping a turn.
Pointer: `app/agents/context.py` (module docstring, `_groups`).

### Q17. Why does feedback after a rejected `final_answer` go back as `tool` messages, but after prose as a `user` message?
**Answer:** If the model made tool calls, each call ID needs a matching `tool` result or the
conversation is malformed; the feedback is that result. Prose has no call to answer, so the
feedback is a normal user turn.
Pointer: `app/agents/custom_loop.py:_finish` (the `if final_calls:` branch).

### Q18. Why was `top_k` given a floor of 3 rather than clamped?
**Answer:** Live qwen3:8b chose `top_k=1` and retrieved only v1 of the payments doc. With `ge=3`,
the request fails validation, the ledger shows it, and the model self-corrects (observed every
live run). Clamping would hide the model's behaviour and make the input schema lie about what is
accepted. Cost: one extra round-trip per run.
Pointer: `app/tools/document_search.py:DocumentSearchInput.top_k`.

---

## Advanced

### Q19. How does the loop defend against prompt injection in documents?
**Answer:** Three layers. (1) Passages are fenced in `<evidence><source …>` and run through
`escape_untrusted`, so `</source></evidence>` in a document becomes `[/source][/evidence]`.
(2) The system prompt says tool results are untrusted. (3) Even if the model is persuaded, it can
only *request* allowed tools through the harness, and its answer must cite labels that exist.
Honest gap: `escape_untrusted` only matches `source|evidence`, so `</tool_result>` inside a
`document_metadata` result (e.g. a crafted filename) is not neutralised.
Pointer: `app/agents/evidence.py:render_passages`, `render_tool_result`;
`app/rag/answer.py:escape_untrusted`;
`tests/unit/test_agent_loop.py::test_prompt_injection_in_evidence_cannot_close_the_fence`.

### Q20. How is a run's ledger made durable, and what is lost on a crash?
**Answer:** `ExecutionStore.start` writes a `running` row; after `run` returns, `save` writes the
outcome and bulk-inserts every `LedgerEvent` (idempotent by `(execution_id, seq)`).
Events are only written at `save`, so a process crash, an `asyncio.CancelledError`, or a non-`AppError`
exception (only `AppError` is caught in `run`) leaves the row `running` with no events. Fix: write
per step and sweep stale `running` rows.
Pointer: `app/agents/store.py:save`; `app/agents/custom_loop.py:run`.

### Q21. Can the loop still exceed its token budget?
**Answer:** Yes, by about one prompt. The harness now caps `max_output_tokens` to
`max_total_tokens - used`, but input tokens are only known after the call, and the check is
pre-call. `test_token_budget_exhaustion` (budget 250, 120 per call) ends after 3 calls = 360 tokens.
Side effect: late in a run the output cap can shrink to a few tokens and truncate `final_answer`.
Pointer: `app/agents/harness.py:call_model`, `_ensure_token_and_cost_budget`.

### Q22. Why set Ollama's `num_ctx` explicitly?
**Answer:** Ollama silently truncates prompts longer than its context window instead of erroring,
which would drop the system prompt or earlier evidence without anyone noticing. `llm_num_ctx=8192`
fits qwen3:8b in 16 GB of unified memory; `agent_max_context_chars=20_000` (~5k tokens) leaves room
for tool schemas and 1024 output tokens. The char budget is a proxy and tool schemas are not
counted, so it is a margin, not a guarantee.
Pointer: `app/llm/provider.py:OllamaProvider` (`num_ctx`); `app/core/config.py`.

### Q23. What is the weakness of deduplicating passages by normalised text?
**Answer:** Two versions of a document often share paragraphs. If v1's chunk is returned first, the
identical v2 chunk is folded into v1's label, so the answer can only cite v1 — wrong for "latest
version" questions. Dedupe should key on text *within the same document lineage and version*, or
prefer the latest version when collapsing.
Pointer: `app/agents/evidence.py:add_passages` (`_label_by_text`).

### Q24. What does `validate_final` not catch?
**Answer:** It checks *which* labels are cited, not *what* is claimed. An answer with one valid
citation and five uncited claims passes; an answer citing only `[T1]` (calculator) passes with no
document source; an `insufficient_evidence=true` answer is returned with no citation check at all.
Claim-level support is the job of the graph's `verify_answer` node.
Pointer: `app/agents/evidence.py:validate_final`.

### Q25. How are tool-call IDs made safe for approvals now?
**Answer:** Ollama sends no IDs; previously the provider used `call_{i}`, which repeats every turn,
so an approval for one `call_0` approved a later, different `call_0`. Now IDs are
`call_<uuid4 hex[:12]>`, and the `approvals` table is unique on `(execution_id, call_id)`. The
pending ledger event also records the validated arguments the reviewer needs.
Pointer: `app/llm/provider.py:OllamaProvider.chat`; `app/agents/harness.py:call_tool`;
`tests/unit/test_llm.py::test_ollama_tool_call_ids_are_unique_and_null_arguments_mean_empty`.

### Q26. Why make `mark_for_reindex` a single `UPDATE … RETURNING`?
**Answer:** The old read-check-then-update let two concurrent requests both see `ready` and both
claim the document. The conditional `UPDATE … WHERE status NOT IN (pending, processing)
RETURNING id` lets the database decide atomically; only one gets a row back, the other gets
`ConflictError`. The commit is honest that the new test guards the behaviour but does not reproduce
the old race.
Pointer: `app/ingestion/pipeline.py:mark_for_reindex`;
`tests/integration/test_ingestion.py::test_concurrent_reindex_requests_claim_the_document_once`.

---

## Scenario

### Q27. In the live run, the first `document_search` uses `top_k=1`. Walk through what happens.
**Answer:** `harness.call_tool` → tool budget OK → `authorize_tool_call` validates
`DocumentSearchInput` → `top_k: greater_than_equal` → `InvalidToolArgumentsError` (recorded as
`rejected`). `_execute` is not terminal for it, so the model receives
`<tool_error code="invalid_tool_arguments">Invalid arguments for document_search: top_k: …</tool_error>`.
Next turn the model resends with a valid `top_k`; the signature differs, so it is executed.
Pointer: `app/agents/harness.py:authorize_tool_call`, `_argument_problems`;
`tests/unit/test_agent_loop.py::test_invalid_tool_arguments_are_reported_back_and_model_recovers`.

### Q28. The RPO question takes 44-58 s on the laptop. Where does the time go and what would you cut?
**Answer:** ~8.5k tokens over several model turns on qwen3:8b: the rejected `top_k=1` turn, the
real search, usually one prose reply that is rejected (`invalid_output`, ~7-12 s), then
`final_answer`. Cuts: tighten the prompt/few-shot so the first call is valid and the model ends with
`final_answer` directly; disable qwen3 "thinking" for tool turns; route single-hop questions to the
single-shot `Answerer` (1-2 calls). The ledger's per-event `latency_ms` shows which turn to attack.
Pointer: `tests/live/test_agent_live.py::show`; `app/agents/custom_loop.py:SYSTEM_PROMPT`.

### Q29. A user asks "What is the CEO's favourite colour?". What should and does happen?
**Answer:** The model searches, finds nothing relevant, and calls `final_answer` with
`insufficient_evidence=true`; status `insufficient_evidence`, no citations (22-28 s live, 2/2).
`validate_final` allows zero citations only in that case. The earlier pre-floor run is the same
behaviour under bad retrieval: `top_k=1` missed v2 and the agent said "insufficient" instead of
inventing a number.
Pointer: `app/agents/evidence.py:validate_final`; `tests/live/test_agent_live.py`.

### Q30. The model searches, the search times out, and it re-sends the exact same call. What happens?
**Answer:** The first attempt retries once (search is idempotent) and then returns
`<tool_error code="tool_timeout">`. But `_execute` already added the signature to `seen_calls`
before calling the harness, so the identical retry is treated as a duplicate: it is not executed,
the model is told "the result is already above" (which is an error), and a `repeated_tool_call`
correction is spent. This is a bug: only successful calls should be recorded as seen.
Pointer: `app/agents/custom_loop.py:_execute` (`seen.add(signature)` before `call_tool`).

### Q31. An operator sees executions stuck in `running` for hours. Likely causes?
**Answer:** `save` was never called: the process died, the request task was cancelled
(`CancelledError` is not an `AppError`), or an unexpected exception escaped `run`. Also any run
whose caller forgets `save`. The ledger lives in memory until `save`, so those rows have no events.
Remedy: catch `BaseException` in a `finally` to write a `failed`/`cancelled` row, write events
per step, and a sweeper that fails rows older than `deadline_s` + margin.
Pointer: `app/agents/custom_loop.py:run`; `app/agents/store.py`.

### Q32. The model asks for `workspace_exec` with `rm -rf /`. What happens?
**Answer:** The tool is not in the registry or allowlist, so `authorize_tool_call` raises
`ToolPermissionError("Tool not permitted: workspace_exec")` — identical to a disallowed tool, so no
probing. The loop feeds it back as `<tool_error code="tool_not_permitted">` and the model can still
answer; nothing runs.
Pointer: `tests/unit/test_agent_loop.py::test_disallowed_tool_request_is_refused_without_running`.

---

## Trade-off

### Q33. Single-shot `Answerer` vs the agent loop: when is each appropriate?
**Answer:** Use the single-shot `Answerer` for single-hop factual questions where the question is a
good search query: one retrieval, one constrained-JSON generation, ≤ 1 correction, and no model call
at all when retrieval is empty. It is fast, cheap, predictable and the eval baseline. Use the loop
when the question needs several searches, document metadata ("latest version"), arithmetic, or later
write tools with approval — at the cost of latency (44-58 s vs 1-2 calls), more failure modes
(bad arguments, prose replies, loops) and needing the harness to bound it. A production system
routes: default to single-shot, escalate to the loop on classification or failure.
Pointer: `app/rag/answer.py:Answerer.answer`; `app/agents/custom_loop.py:AgentLoop`.

### Q34. Feeding permission errors back to the model vs ending the run?
**Answer:** Chosen: feed back. The harness has already refused, so nothing unsafe happens, and a
model that tried an unavailable tool can usually still answer. Ending the run wins when a
forbidden request is itself a signal of compromise (e.g. an injected document asks for a write tool)
and you want a hard stop plus an alert; that is a reasonable policy once write tools exist.
Pointer: `app/agents/custom_loop.py:TERMINAL`.

### Q35. Sequential vs parallel execution of tool calls in one turn?
**Answer:** Chosen: sequential (`for call in response.tool_calls`). Deterministic ledger order,
simple per-call budget checks, and one local GPU gains nothing from parallel embedding calls.
Parallel (`asyncio.gather`) wins with remote tools and real I/O latency; it needs a reservation on
the tool budget and ordered result insertion.
Pointer: `app/agents/custom_loop.py:_run`.

### Q36. Persisting the ledger as rows vs one JSONB document per execution?
**Answer:** Chosen: rows in `execution_events`. They can be indexed and aggregated ("tool timeouts
per day", "corrections by kind") and appended idempotently by `(execution_id, seq)`. A JSONB blob is
simpler to write atomically and read whole, and wins if the ledger is only ever displayed, never
queried.
Pointer: `app/db/migrations/versions/0002_agent_executions.py`.

---

## Justification

### Q37. "Rejecting a prose reply costs 7-12 s each run. Just accept prose as the answer."
**Answer:** Prose has no structured citations and no `insufficient_evidence` flag; accepting it
means returning text we cannot validate, and §6 forbids treating free text as the control signal.
The cost is bounded (one `invalid_output` correction) and fixable at the prompt/model level; the
guarantee "every returned answer passed citation validation" is not something to trade for 10 s.
Pointer: `app/agents/custom_loop.py:_finish`; `app/agents/result.py:build_result`.

### Q38. "The system prompt already says 'don't repeat calls'. The dedupe code is redundant."
**Answer:** The prompt is a request; small models ignore it (qwen3:8b also ignored "use the
calculator for every calculation" and did 5×60 itself). Code-level dedupe guarantees a stuck model
cannot burn the tool budget and that the run ends after two repeats. Policy that matters lives in
code; the prompt only reduces how often the code has to act.
Pointer: `app/agents/custom_loop.py:_execute`.

### Q39. "Why write your own loop when LangGraph exists?"
**Answer:** §6 requires it, and it pays off: the loop is ~200 lines whose every branch is tested,
it forced the shared pieces (`Harness`, `EvidenceStore`, `AgentRunResult`, `ExecutionStore`) to be
framework-independent, and it is the baseline the LangGraph workflow must beat. What the loop lacks
— durable checkpoints and pause/resume for approvals — is exactly what the graph adds next.
Pointer: `app/agents/result.py` (`mode: "loop" | "graph"`); `app/agents/evidence.py` docstring.

### Q40. "Returning a fixed fallback string on `failed_validation` throws away a probably-correct answer."
**Answer:** "Probably correct" is the problem: it failed the only check we have. A grounded-QA
product's value is that answers are traceable; one confident, uncited wrong answer costs more trust
than a refusal. The draft is still visible to operators through the ledger and logs, not to users.
Pointer: `app/agents/result.py:build_result` (`answer` only for answered/insufficient_evidence).

---

## System design

### Q41. How does the loop fit into the platform?
**Answer:** API request → `ExecutionContext` (tenant, tightened limits, allowed tools) →
`ExecutionStore.start` → `AgentLoop.run` → every model/tool call through `Harness` (policy +
ledger) → tools call retrieval/ingestion scoped by `ctx.tenant_id` → `EvidenceStore` labels and
fences → `AgentRunResult` → `ExecutionStore.save`. The LangGraph workflow will reuse the harness,
evidence store, result type and store with `mode="graph"`, so the two can be evaluated head to
head. SSE and Langfuse will read the same ledger events.
Pointer: `app/services.py:build_services`; `app/agents/*`.

### Q42. How would you run this for many concurrent users?
**Answer:** Make the loop stateless per step: persist messages + evidence + counters per execution
(a checkpoint), write ledger events per step, and run steps on workers behind a queue so a GPU pool
can be shared and a run can resume after a crash or an approval. Enforce per-tenant token/cost
budgets with atomic DB counters across executions, cache deterministic tool results per tenant +
arguments + index version, and route easy questions to the single-shot path or a smaller model.
Pointer: `app/agents/store.py`; `app/core/budgets.py`; journal 03 §8.

### Q43. How does approval/resume work today, and what is missing?
**Answer:** The harness raises `ApprovalRequiredError` and records `approval/pending` with
arguments; the loop maps it to `awaiting_approval` and `save` leaves `finished_at` empty. Missing:
writing the `approvals` row, persisting the conversation so the loop can resume, and an API to
approve (adds the `call_id` to `approved_call_ids`). The custom loop has no resume path; LangGraph's
interrupt + checkpointer is the planned mechanism.
Pointer: `app/agents/custom_loop.py:run`; `app/agents/store.py:save`; `app/db/models.py:Approval`.
