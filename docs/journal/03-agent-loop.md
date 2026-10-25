# Journal 03 — Custom Agent Loop + Execution Ledger (Day 2, M3)

Milestone: third part of Day 2 in product.txt §18 / §6 (the hand-written agent loop, built before
LangGraph), plus persistence of the execution ledger and the fixes for the M1-M2 Recorder findings.
Commit `3ef6226` on `day-2-harness-loop-graph`. Status: **194 tests green** (unit + integration on
Postgres), 30 more than M1-M2; live `qwen3:8b` on an Apple M4 / 16 GB passes **2/2 runs** for both
live questions. Written by the Recorder agent from the code and test output, not from the plan.

LangGraph, the approvals API, resume-after-approval, SSE and an HTTP endpoint for the loop are
**not** in this milestone. The loop is reachable as `services.agent_loop.run(ctx, question)`.

---

## 1. What was built, by subsystem

### The loop (`app/agents/custom_loop.py`)
`AgentLoop(harness, max_context_chars=20_000, max_output_tokens=1024)`; `services.py` wires it with
`settings.agent_max_context_chars` and `settings.llm_max_output_tokens`.

`run(ctx, question)` creates an `EvidenceStore`, calls `_run`, and converts any `AppError` into a
typed `AgentRunResult` (never raises an `AppError` to the caller). `_run` follows §6:

1. `tools = registry.specs(ctx.allowed_tools) + [final_answer spec]` (computed once per run).
2. Messages start as `[system SYSTEM_PROMPT, user "Question: …"]`; `ctx.workflow_state = "running"`.
3. Per iteration: `harness.begin_step` → `harness.call_model(ctx, "main",
   LLMRequest(messages=bound_history(...), tools, max_output_tokens), name="agent")` → the assistant
   message (content + tool calls) is appended.
4. **Classify the response into exactly one outcome:**
   - **FINAL** if any call is `final_answer`, or if there are no tool calls at all (content is then
     tried as JSON). Handled by `_finish`.
   - **TOOL_CALLS** otherwise: each call goes through `_execute` in order and its result is appended
     as a `role="tool"` message with the same `tool_call_id`.
5. Loop until `_finish` returns a result or the harness raises a terminal error.

`_finish` (FINAL handling):
- `final_answer` together with other calls → problem "final_answer must be the only tool call".
- Otherwise `parse_final(arguments or None, content)`; a `ValueError` becomes the problem text
  (or "reply with a tool call or final_answer" when there was no `final_answer` call, i.e. prose).
- `validate_final` checks citations against `store.labels`.
- Clean → status `answered` or `insufficient_evidence`, ledger `final/ok`, `build_result`.
- Problems → `consume_correction(kind)` where kind is `invalid_output` (no parseable answer) or
  `citations` (parsed but invalid). When the allowance is spent → status **`failed_validation`**,
  the draft is *not* returned (`FALLBACK_TEXT`). Otherwise the feedback ("Your previous reply was
  rejected: …. Valid IDs: [...]") goes back as a `tool` message per call (so every tool call has a
  result) or as a `user` message after prose.

`_execute` (TOOL_CALLS handling):
- `_signature(call) = name + json.dumps(arguments, sort_keys=True)`. A signature already in
  `seen_calls` is **not executed**; it consumes a `repeated_tool_call` correction and the model gets
  "Duplicate call with identical arguments; the result is already above."
- Otherwise `harness.call_tool`. `TERMINAL = (BudgetExhaustedError, DeadlineExceededError,
  ExecutionCancelledError, ApprovalRequiredError)` propagate and end the run. **Every other
  `AppError`** (invalid arguments, tool failure/timeout, `ToolPermissionError`, `NotFoundError` from
  `document_metadata`) becomes `<tool_error code="…">message</tool_error>` for the model.
- `document_search` output → `store.add_passages` + `render_passages`; any other tool →
  `add_tool_result` + `render_tool_result`.

`SYSTEM_PROMPT` lists the tools, requires inline `[S1]`/`[T2]` citations, `insufficient_evidence`
when the documents lack the answer, no repeated identical calls, and marks tool results as untrusted.
`PROMPT_VERSION = "agent-loop-v1"` is declared but not yet persisted anywhere.

### Evidence (`app/agents/evidence.py`)
- `FinalAnswer` (the FINAL contract, also the `final_answer` tool schema via `_final_spec`):
  `answer` (≤ 6000 chars), `citations` (≤ 20), `insufficient_evidence`.
- `EvidenceStore`: passages get **S1, S2, …**, other tool results **T1, T2, …**, numbered across the
  whole run. Dedupe by `chunk_id` *or* normalised text (whitespace-collapsed, lowercased); a repeat
  keeps its first label and is rendered as "(Already shown above, not repeated: S1)". Passage text is
  cut to `max_passage_chars=1500` for the prompt.
- Fencing: `<evidence><source id file version document_id page section>…</source></evidence>` and
  `<tool_result id tool>json</tool_result>`; text goes through `escape_untrusted` (renamed from the
  Day 1 private `_escape` in `app/rag/answer.py`), which neutralises `</source` / `</evidence`.
- `validate_final`: unknown labels (never shown in this run) → error; `answered` with no citation at
  all → error; empty answer → error. `cited_labels` = `citations` field ∪ inline `[S#]`/`[T#]`.
- `parse_final`: tool arguments if present, else `model_validate_json(content)`; schema errors are
  reported as `field: error_type` (no values echoed).
- `citations(labels)` resolves S-labels to the Day 1 `Citation` model (chunk/document IDs, filename,
  pages, 300-char snippet); `references(labels)` returns cited T-results.

### Bounded history (`app/agents/context.py`)
`bound_history(messages, max_chars, keep_head=2)`: always keeps system + question; groups the rest
into turns (an assistant message plus the `tool` messages that follow it), drops whole groups oldest
first, always keeps the newest group even if it alone is over budget, and inserts a
`[N earlier turn(s) omitted to fit the context.]` user note. Size is measured in characters
(content + tool-call arguments). Companion change: `OllamaProvider` now sends `num_ctx`
(`settings.llm_num_ctx`, default 8192, 2048–40960) because Ollama silently truncates over-long
prompts.

### Result (`app/agents/result.py`)
`RunStatus` = `answered | insufficient_evidence | failed_validation | awaiting_approval |
budget_exhausted | deadline_exceeded | cancelled | failed`. `AgentRunResult` carries
`execution_id`, `request_id`, `mode` (`loop`|`graph`), status, answer, `citations`,
`tool_references`, `error_code`, steps/model_calls/tool_calls, `corrections`, `usage`, `cost_usd`
(None when unknown), `total_ms`, full `ledger`. `status_for_error` maps the policy errors;
`build_result` only exposes the model's text for `answered`/`insufficient_evidence` and only resolves
citations for `answered`. Shared with the coming LangGraph workflow.

### Persistence (`app/db/migrations/versions/0002_agent_executions.py`, `app/db/models.py`, `app/agents/store.py`)
- `agent_executions`: typed counters/status columns (`steps`, `model_calls`, `tool_calls`,
  `input_tokens`, `output_tokens`, `cost_usd NUMERIC(12,6)`, `total_ms`), `question`, `answer`,
  `citations JSONB`, `limits JSONB`, `created_at`, `finished_at`; index `(tenant_id, created_at)`.
- `execution_events`: one row per `LedgerEvent`, FK `ON DELETE CASCADE`, **unique
  `(execution_id, seq)`** (`uq_events_execution_seq`).
- `approvals`: `(execution_id, call_id)` unique, `tool_name`, `arguments JSONB`, `status`, `reason`,
  `decided_at`; index `(tenant_id, status)`. Table only; nothing writes it yet.
- `ExecutionStore.start` inserts a `running` row; `save` locks the row (`with_for_update`), checks
  tenant, writes outcome + counters, sets `finished_at` unless `awaiting_approval`, and bulk-inserts
  the whole ledger with `ON CONFLICT ON CONSTRAINT uq_events_execution_seq DO NOTHING` — so saving
  twice is idempotent. `get` and `events` filter by `tenant_id` (other tenant → `NotFoundError`).

### Review fixes from M1-M2 (journal 02 §7)
| Finding | Fix in `3ef6226` |
|---|---|
| 1. Approval keyed by repeatable `call_0` | `OllamaProvider` IDs are `call_<uuid4 hex[:12]>` |
| 2. Calculator `OverflowError`/`0**-1` escape | caught in `_eval`; `abs()` checked before `math.isfinite` |
| 3. Tool budget after permission/arguments | `call_tool` checks the tool-call limit right after `ensure_active` |
| 4. Double ledger entry on in-attempt deadline | `attempt()` uses `_inactive(ctx)` (no record); the outer handler records once |
| 5. Token budget overshoot | `max_output_tokens = min(request, max_total_tokens - used)` |
| 7. `tightened()` `KeyError`, cost cap ≤ 0 | unknown keys → `ValueError`; `max_cost_usd` / `agent_max_cost_usd` `gt=0` |
| 8. Pending approval lacks arguments | `approval/pending` event now has `arguments`; `LedgerEvent` docstring now admits validated arguments are stored |
| 9. `NotFoundError` from metadata | loop treats it as a non-terminal `<tool_error>` |
| 11. `arguments: null` → raw string | `None` arguments become `{}` |

Also: `IngestionService.mark_for_reindex` is now one conditional `UPDATE … WHERE status NOT IN
(pending, processing) RETURNING id` (claim-once); `document_search.top_k` floor raised to **3**.

---

## 2. Files touched

```
app/agents/{custom_loop,evidence,context,result,store}.py        (new)
app/db/migrations/versions/0002_agent_executions.py              (new)
app/db/models.py                       (AgentExecution, ExecutionEvent, Approval)
app/agents/harness.py, app/core/{budgets,config,execution_context}.py
app/llm/provider.py, app/rag/answer.py (escape_untrusted), app/services.py
app/tools/{calculator,document_search}.py, app/ingestion/pipeline.py
tests/unit/test_agent_loop.py, tests/integration/test_agent_loop_db.py, tests/live/test_agent_live.py (new)
tests/unit/{test_harness,test_calculator,test_llm}.py, tests/integration/test_ingestion.py, tests/conftest.py
```

---

## 3. Key design decisions (taken vs rejected)

| # | Decision taken | Rejected alternative | Why / cost accepted |
|---|---|---|---|
| 1 | **FINAL is a tool (`final_answer`)**, with JSON content as fallback | Treat any plain-text reply as the answer | Tool-calling gives a schema the provider enforces; the answer and its citations arrive as structured arguments. Prose is never executed *or* trusted (`test_plain_text_reply_is_not_executed_or_trusted`). Cost: a model that "talks first" pays an `invalid_output` correction (observed live, ~7-12 s per run). |
| 2 | **`final_answer` must be alone in its turn** | Run the other calls, then accept the final | An answer written in the same turn as a search cannot have seen that search's results. Rejecting it is cheaper than reasoning about which evidence it used. |
| 3 | **Duplicate calls are short-circuited and cost a correction** (`_signature`, `seen_calls`) | Re-execute, or ignore silently | Re-running a deterministic read wastes latency and budget; silent ignoring lets a stuck model loop until `max_steps`. With 2 corrections the third duplicate ends the run (`budget_exhausted`). |
| 4 | **Only four errors are terminal** (`TERMINAL`); permission, argument, execution, timeout, not-found errors go back to the model | End the run on any tool error (journal 02 listed `ToolPermissionError` as policy) | A model that asks for a forbidden tool can still answer with the tools it has; the harness has already refused, so feeding the refusal back is safe. Budgets/deadline/cancel/approval cannot be fixed by the model. |
| 5 | **Labels assigned by the agent across the run** (S# passages, T# other tools) in `EvidenceStore` | Per-call labels from the tool | Two searches would both produce `S1`; stable labels make citations unambiguous and let a calculator result be cited (`T1`). |
| 6 | **Citations validated against labels shown in this run** | Trust the model's citation list; or check only format | Fabricated IDs (`S9`) are caught and corrected once (`test_unknown_citation_gets_one_correction_then_succeeds`); exhausted corrections give `failed_validation`, never the unverified draft. |
| 7 | **Dedupe passages by chunk ID *and* normalised text** | Chunk ID only | Re-ingested or copied paragraphs come back with new chunk IDs; sending them again wastes context. Cost: identical text in two *versions* collapses to the first label (see §7). |
| 8 | **History bounded by whole turns; system + question pinned** | Truncate characters/tokens from the front | Splitting an assistant tool call from its tool results produces a malformed conversation some providers reject; losing the question loses the task. Cost: character count is a proxy for tokens. |
| 9 | **`num_ctx` set explicitly for Ollama** | Rely on Ollama's default context | Ollama truncates silently; the default was smaller than the agent's prompt + tool schemas. 8192 fits qwen3:8b in 16 GB. |
| 10 | **Ledger persisted as rows keyed `(execution_id, seq)`, insert-or-ignore** | One JSONB blob per execution; or append on every event | Rows are queryable (failures by `error_code`, latency per tool) and a paused graph run can `save` again without duplicates. Cost: written only at `save` time, so a crash mid-run loses the in-memory ledger. |
| 11 | **Typed columns for counters, JSONB only for variable shapes** (`citations`, `limits`, `data`) | Everything in JSONB | Dashboards and evals aggregate `model_calls`, tokens and status with plain SQL and indexes. |
| 12 | **`top_k` floor of 3** in `DocumentSearchInput` | Silently clamp `top_k`; or trust the model | Live qwen3:8b asked for `top_k=1`, retrieved only v1 and (correctly) gave up. A validation error is visible in the ledger and the model self-corrects; clamping would hide the model's behaviour. Cost: one wasted round-trip per run. |
| 13 | **Tool calls in one turn run sequentially** | `asyncio.gather` | Deterministic ledger order and simple budget accounting; parallelism matters little with one local GPU. |
| 14 | **`mark_for_reindex` as one conditional `UPDATE … RETURNING`** | Read status, then update | Check-then-act lets two concurrent requests both pass. The new concurrency test is a regression guard; it does not reproduce the old race. |

---

## 4. Single-shot Answerer (Day 1) vs the agent loop (M3)

| | `Answerer.answer` (`app/rag/answer.py`) | `AgentLoop.run` (`app/agents/custom_loop.py`) |
|---|---|---|
| Control flow | Fixed: 1 retrieval (`top_k=6`) → 1 generation → ≤ 1 correction | Model chooses tools and how many; ≤ 8 steps |
| Retrieval query | The user's question verbatim | Model-written queries, can search several times, filter by `document_ids`/labels |
| Tools | None (retrieval is code, not a tool) | `document_search`, `document_metadata`, `calculator` |
| Output contract | `json_schema` constrained decoding (`AnswerDraft`) | `final_answer` tool call (`FinalAnswer`), JSON-content fallback |
| No evidence | Returns `insufficient_evidence` **without a model call** | Model must decide and call `final_answer(insufficient_evidence=true)` |
| Policy | `LLMRouter` timeout only; no harness, no ledger | Every call through `Harness`: deadline, budgets, permissions, ledger |
| Cost/latency | 1-2 model calls | RPO question live: 44-58 s, ~8.5k tokens |
| Failure modes | Bad retrieval = bad answer, cannot recover | More moving parts: wrong arguments, prose replies, loops (all bounded) |

**When the single-shot Answerer is the right tool:** factual lookups answerable from one retrieval
of the question text; latency- or cost-sensitive paths; high-volume FAQs; whenever predictability
and evaluability matter more than flexibility. It is also the baseline for evals: if the loop does
not beat it on the eval set, the loop is not worth its cost.

**When the loop is worth it:** multi-hop questions ("latest version" needs metadata or a second
search), questions needing a computation (RPO in seconds), questions whose wording is a poor search
query, and anything that will later need write tools and approvals. The live run shows the price:
the model needed one argument correction (`top_k`) and one prose-reply correction before it answered.

---

## 5. Commands

```bash
AGENTOPS_DB_PORT=5435 docker compose up -d db   # port used on this machine; default is 5433
export AGENTOPS_TEST_ADMIN_DSN=postgresql://agentops:agentops@localhost:5435/agentops
export AGENTOPS_TEST_DATABASE_URL=postgresql+asyncpg://agentops:agentops@localhost:5435/agentops_test
uv run alembic upgrade head      # applies 0002_agent_executions
uv run pytest -m "not live"      # 194 passed
uv run pytest tests/unit/test_agent_loop.py tests/integration/test_agent_loop_db.py
uv run pytest -m live tests/live/test_agent_live.py -s   # needs Ollama + qwen3:8b; prints the ledger
uv run ruff check app tests && uv run mypy app
```

---

## 6. How it was verified

- **194 passed, 5 deselected (live)** on Postgres; `test_agent_loop.py` + `test_agent_loop_db.py`
  = 20 tests. The §6 required list maps to: normal completion
  (`test_normal_completion_with_citation`), multiple tool calls
  (`test_multiple_tool_calls_in_one_turn_and_tool_result_citation`), repeated loops
  (`test_repeated_identical_tool_calls_are_not_re_executed_and_are_bounded`, search runs once),
  invalid arguments, tool failure (no `detail` leak), timeout (idempotent search retried once, then
  `tool_timeout` to the model), cancellation, and step/token/deadline budget exhaustion.
- Extra unit tests: citation correction, `failed_validation` never returns the draft, prose not
  executed, JSON content accepted, `final_answer` mixed with other calls rejected, disallowed tool
  refused, fence break-out escaped, evidence dedupe, `bound_history` keeps groups whole.
- Integration (real retrieval + Postgres, scripted model): answer cites the ingested document;
  persisted row and 6 events in order `step, model_call, tool_call, step, model_call, final`; a
  second `save` adds no events; tenant B gets `NotFoundError` for row and events.
- **Live, qwen3:8b on M4 16 GB, 2/2 runs each:**
  - "What is the RPO in the latest payments architecture, and how many seconds is that?" →
    `answered`, contains 300, cites `payments-architecture-v2.md`; 44-58 s, ~8.5k tokens.
  - "What is the CEO's favourite colour?" → `insufficient_evidence`; 22-28 s.
  - Observed in ledgers: the first search asks `top_k=1`, is rejected (`invalid_tool_arguments`,
    floor 3) and the model self-corrects; the model usually sends one prose reply before
    `final_answer`, costing an `invalid_output` correction (~7-12 s); it computed 5×60 itself
    instead of calling `calculator`, despite the system prompt.
  - Before the floor existed, a `top_k=1` search returned only v1; the agent answered
    `insufficient_evidence` rather than inventing the v2 value — the right failure.

---

## 7. Known limitations and open findings

Found by the Recorder while reading the code (not fixed in this milestone):

1. **`<tool_result>` fence can be closed by tool output.** `render_tool_result`
   (`evidence.py:93`) relies on `escape_untrusted`, whose regex only matches `source|evidence`
   (`app/rag/answer.py:25`). A `document_metadata` result with a filename or label containing
   `</tool_result>` is rendered verbatim (checked: the closing tag survives).
2. **Identical text across document versions collapses to the first label.**
   `add_passages` dedupes on normalised text (`evidence.py:56-57`). If v1 and v2 share a paragraph
   and v1 ranks first, the v2 chunk is dropped and the answer can only cite v1. In the same call it
   also reports S1 as both new and "already shown".
3. **A failed call cannot be retried with the same arguments.** `_execute` adds the signature to
   `seen` *before* `call_tool` (`custom_loop.py:191`). After a `tool_timeout` or transient
   `retrieval_error`, re-sending the same call is treated as a duplicate ("the result is already
   above" — but the result above is an error) and burns a correction.
4. **Citation check is "at least one valid label", not "documents back the claims".**
   `validate_final` passes an answer citing only `[T1]` (a calculator result) with no document
   citation, and passes any text when `insufficient_evidence=true` (`evidence.py:131`) — that text is
   returned to the user uncited. `verify_answer` is planned for the graph.
5. **Inconsistent outcome codes.** Corrections exhausted on citations → `status="failed_validation"`
   but `error_code="budget_exhausted"` (`custom_loop.py:167` passes the `BudgetExhaustedError`);
   the ledger's `final` event says `failed_validation`. Exhausted *duplicate* corrections end as
   `budget_exhausted`, not `failed_validation`.
6. **Only `AppError` is caught** (`custom_loop.py:84`). A bug (e.g. `KeyError` in
   `add_passages`) or task cancellation (`asyncio.CancelledError` on client disconnect) escapes; the
   caller never calls `save`, and the `agent_executions` row stays `running` forever with no ledger.
7. **Bounded history can drop evidence that is still citable.** `bound_history` may omit the turn
   holding `S1`, but `store.labels` still contains it, so the model can cite text it no longer sees.
   The newest group is kept even if alone it exceeds `max_chars` (`context.py:33`), and tool
   schemas are not counted, so `num_ctx` can still be exceeded; Ollama then truncates from the front,
   i.e. the system prompt.
8. **Token cap is still not exact.** `max_output_tokens` is capped to what is left, but input tokens
   are only known after the call, so a run can still overshoot by one prompt; late in a run the cap
   can shrink output to a few tokens and truncate a `final_answer`, which then costs a correction.
9. **Awaiting approval is a dead end in the loop.** `run` returns `awaiting_approval` but the
   message history is not saved and there is no resume; nothing writes the `approvals` table yet.
10. `PROMPT_VERSION = "agent-loop-v1"` is not persisted or returned, so runs cannot be grouped by
    prompt version (the Day 1 `GroundedAnswer` does carry `prompt_version`).
11. JSON-content fallback rejects fenced JSON (```` ```json … ``` ````) and `<think>` prefixes:
    `model_validate_json(content)` must parse the whole content.
12. The ledger is persisted only at `save` (end of run); no live progress, and a process crash loses
    it. Model-written `<tool_error>` text includes the model-chosen tool name unescaped.
13. Live behaviour the prompt does not fix: qwen3:8b skips the calculator for simple arithmetic and
    often replies in prose first. Both are bounded, but each costs time on local hardware.

---

## 8. What I'd do differently at 100x scale

- **Write events as they happen** (or batch every step) and stream them over SSE; mark stale
  `running` rows via a heartbeat/sweeper so crashed runs become `failed`.
- **Persist the conversation state** (messages + evidence map) per execution so a run can pause for
  approval and resume on any worker — this is exactly what LangGraph checkpoints provide (M4).
- **Token-accurate context bounding** with the model's tokenizer and a reserve for tool schemas and
  output, instead of a character proxy.
- **Route by question type**: send single-hop questions to the single-shot `Answerer` (or a small
  model) and only escalate to the loop when a classifier or a failed first attempt says so; at
  44-58 s per agent run on local hardware, the loop cannot be the default path for every query.
- **Cache deterministic tool results** per tenant + arguments + index version, so duplicate calls
  across runs are free, and parallelise independent tool calls once the provider has capacity.
- **Answer verification** (claim-level support check against cited passages) before returning,
  as planned for the graph's `verify_answer` node.
