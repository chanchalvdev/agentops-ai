# Journal 07 — Observability: Traces from the Ledger, Langfuse, Optional LangSmith (Day 3, M7)

Milestone: the observability part of Day 3 in product.txt §18 — "Langfuse adapter (no-op fallback),
optional LangSmith" — implementing §10. Branch `day-3-workspace-observability-eval`, **uncommitted**
(the observability package is untracked; the hooks are mixed into the same working-tree diff as the
M6 workspace tools). Status: **244 passed** for `uv run pytest -q -W error tests/unit tests/security`
after the fixes in §10 (Recorder re-run: `244 passed in 2.73s`; before the fixes: `239 passed in
2.94s`); `tests/unit/test_observability.py` is **13 tests** (12 before the fixes). `ruff check` and `mypy` on `app/observability` are clean. Integration tests
were **not** run: they need Postgres, and Docker is off on purpose. Written by the Recorder agent
from the code and test output, not from the plan.

**No live Langfuse or LangSmith project was used.** Both adapters are verified only against
`httpx.MockTransport` (request URL, auth header, body shape). Whether the real services accept every
field as sent has not been checked; both module docstrings say so too.

Not in this milestone: the evaluation runner that sets `source="eval"` and pushes scores (M8), the
§14 `GET /api/v1/metrics` endpoint and dashboards (Day 4 monitoring), live/streaming spans.

---

## 1. What was built, by subsystem

### The idea: a trace is derived from the execution ledger after the run
Since Day 2 the harness records every node step, model call, tool call, rejection, approval,
correction and the final decision into `ctx.ledger` (`LedgerEvent`: `seq`, `kind`, `name`, `status`,
`at`, `latency_ms`, tokens, `error_code`, `data`). M7 does **not** add span code to nodes. Instead one
pure function, `build_trace(ctx, result, question=, capture_io=)` (`app/observability/telemetry.py:63-195`),
turns the finished ledger plus the `AgentRunResult` into a vendor-neutral `TraceRecord`, and adapters
translate that into each vendor's wire format. Rationale, from the module docstring (`telemetry.py:1-10`):
the trace and the persisted audit trail cannot disagree, nodes stay vendor-free, and no exporter can
slow down or break a run.

### Vendor-neutral model (`app/observability/interfaces.py`)
| Type | Fields | Notes |
|---|---|---|
| `Observation` | `id, kind (span\|generation\|event), name, start, end, parent_id, level (DEFAULT\|WARNING\|ERROR), status_message, metadata, input, output, model, input_tokens, output_tokens` | The last three only for generations. |
| `TraceRecord` | `id (= execution id), name, start, end, source (api\|cli\|eval), terminal, input, output, metadata, tags, observations` | `terminal=False` while the run waits for approval. |
| `Score` | `trace_id, name, value, comment` | For evaluation scores (pushed by M8). |
| `TraceSink` (Protocol) | `name`, `accepts(trace) -> bool`, `async export(trace)`, `async score(score, source)` | `accepts` is the routing/sampling decision. |

### `build_trace` — how ledger events become observations (`telemetry.py:63-150`)
| Ledger event | Observation | Parent | Timing |
|---|---|---|---|
| `step` (one per graph node / loop turn) | `span` named after the node (`classify_request`, `plan_workflow`, `retrieve_evidence`, …) | none (top level) | start = `at`; end = next step's `at`, or the last ledger event's `at`; ends immediately if status ≠ ok |
| `model_call`, not rejected | `generation` (name = call name: `classify`, `plan`, `generate_answer`, `verify`, …) with `model`, `input_tokens`, `output_tokens`, metadata `role, provider, attempts, pricing_source, tool_calls` | current step | start = `at - latency_ms`, clamped to ≥ the step's start; end = `at` |
| `tool_call`, not rejected | `span` `tool:<name>`; input = redacted `arguments` (only if `capture_io`); output = redacted `data["result"]` minus `timings_ms`; metadata `call_id, attempts` | current step | same reconstruction |
| `document_search` with `timings_ms` | child spans `embed_query`, `vector_search`, `keyword_search` (`_retrieval_spans`) | the tool span | chained in sequence, as `HybridRetriever.search` runs them: embed at the tool span's start, vector when embed ends, keyword when vector ends (fixed after review, §10) |
| everything else (rejections, `policy`, `approval`, `correction`, `final`) | `event` named `<kind>:<name>` (e.g. `final:graph`, `approval:save_note`), metadata = redacted `{status, **data}`; with `capture_io=False` the `arguments` and `message` data keys and the free-text `message` fallback are dropped (§10) | current step | point in time `at` |

Error mapping: ledger status `error` → level `ERROR`, `rejected` → `WARNING`; `status_message` =
`error_code` (events fall back to `message`). Caps: at most 500 ledger events become observations
(`_MAX_OBSERVATIONS`), question/answer text cut to 2000 chars (`_MAX_TEXT`).

Trace-level fields (`telemetry.py:152-195`): `name = agent.graph | agent.loop`; `start/end` = min/max
over observations; `input = {"question": …}` only if `capture_io`; `output = {status, answer (None if
not capture_io), citations ["S1:file.md", …]}`; tags `[mode, status, source]`; metadata
`request_id, tenant_id, mode, status, error_code, verification, steps, model_calls, tool_calls,
corrections, input_tokens, output_tokens, cost_usd (str), total_ms, prompt_version,
workflow_version, cited_chunk_ids`.

### Ids and correlation
- **Correlation id = `execution_id`.** Trace id = execution id (Langfuse uses `execution_id.hex`,
  32 lowercase hex, which is also a valid W3C/OTel trace id — `langfuse_adapter.py:62`; LangSmith root
  run id = `str(execution_id)`). The same id is returned by the API, stored on the ledger rows and in
  the logs. `request_id` travels in trace metadata.
- **Observation ids are deterministic:** `uuid5(execution_id, "step:<seq>" | "event:<seq>" |
  "<seq>:embed" …)` (`telemetry.py:74-75`). Re-building the trace for the same execution yields the
  same ids, so re-exporting after an approval resume **upserts** in Langfuse instead of duplicating.
  Tested by `test_trace_ids_are_stable_so_reexport_upserts`.
- **Langfuse envelope ids are random** (`uuid4`, `langfuse_adapter.py:27-35`): Langfuse de-duplicates
  on the envelope id, so a stable envelope id would make the second send a no-op. Body id = stable
  object id → update; envelope id = per-send → never dropped.
- Langfuse score id = `uuid5(trace_id, "score:<name>")` (`langfuse_adapter.py:97`), so re-scoring the
  same metric overwrites.

### `Telemetry` — fire-and-forget fan-out (`telemetry.py:229-278`)
- `submit(ctx, result, question)` (241-254): no sinks → return; build the trace inside `try`
  (a bug in `build_trace` is logged, never raised); for each sink whose `accepts(trace)` is true,
  `_spawn` its `export`. **Never raises, never awaits the network.**
- `score(score, source)` (256-258): spawns `sink.score` on **every** sink; each sink decides
  (LangSmith drops non-eval).
- `_spawn` (260-267): if 100 tasks are already pending (`_MAX_PENDING`), close the coroutine and log
  `telemetry backlog full; dropping export` — drop rather than grow memory without bound. Otherwise
  `asyncio.create_task(_guarded(...))`, tracked in `_pending`, removed by a done-callback.
- `_guarded` (269-274): `asyncio.timeout(timeout_s)` around the export; any `Exception` → one warning
  log line (`telemetry export failed`, sink name, `repr(exc)`).
- `flush(timeout_s=None)` (276-278): `asyncio.wait` on the pending set with a bound (default
  `telemetry_timeout_s`). Called by `Services.aclose()` (`services.py:112`) — so at API shutdown and
  at the end of every CLI command (`_with_services` → `aclose`).

### Sinks
- **`LogSink`** (`telemetry.py:198-226`) — the local/no-op-equivalent backend required by §10. One
  structured `trace` log line per run: `trace_id, trace_name, source, duration_ms`, counts of spans /
  generations / events, `status, request_id, prompt_version`; one `score` line per score. Accepts
  everything. On by default (`telemetry_log=True`).
- **`LangfuseSink`** (`app/observability/langfuse_adapter.py`) — plain HTTP, not the SDK.
  `POST {host}/api/public/ingestion` with HTTP basic auth `(public_key, secret_key)` and body
  `{"batch": [...]}`. `to_batch` (61-77): one `trace-create` (with `version = workflow_version`,
  `release = prompt_version`, tags, metadata) then one `span-create` / `generation-create` /
  `event-create` per observation (`parentObservationId`, `startTime`, `endTime` except events,
  `level`, `statusMessage`; generations add `model` and `usage {input, output, total, unit:
  "TOKENS"}`). `None` fields are dropped from bodies (`_envelope`). `raise_for_status()`; a **207**
  with a non-empty `errors` list raises `RuntimeError("langfuse rejected N event(s)…")`
  (`_post`, 106-111) — partial failure is not silent. Accepts every trace.
- **`LangSmithSink`** (`app/observability/langsmith_adapter.py`) — plain HTTP, `x-api-key` header.
  `export` → `POST {endpoint}/runs/batch` with `{"post": to_runs(trace, project)}`; `score` →
  `POST {endpoint}/feedback` `{run_id, key, score, comment}`. `to_runs` (30-76) emits a root `chain`
  run plus one child per observation; `run_type` = `llm` for generations, `tool` for names starting
  `tool:`, else `chain`; the tree is encoded in `dotted_order` (parent's dotted order + `.` +
  `%Y%m%dT%H%M%S%fZ` start + run id); generations carry `outputs.usage_metadata` and
  `extra.metadata.ls_model_name` / `ls_provider` (default `ollama`). Root `error` is `None` only for
  `answered` / `insufficient_evidence`, otherwise `error_code` or status.
  **Routing:** `accepts(trace) = trace.terminal and in_scope(trace.source)`; scope `"eval"` (default)
  means only `source == "eval"`; scope `"all"` sends everything. `score` is skipped unless in scope.

### LangSmith scope "eval" = §10's comparison requirement
§10: "compare the two systems on a small evaluation subset without sending all production data to
both." With the default scope, Langfuse gets everything (it is the primary backend) and LangSmith
gets only finished runs whose context has `source == "eval"`, plus only eval feedback. Only
terminal runs are sent so each LangSmith run id is posted exactly once (a run paused for approval is
sent when it completes). **The evaluation runner that sets `ctx.source = "eval"` and calls
`Telemetry.score` does not exist yet — that is M8.** The hook is in place and tested end to end
through `AgentService` with `source="eval"` (`test_agent_service_submits_every_finished_run`).
Nothing in `app/` calls `Telemetry.score` today.

### Hooks in the rest of the application
- **`AgentService._trace`** (`app/agents/service.py:182-184`) → `telemetry.submit(ctx, result,
  question)`. Called from `_execute` (148-180) **after** the execution row is saved on every exit
  path: success, the `except Exception` → `failed/internal_error` path, `awaiting_approval` (after the
  approval row is created), and the `CancelledError` path (after the shielded save, before the
  re-raise, line 164). Not called on `ConflictError` (nothing ran) or if `save` itself raises.
  `run()` passes the question; since the review fix, `decide()` (resume) loads the execution row
  and passes `question=execution.question` too (§10).
- **`ExecutionContext.source`** (`app/core/execution_context.py:51`): `"api"` default, included in
  `snapshot()` (98) and `restore()` (126, defaulting to `"api"` for old checkpoints), so a resumed
  run keeps its origin. The CLI sets `ctx.source = "cli"` (`app/cli.py:137`).
- **Tool result summaries in the ledger:** `Tool.ledger_output(output) -> dict` (`app/tools/base.py:43`,
  default `{}`) is stored as `data["result"]` on the successful `tool_call` event when non-empty
  (`app/agents/harness.py:306`). `DocumentSearch.ledger_output` (`app/tools/document_search.py:60-67`)
  returns `timings_ms`, `chunk_ids`, de-duplicated `document_ids` and `filenames` — **never passage
  text**. `DocumentSearchOutput.timings_ms` (46) is filled from the retriever's
  `RetrievalDiagnostics` (`embed, vector, keyword, total`, lines 82-87). This is how §10's "retrieved
  document IDs and chunk IDs" reach traces. (Workspace tools from M6 also override `ledger_output` /
  `ledger_arguments`, e.g. writes log path + size + sha256, not content.)
- **Versions:** `graph.WORKFLOW_VERSION = "agent-graph-workflow-v2"` (`graph.py:38`),
  `custom_loop.WORKFLOW_VERSION = "agent-loop-workflow-v1"` and `PROMPT_VERSION = "agent-loop-v2"`
  (`custom_loop.py:27-28`), `nodes.PROMPT_VERSION = "agent-graph-v2"` (`nodes.py:27`). `build_trace`
  picks the pair by `result.mode` (155-158).
- **Composition** (`app/services.py:140-163`, `build_telemetry`): `LogSink` if `telemetry_log`;
  `LangfuseSink` if **both** Langfuse keys are set; `LangSmithSink` if the LangSmith key is set. All
  share the services' `httpx.AsyncClient` (the same one the Ollama provider uses,
  `max_connections=20`). `Services.telemetry` is exposed; `aclose()` flushes first.

### §10 coverage
| §10 item | Where it shows up |
|---|---|
| Classification, planning, retrieval, tool selection, answer generation, citation validation, verification, finalization | One span per graph node step (names from the ledger), `final:<mode>` event |
| Embedding calls | `embed_query` child span of `tool:document_search` (from `timings_ms`) |
| Model generations | `generation` observations with model, tokens, latency |
| Tool execution | `tool:<name>` spans with redacted arguments and result summary |
| Retries | `attempts` in generation/tool metadata; `correction:<kind>` events |
| Tokens, model id, latency, cost, tool-call count, status | generation fields; trace metadata `input/output_tokens`, `cost_usd`, `total_ms`, `tool_calls`, `status`, `error_code` |
| Document and chunk IDs | `tool:document_search` output (`chunk_ids`, `document_ids`), trace `cited_chunk_ids` |
| Evaluation scores | `Score` → Langfuse `score-create` / LangSmith `/feedback` (caller arrives in M8) |
| Prompt and workflow versions | trace metadata; Langfuse `release` / `version` |
| No keys/auth headers/raw documents | `redact` on tool I/O and event metadata; passage text never in the ledger summary; `capture_io` |
| Single correlation id | `execution_id` everywhere, `request_id` in metadata |
| Optional LangSmith behind its own flag, eval subset | `AGENTOPS_LANGSMITH_API_KEY`, `langsmith_scope="eval"` |
| Runs without credentials | default sinks = `[LogSink]`; `telemetry_log=False` → no sinks at all |

---

## 2. Files touched

```
app/observability/__init__.py, interfaces.py, telemetry.py,
  langfuse_adapter.py, langsmith_adapter.py                         (new, untracked)
app/services.py              build_telemetry, Services.telemetry, aclose() flushes first
app/agents/service.py        telemetry param, question passed into _execute, _trace on every exit
app/core/config.py           telemetry_*, langfuse_*, langsmith_* settings
app/core/execution_context.py  `source` field, in snapshot()/restore()
app/tools/base.py            ledger_output() (and ledger_arguments(), shared with M6)
app/agents/harness.py        ledger_output -> tool_call data["result"]
app/tools/document_search.py timings_ms on the output, ledger_output with ids + filenames
app/agents/graph.py, app/agents/custom_loop.py   WORKFLOW_VERSION constants
app/cli.py                   ctx.source = "cli"
tests/unit/test_observability.py (new, 13 tests after the review fixes)
```
`.env.example` and the README were **not** updated with the new settings (§8).

---

## 3. Key design decisions (taken vs rejected)

| # | Decision taken | Rejected alternative | Why / cost accepted |
|---|---|---|---|
| 1 | **Derive the trace from the ledger after the run** (`build_trace`, pure) | Span code (`with tracer.span(...)`) in every node, tool and model call; LangChain/LangGraph callback handlers | The trace cannot disagree with the persisted audit trail; nodes stay vendor-free; exporters run after the run so they cannot slow or break it; one function to test. **Cost:** no live spans while a run is in progress (the SSE stream from M5 covers live progress), and start times are reconstructed (`at - latency_ms`). A process crash mid-run produces no trace (the ledger rows are still there). |
| 2 | **Plain HTTP adapters** (Langfuse ingestion API, LangSmith REST) | Langfuse SDK v3, `langsmith` SDK | One batched POST per finished execution under our own timeout and redaction; no background threads or OTel pipeline (per the Langfuse adapter docstring); the shared `httpx` client is mockable. **Cost:** we own the wire format; schema drift on the vendor side is our problem, and it was only checked against docs + mocks. |
| 3 | **Correlation id = `execution_id`**; `request_id` in metadata | Use the HTTP `request_id` as trace id | An execution outlives a request: an approval resume is a different HTTP request but the same execution and the same trace. |
| 4 | **Deterministic observation ids** (`uuid5(execution_id, seq)`) | Random ids per export | Re-export after resume upserts the same objects. **Cost:** the envelope id must still be random for Langfuse, which is a subtle rule (comment in `_envelope`). |
| 5 | **Fire-and-forget with a bounded backlog** (`_spawn`, `_MAX_PENDING=100`, per-export timeout) | Await export inline; unbounded queue; a background worker thread | A run's latency and success never depend on a vendor. **Cost:** traces are dropped (with a warning) when the backlog is full, and lost if the process dies before `flush`. |
| 6 | **Routing in the sink** (`accepts`, `score` checks scope) | Routing in `Telemetry` or the caller | Each backend owns its policy (LangSmith: eval + terminal only). `Telemetry.score` fans out to every sink and lets them decide. |
| 7 | **LangSmith only for `source == "eval"` and terminal runs, by default** | Send all traffic to both vendors; random sampling | Meets §10's "small evaluation subset" without duplicating production data at a second vendor; terminal-only means one POST per run id. `scope="all"` exists for debugging. |
| 8 | **Log sink as the default backend** | A true no-op | §10 asks for "no-op or local logging"; one structured line per run gives dev visibility at near-zero cost. Turning it off (`telemetry_log=False`) with no keys is the no-op. |
| 9 | **Summaries, not payloads, in the ledger** (`ledger_output`) | Store full tool outputs | Ids, counts, timings are what traces and evaluation need; passage text and file contents stay in the database/workspace. |
| 10 | **`capture_io` switch** for question/answer/tool-argument text | Always send / never send | Hosted traces are far more useful with the question and answer; a deployment with sensitive questions can turn it off without losing structure, timings, tokens or ids. |
| 11 | **Embedding spans reconstructed from retrieval diagnostics** | Instrument the embedder | No new code in the retrieval path; diagnostics already existed. **Cost:** placement is approximate (§8 item 2). |

---

## 4. Live verification

None against a real backend. No Langfuse or LangSmith credentials/project were available during the
build. Everything in §5 uses `httpx.MockTransport` or in-process recorder sinks. A live Ollama run
was not repeated for this milestone; the `LogSink` line is exercised by a unit test, not observed in
a live API log.

---

## 5. How it was verified

- `uv run pytest -q -W error tests/unit tests/security` → **244 passed in 2.73s** (Recorder run
  after the §10 fixes; 239 before them).
- `tests/unit/test_observability.py` → **13 tests** (12 before the fixes, + the round-trip test).
- `ruff check app/observability tests/unit/test_observability.py` clean; `mypy app/observability` →
  "no issues found in 5 source files".
- Integration suite not run (Postgres down by choice); nothing in M7 needs Postgres for its own tests.

`tests/unit/test_observability.py` (graph runs use the fake `GraphModel` and a `TimedSearch` whose
`timings_ms = {embed 40, vector 5, keyword 3}`):

| Test | What it proves |
|---|---|
| `test_trace_mirrors_the_graph_with_nested_observations` | trace id = execution id, `agent.graph`, terminal; spans for `classify_request, plan_workflow, retrieve_evidence, verify_answer`; generations exactly `[classify, plan, generate_answer, verify]` with model + tokens; generation parent = its step; `tool:document_search` under `retrieve_evidence` with `chunk_ids` and **no** `timings_ms` in output; children `[embed_query, vector_search, keyword_search]` **chained: `embed.end == vector.start`, `vector.end == keyword.start`** (added with the §10 fix); `final:graph` event; prompt/workflow version, request id, status, input, citations |
| `test_trace_ids_are_stable_so_reexport_upserts` | two builds → identical observation ids; no child starts before its parent |
| `test_capture_io_off_and_redaction` | `capture_io=False` → no trace input, `answer=None`, no span input; `api_key` key and a `Bearer …` value redacted |
| `test_langfuse_ingestion_batch` | URL `/api/public/ingestion`, Basic auth = `pk:sk`, first item `trace-create` with id = `execution_id.hex`, unique envelope ids, generation has `traceId`, parent, `usage.unit = TOKENS`, model; all four create types; score → `score-create` |
| `test_langfuse_partial_failure_is_reported` | 207 with `errors` → `RuntimeError("…rejected 1…")` |
| `test_langsmith_receives_only_finished_eval_runs_by_default` | api trace rejected, eval trace accepted, paused eval trace rejected, `scope="all"` accepts api; `x-api-key`; root id + `session_name`; every child's `dotted_order` extends its parent's; run types `{chain, llm, tool}`; `usage_metadata`, `ls_model_name`; feedback skipped for `api`, sent to `/feedback` for `eval` |
| `test_to_runs_marks_failed_runs` | non-success status → root `error = "budget_exhausted"` |
| `test_exporter_failures_and_timeouts_never_reach_the_caller` | one raising sink + one hanging sink (timeout 0.05 s) + one good sink: `submit` returns immediately, the good sink still gets the trace |
| `test_routing_and_scores_fan_out` | non-accepting sink gets no trace; scores reach every sink with the source |
| `test_agent_service_submits_every_finished_run` | real `AgentService.run` with `source="eval"` → one trace, id = execution id, source `eval`, input = question |
| `test_sinks_follow_configuration` | defaults → `["log"]`; keys + `telemetry_log=False` → `["langfuse", "langsmith"]` |
| `test_log_sink_writes_one_line` | one `trace` record carrying `trace_id` |
| `test_source_survives_a_checkpoint_round_trip` | `ExecutionContext.restore(ctx.snapshot()).source == "eval"` (added after review) |

Not covered by tests: the `CancelledError` and `failed` exit paths submitting a trace; the
`_MAX_PENDING` drop path; `flush` from `Services.aclose`; and two of the §10 fixes — `decide`
passing the stored question (needs Postgres; no unit test) and event-metadata dropping of
`arguments`/`message` under `capture_io=False` (the existing capture-io test asserts span inputs,
trace input and answer only).

---

## 6. Commands and configuration

All settings use the `AGENTOPS_` prefix (`app/core/config.py:15, 86-96`).

| Env var | Default | Effect |
|---|---|---|
| `AGENTOPS_TELEMETRY_LOG` | `true` | `LogSink` on/off |
| `AGENTOPS_TELEMETRY_CAPTURE_IO` | `true` | include question, answer, tool arguments in traces |
| `AGENTOPS_TELEMETRY_TIMEOUT_S` | `10.0` (0 < x ≤ 60) | per-export timeout and default `flush` bound |
| `AGENTOPS_LANGFUSE_PUBLIC_KEY` + `AGENTOPS_LANGFUSE_SECRET_KEY` | unset | **both** set → Langfuse on |
| `AGENTOPS_LANGFUSE_HOST` | `https://cloud.langfuse.com` | self-hosted or regional host |
| `AGENTOPS_LANGSMITH_API_KEY` | unset | set → LangSmith on |
| `AGENTOPS_LANGSMITH_ENDPOINT` | `https://api.smith.langchain.com` | |
| `AGENTOPS_LANGSMITH_PROJECT` | `agentops-ai` | sent as `session_name` |
| `AGENTOPS_LANGSMITH_SCOPE` | `eval` | `eval` = eval runs only; `all` = every finished run |

```bash
uv run pytest -q -W error tests/unit tests/security          # 244 passed
uv run pytest -q tests/unit/test_observability.py -v          # 13 passed

# Langfuse (untested live): every run, API and CLI
export AGENTOPS_LANGFUSE_PUBLIC_KEY=pk-lf-... AGENTOPS_LANGFUSE_SECRET_KEY=sk-lf-...
# LangSmith (untested live): only eval runs unless SCOPE=all
export AGENTOPS_LANGSMITH_API_KEY=lsv2_... AGENTOPS_LANGSMITH_SCOPE=all
uv run agentops run "What is the RPO in v2?" --mode graph     # trace id = printed execution=...
```
Note: the CLI calls `configure_logging("WARNING")` (`cli.py:33`), so the `LogSink`'s INFO `trace`
line is not printed by CLI commands; hosted sinks still receive CLI runs (flushed on exit).

---

## 7. What this milestone taught

1. **An audit ledger is most of a tracing system.** Because every model/tool call already went
   through the harness with latency, tokens and ids, observability was a projection (one function)
   plus two serializers, not an instrumentation pass over every node.
2. **Idempotency matters for telemetry too.** Approvals mean the same execution is exported twice;
   deterministic ids turn that into an update. The vendor's own de-duplication (Langfuse envelope
   ids) has to be worked *with*, not against.
3. **Observability must fail open for the app and closed for data.** Exporters can time out, error
   or back up without affecting a run; data leaves only as redacted summaries and ids.
4. **Routing is a privacy control.** "Which runs go to which vendor" (`accepts`) is where the §10
   eval-subset requirement lives, not in the evaluation code.

---

## 8. Known limitations and open findings (Recorder review of the working tree)

Not fixed in this milestone:

1. **Not verified against live vendors.** Payload shapes follow the vendors' documented APIs and
   are asserted only against mocks. Field-level acceptance (e.g. Langfuse `release`/`version`,
   LangSmith `dotted_order` format, `extra.metadata`) is unconfirmed.
2. ~~`keyword_search` span placement wrong for hybrid search~~ — **fixed, see §10 item 1.**
   Still approximate: the embed span is pinned to the tool span start, which includes any retry
   attempts and fixed overhead.
3. **Reconstructed, not measured, start times.** Generation/tool spans start at `at - latency_ms`
   (clamped to the step start); step spans end when the next step starts, so a step span includes
   routing time between nodes. No live spans during a run.
4. ~~Resumed runs export without the question~~ — **fixed, see §10 item 2.**
5. **`capture_io=False` still exports references.** After the §10 item 3 fix it drops the question,
   the answer, tool-span inputs, and event `arguments` / `message` data and free-text messages.
   **Documented choice:** citation filenames (trace output) and the `document_search` summary's
   `filenames`, `document_ids` and `chunk_ids` are still exported — they are references to stored
   documents, not their content. A deployment where filenames themselves are sensitive must change
   `DocumentSearch.ledger_output` and the trace `citations`.
6. **The `*token*` redaction rule is key-based.** `app.core.logging.redact` replaces any key matching
   `token` with `[REDACTED]` (checked: `input_tokens`, `output_tokens`, `max_output_tokens` all
   redact). The JSON log formatter applies it to every log line's extras, so token counts cannot be
   logged under those names. Today the `LogSink` line does not include token counts, so nothing is
   lost there yet; trace-level metadata is not passed through `redact`, so Langfuse/LangSmith do
   receive `input_tokens`/`output_tokens`. Event metadata is redacted, so any future ledger `data`
   key containing "token" would show as `[REDACTED]` in hosted traces.
7. **Dropped or lost traces are possible.** Backlog > 100 → dropped with a warning; a crash before
   `flush` loses pending exports; `flush` waits at most `telemetry_timeout_s` (10 s) at shutdown. No
   retry, no persistent outbox.
8. **Shared HTTP client.** Telemetry uses the same `httpx.AsyncClient` (20 connections) as the
   Ollama provider; a slow vendor holding connections could in principle compete with model calls.
   The 10 s timeout bounds it.
9. **Observation cap truncates silently.** Only the first 500 ledger events become observations;
   nothing in the trace marks it as truncated (a step span open at the cut still ends at the last
   ledger event).
10. **No trace if the save fails or on `ConflictError`.** `_trace` runs after `executions.save`;
    if the save raises (DB down) no trace is sent.
11. **Config not documented in `.env.example` / README** at the time of this review. The ten new
    settings exist only in `config.py` and this journal; the main agent is adding the entries.
12. **No scores flow yet.** `Telemetry.score` gets its caller in M8 (the evaluation runner);
    `source="eval"` is likewise only set by tests today.
13. Not in scope here: `GET /api/v1/metrics`, dashboards and alerting (§14, Day 4).

---

## 9. What I'd do differently at 100x scale

- **Outbox instead of in-memory tasks:** write the trace (or just the execution id) to a table/queue
  in the same transaction as the execution save; a separate exporter worker builds and ships traces
  with retries and backoff. No lost traces on crash, no competition with request handling.
- **OpenTelemetry as the internal format** (OTLP exporter to Langfuse's OTel endpoint, or any
  collector), keeping `build_trace` as the ledger→span projection; sampling and routing in the
  collector.
- **Live spans for long runs** via the existing ledger listeners (export each event as it is
  recorded), keeping the post-run build as the reconciliation pass.
- **Separate HTTP client/pool for telemetry**, with its own limits.
- **Sampling policy** (always keep errors, cancelled and slow runs; sample successes) rather than
  all-or-nothing per vendor.
- **Contract tests against real vendors** in a nightly job with a sandbox project, to catch schema
  drift that mocks cannot.

---

## 10. Fixed after Recorder review

The main agent fixed four findings from the first Recorder pass (§8). Each one was checked in the
working tree by the Recorder; DB-free suite after the fixes: **244 passed** (`uv run pytest -q -W
error tests/unit tests/security`, 2.73 s).

| # | Finding | Fix (confirmed in code) | Test |
|---|---|---|---|
| 1 | Retrieval sub-spans placed as if vector and keyword search ran concurrently; `HybridRetriever.search` runs them one after the other | `_retrieval_spans` (`app/observability/telemetry.py`) now keeps a running `begin`: `embed_query` starts at the tool span's start, `vector_search` starts at embed end, `keyword_search` starts at vector end (`begin = end` after each search). Docstring: "HybridRetriever.search runs them in sequence: embed, then vector, then keyword." | `test_trace_mirrors_the_graph_with_nested_observations` asserts `embed.end == vector.start and vector.end == keyword.start` |
| 2 | Resumed runs exported with `input=None`; LangSmith (terminal-only) would get `inputs: {}` for an eval run that paused | `AgentService.decide` (`app/agents/service.py`) calls `self.executions.get(tenant_id, ctx.execution_id)` after `restore_context`, then `_execute(..., question=execution.question)` ("so the finished trace still carries its input"). Cost: one extra tenant-scoped SELECT per decision. | None at unit level (needs Postgres); the integration approval tests exercise the path but do not assert the trace input |
| 3 | `capture_io=False` still exported approval-event `arguments` and event `message` text | In `build_trace`, event observations filter `e.data` with `if capture_io or k not in ("arguments", "message")`, and `status_message = e.error_code or (e.message if capture_io else None)`. Comment: "Messages can echo user/model text and approval events carry arguments: both are content". Citation filenames and the search `filenames` summary are **kept** on purpose: they are references, not content (§8 item 5). | Not asserted directly; `test_capture_io_off_and_redaction` covers trace input, answer and span inputs |
| 4 | `source` surviving `snapshot()`/`restore()` was untested | New test | `test_source_survives_a_checkpoint_round_trip`: `ExecutionContext.restore(ctx.snapshot()).source == "eval"` |

Still open (unchanged, §8): `_trace` skipped on `ConflictError` and when `save` raises; the CLI's
WARNING log level hides the INFO `trace` line; the `_MAX_PENDING` drop path and the cancelled/failed
trace paths are untested; telemetry shares the services' `httpx` pool with Ollama; the 500-event
observation cap is not marked on the trace; the latent key-based `*token*` redaction in logs and event
metadata. `.env.example`/README entries are being added by the main agent; `Telemetry.score` gets its
caller in M8.
