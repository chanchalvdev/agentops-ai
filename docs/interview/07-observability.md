# Interview Prep 07 — Observability: Traces from the Ledger, Langfuse, Optional LangSmith (Day 3, M7)

Every answer is grounded in the uncommitted working tree on `day-3-workspace-observability-eval`
(`app/observability/`, hooks in `app/agents/service.py`, `app/services.py`). Format: direct answer,
then the reasoning, then a code pointer. Companion: `docs/journal/07-observability.md`.
Caveat that applies throughout: the Langfuse and LangSmith adapters were verified with
`httpx.MockTransport` only — no live vendor project was used.

---

## Basic

### Q1. What does the observability layer do, in one sentence?
**Answer:** After every execution, it turns the execution ledger and result into a vendor-neutral
`TraceRecord` and hands it, in the background, to the configured sinks: a local log sink by default,
Langfuse if its keys are set, LangSmith if its key is set.
Pointer: `app/observability/telemetry.py` (`build_trace`, `Telemetry`), `app/services.py:build_telemetry`.

### Q2. What are `Observation`, `TraceRecord`, `Score` and `TraceSink`?
**Answer:** `Observation` is one node in the trace tree (`span`, `generation` or `event`, with
start/end, parent, level, metadata, I/O, and model/tokens for generations). `TraceRecord` is one
execution (id = execution id, source, terminal flag, input/output, metadata, tags, observations).
`Score` is an evaluation metric for a trace. `TraceSink` is the protocol every backend implements:
`accepts`, `export`, `score`.
Pointer: `app/observability/interfaces.py`.

### Q3. Which backends exist and how is each enabled?
**Answer:** `LogSink` (on unless `AGENTOPS_TELEMETRY_LOG=false`), `LangfuseSink` (both
`AGENTOPS_LANGFUSE_PUBLIC_KEY` and `AGENTOPS_LANGFUSE_SECRET_KEY` set; host via
`AGENTOPS_LANGFUSE_HOST`), `LangSmithSink` (`AGENTOPS_LANGSMITH_API_KEY` set; endpoint, project and
scope configurable). With no keys the app runs with just the log sink; with the log sink off too,
`Telemetry` has no sinks and `submit` returns immediately.
Pointer: `app/core/config.py:86-96`; `app/services.py:140-163`;
`tests/unit/test_observability.py::test_sinks_follow_configuration`.

### Q4. What is the trace correlation id?
**Answer:** The execution id. Langfuse trace id = `execution_id.hex`, LangSmith root run id =
`str(execution_id)`, and the same id is returned by the API, printed by the CLI and stored on every
ledger row. The HTTP `request_id` goes into trace metadata.
Pointer: `telemetry.py:160, 175`; `langfuse_adapter.py:62`; `langsmith_adapter.py:36`.

### Q5. What becomes a span, a generation and an event?
**Answer:** Each ledger `step` (graph node or loop turn) → a top-level span. Each non-rejected
`model_call` → a generation under the current step. Each non-rejected `tool_call` → a `tool:<name>`
span under the step. Everything else — rejections, policy stops, approvals, corrections, the final
decision — → an `event` named `<kind>:<name>`.
Pointer: `telemetry.py:79-148`.

### Q6. Where do retrieved document and chunk ids come from?
**Answer:** `DocumentSearch.ledger_output` returns `chunk_ids`, de-duplicated `document_ids`,
`filenames` and `timings_ms`; the harness stores it as `data["result"]` on the `tool_call` ledger
event; `build_trace` puts it (minus `timings_ms`) into the tool span's output. Cited chunk ids are also
in trace metadata as `cited_chunk_ids`.
Pointer: `app/tools/document_search.py:60-67`; `app/agents/harness.py:306`; `telemetry.py:117-129, 191`.

### Q7. How are embedding calls traced?
**Answer:** As child spans of `tool:document_search`: `embed_query`, `vector_search`,
`keyword_search`, built from the retriever's diagnostics timings that `DocumentSearch` now returns in
`timings_ms`. The embedder itself is not instrumented.
Pointer: `telemetry.py:39-60, 132-133`; `document_search.py:82-87`.

### Q8. What is `source` on the execution context?
**Answer:** The origin of the run: `"api"` by default, `"cli"` set by `agentops run`, `"eval"` for
evaluation runs (set by the M8 runner; today only by a test). It is saved in checkpoints and
restored on resume, becomes the trace's `source` and a tag, and drives LangSmith routing.
Pointer: `app/core/execution_context.py:51, 98, 126`; `app/cli.py:137`.

### Q9. Which prompt and workflow versions are recorded?
**Answer:** Graph runs: `nodes.PROMPT_VERSION = "agent-graph-v2"`, `graph.WORKFLOW_VERSION =
"agent-graph-workflow-v2"`. Loop runs: `custom_loop.PROMPT_VERSION = "agent-loop-v2"`,
`WORKFLOW_VERSION = "agent-loop-workflow-v1"`. They are in trace metadata; Langfuse also gets them as
the trace's `release` and `version`.
Pointer: `telemetry.py:155-158`; `langfuse_adapter.py:73-74`.

### Q10. What does the log sink write?
**Answer:** One INFO line `trace` per execution with `trace_id`, `trace_name`, `source`,
`duration_ms`, counts of spans/generations/events, `status`, `request_id`, `prompt_version`; one
`score` line per score.
Pointer: `telemetry.py:198-226`; `test_log_sink_writes_one_line`.

---

## Logical

### Q11. Why derive the trace from the ledger instead of putting span code in each node?
**Answer:** Three reasons from the module docstring: the trace and the persisted audit trail cannot
disagree (they are the same data); nodes stay free of vendor code; and since export happens after
the run, no exporter can slow down or break it. The harness already recorded latency, tokens, ids
and status for every call, so a projection was enough.
Pointer: `telemetry.py:1-10`.

### Q12. What did that choice cost?
**Answer:** No live spans while a run is in progress — a hosted trace appears only after the run
finishes (the SSE stream covers live progress). Start times are reconstructed as `at - latency_ms`
rather than measured. A process crash mid-run produces no trace, although ledger rows written so far
remain in the database.
Pointer: `telemetry.py:96-98`; `app/agents/service.py:182-184`.

### Q13. Why are observation ids `uuid5(execution_id, seq)`?
**Answer:** So building the trace twice gives the same ids. A run paused for approval is exported
when it pauses and again when it finishes; with stable ids Langfuse updates the existing
observations instead of creating duplicates.
Pointer: `telemetry.py:70-75`; `test_trace_ids_are_stable_so_reexport_upserts`.

### Q14. Then why is the Langfuse envelope id random?
**Answer:** Langfuse de-duplicates ingestion events on the envelope id. A stable envelope id would
make the second send look like a duplicate and be dropped. So the body id is the stable object id
(update semantics) and the envelope id is fresh per send.
Pointer: `langfuse_adapter.py:27-35`; `test_langfuse_ingestion_batch` asserts unique envelope ids.

### Q15. Why can't a failing exporter break a run?
**Answer:** `submit` builds the trace inside `try` (logs and returns on a bug) and only schedules
tasks; it never awaits the network. Each export runs in `_guarded` under `asyncio.timeout` and any
exception becomes one warning log line. `AgentService._trace` is called after the result is saved.
The test uses a raising sink and a hanging sink alongside a good one; `submit` returns immediately
and the good sink still receives the trace.
Pointer: `telemetry.py:241-274`; `test_exporter_failures_and_timeouts_never_reach_the_caller`.

### Q16. Why does LangSmith only get `eval` runs by default?
**Answer:** §10 asks to compare the two systems on a small evaluation subset without sending all
production data to both. Langfuse is primary and gets everything; `LangSmithSink.accepts` requires
`source == "eval"` unless `scope="all"`, and `score` drops non-eval feedback the same way.
Pointer: `langsmith_adapter.py:104-120`; `test_langsmith_receives_only_finished_eval_runs_by_default`.

### Q17. Why does LangSmith only get terminal runs?
**Answer:** LangSmith gets a POST per run id; sending the paused trace and then the finished one
would post the same run ids twice. Waiting for `terminal` (status ≠ `awaiting_approval`) means each
run id is posted exactly once. Langfuse does not need this because its ingestion upserts.
Pointer: `langsmith_adapter.py:107-108`; `interfaces.py:43`.

### Q18. Why is routing in the sink rather than in `Telemetry`?
**Answer:** The policy belongs to the backend: "LangSmith = eval + terminal" is a property of how we
use LangSmith. `Telemetry.submit` asks each sink `accepts(trace)`; `Telemetry.score` sends to every
sink and each decides. Adding a sink does not touch the dispatcher.
Pointer: `telemetry.py:252-258`; `test_routing_and_scores_fan_out`.

### Q19. Why does `_trace` run after the save, including on cancellation?
**Answer:** The persisted row is the source of truth; the trace should describe what was saved.
On `CancelledError`, `_execute` records the `final/error execution_cancelled` event, saves under
`asyncio.shield`, then calls `_trace`, then re-raises — so a cancelled run still produces a trace
with its cancellation event.
Pointer: `app/agents/service.py:157-165, 174-180`.

### Q20. Why does `source` live in the checkpoint snapshot?
**Answer:** An approval resume rebuilds the context with `restore_context`. Without `source` in the
snapshot, a resumed eval run would come back as `"api"` and LangSmith would never receive its
terminal trace. `restore` defaults to `"api"` for checkpoints written before the field existed.
Pointer: `execution_context.py:98, 126`;
`tests/unit/test_observability.py::test_source_survives_a_checkpoint_round_trip`.

---

## Advanced

### Q21. How are child start times reconstructed, and what guards exist?
**Answer:** A model/tool event is recorded when the call ends, at `at`, with `latency_ms` measured by
`perf_counter`. Start = `at - latency_ms`, then `max(start, step.start)` so clock rounding never
puts a child before its parent. Step spans end at the next step's `at` (or the last ledger event).
The stability test also asserts no child starts before its parent.
Pointer: `telemetry.py:80-98, 149-150`.

### Q22. Is the retrieval sub-span placement accurate?
**Answer:** The order is now right; the absolute start is approximate. `HybridRetriever.search`
embeds the query, then runs vector search, then keyword search, one after the other in one session.
`_retrieval_spans` mirrors that with a running `begin`: `embed_query` at the tool span's start,
`vector_search` from embed end, `keyword_search` from vector end. The first version started both
searches at embed end (as if parallel); the Recorder review caught it and the test now asserts
`embed.end == vector.start and vector.end == keyword.start`. Remaining approximation: the tool span's
start includes retry attempts and fixed overhead, so the whole chain may sit slightly early.
Pointer: `telemetry.py:_retrieval_spans`; `app/retrieval/hybrid_search.py:88-100`;
`test_trace_mirrors_the_graph_with_nested_observations`.

### Q23. Walk through what Langfuse receives for one graph run.
**Answer:** One `POST {host}/api/public/ingestion`, Basic auth `(public_key, secret)`, body
`{"batch": [...]}`: a `trace-create` (id = execution hex, name `agent.graph`, input/output,
metadata, tags, `version` = workflow version, `release` = prompt version) followed by one
`span-create` / `generation-create` / `event-create` per observation with `traceId`,
`parentObservationId`, times, `level`, `statusMessage`; generations add `model` and
`usage {input, output, total, unit: "TOKENS"}`. `None` fields are omitted.
Pointer: `langfuse_adapter.py:38-77`; `test_langfuse_ingestion_batch`.

### Q24. How does the Langfuse adapter handle partial failure?
**Answer:** `raise_for_status()` covers 4xx/5xx. Langfuse answers a batch with 207 and an `errors`
list when some events are rejected; the adapter raises `RuntimeError("langfuse rejected N event(s)…")`
so `_guarded` logs it instead of treating a half-ingested trace as success.
Pointer: `langfuse_adapter.py:106-111`; `test_langfuse_partial_failure_is_reported`.

### Q25. How does the LangSmith adapter encode the tree?
**Answer:** A root `chain` run (id = execution id) and one child per observation with
`parent_run_id` (the observation's parent, or the root). `dotted_order` = parent's dotted order + `.`
+ start time `%Y%m%dT%H%M%S%fZ` + run id, which is how LangSmith orders and nests runs in a batch.
`run_type`: generations → `llm`, `tool:*` → `tool`, else `chain`. Generations carry
`usage_metadata` and `ls_model_name`/`ls_provider`. The root's `error` is set unless the status is
`answered` or `insufficient_evidence`.
Pointer: `langsmith_adapter.py:30-86`; `test_to_runs_marks_failed_runs`.

### Q26. What does redaction cover, and where does it fall short?
**Answer:** `app.core.logging.redact` (keys matching api key/authorization/password/secret/token/
cookie; values like `Bearer …`, `sk-…`, DSN passwords) is applied to tool arguments, tool result
summaries, tool metadata and event metadata. Passage text never enters the ledger summary.
`capture_io=False` drops the question, the answer, tool-span inputs, and — since the review fix —
event `arguments` / `message` data keys and the free-text `message` fallback in `status_message`
(approval events carry arguments; messages can echo user or model text). Kept on purpose: citation
filenames and the search summary's `filenames`/ids, which are references, not content. Trace-level
metadata is not passed through `redact` (it holds counters, versions and ids). Gap: the event-metadata
dropping is not directly asserted by a test.
Pointer: `app/core/logging.py:14-39`; `telemetry.py` (`build_trace`, event branch);
`test_capture_io_off_and_redaction`.

### Q27. What is the side effect of key-based `token` redaction?
**Answer:** Any key containing "token" becomes `[REDACTED]`: `input_tokens`, `output_tokens`,
`max_output_tokens` all do (checked in a REPL). The JSON log formatter redacts every log line's
extras, so token counts cannot be logged under those names. The `LogSink` line does not include
tokens today, so nothing is lost there yet; hosted traces get token counts because trace metadata
and generation usage are not redacted. Any future event `data` key with "token" would be hidden in
hosted traces.
Pointer: `app/core/logging.py:14, 53`; `telemetry.py:185-186`.

### Q28. What happens under backpressure?
**Answer:** `_spawn` keeps a set of pending export tasks. At 100 (`_MAX_PENDING`) it closes the new
coroutine (so it is never awaited and no warning about an un-awaited coroutine is raised) and logs
`telemetry backlog full; dropping export`. Memory is bounded; traces are lost. Not covered by a test.
Pointer: `telemetry.py:29, 260-267`.

---

## Scenario

### Q29. "Langfuse is down for an hour. What do users see?"
**Answer:** Nothing different: each export fails or times out (10 s) inside `_guarded` and logs a
warning; runs complete normally. If more than 100 exports are pending, new ones are dropped with a
warning. Traces from that hour are lost — there is no retry or outbox. The ledger in Postgres still
has every event (`GET /executions/{id}/trace`).
Pointer: `telemetry.py:260-274`.

### Q30. "A run paused for approval and was approved a day later. What does Langfuse show?"
**Answer:** One trace (id = execution id). The first export at the pause created it; the resumed
run's export rebuilds the whole tree from the restored ledger with the same uuid5 ids, so existing
observations update and the new ones (approval, tool call, answer) are added. The resumed export
carries the question: `decide` loads the execution row (`executions.get`) and passes
`question=execution.question` to `_execute` — added after the Recorder review, because before it the
resumed trace had `input=None` (Langfuse kept the earlier input, but LangSmith, which only gets the
terminal trace, would have seen `inputs: {}`). Trace start/end span the full day, including the
human wait.
Pointer: `app/agents/service.py:decide`; `langfuse_adapter.py:34`.

### Q31. "Security says customer questions must not leave our network, but we want Langfuse Cloud."
**Answer:** Set `AGENTOPS_TELEMETRY_CAPTURE_IO=false`: no question, answer, tool-span arguments,
approval-event arguments or free-text event messages; structure, timings, tokens, model ids,
versions, error codes and chunk/document ids remain. Be explicit about what still leaves: citation
filenames and the search summary's `filenames` — a documented choice (references, not content). If
filenames are sensitive too, change `DocumentSearch.ledger_output` and the trace `citations`, or
point `AGENTOPS_LANGFUSE_HOST` at a self-hosted Langfuse.
Pointer: `config.py:87`; `telemetry.py:build_trace`.

### Q32. "We want to compare Langfuse and LangSmith on our eval set."
**Answer:** Set the LangSmith key and leave `AGENTOPS_LANGSMITH_SCOPE=eval`. Runs with `ctx.source =
"eval"` go to both; production runs go only to Langfuse. Scores pushed with `Telemetry.score(...,
"eval")` reach Langfuse as `score-create` and LangSmith as `/feedback`. The piece that sets
`source="eval"` and pushes scores is the M8 evaluation runner; today the path is proven by
`test_agent_service_submits_every_finished_run` with a hand-set `source`.
Pointer: `langsmith_adapter.py:104-131`; `telemetry.py:256-258`.

### Q33. "The CLI run printed no `trace` log line. Is telemetry broken?"
**Answer:** No. The CLI calls `configure_logging("WARNING")`, which hides the `LogSink`'s INFO line.
Hosted sinks still get CLI runs (source `cli`), and `Services.aclose()` flushes pending exports
before the CLI exits.
Pointer: `app/cli.py:32-39`; `app/services.py:111-112`.

### Q34. "An early trace showed keyword search running in parallel with vector search. Did it?"
**Answer:** No — that was a reconstruction artifact in the first version: the retriever runs vector
then keyword in one session, but `_retrieval_spans` started both after the embed. The Recorder
review caught it by reading `HybridRetriever.search`; the fix chains the spans (keyword starts at
vector end) and the test pins it. Lesson: a reconstructed trace encodes assumptions about the code's
concurrency, so those assumptions need tests too. Traces exported before the fix keep the old
placement.
Pointer: `telemetry.py:_retrieval_spans`; `hybrid_search.py:93-100`.

---

## Trade-off

### Q35. Ledger-derived traces vs live instrumentation (OTel spans / LangChain callbacks)?
**Answer:** Ledger-derived: one source of truth, vendor-free nodes, zero risk to the run, trivially
testable, deterministic ids for re-export. Cost: no in-flight visibility, reconstructed start times,
nothing exported if the process dies mid-run. Live instrumentation: real timings and in-progress
traces, standard tooling; cost: span code (or callbacks) everywhere, the trace can drift from the
audit ledger, and exporter problems sit on the hot path. With SSE already covering live progress,
the ledger approach was the better fit for this project.
Pointer: `telemetry.py:1-10`.

### Q36. Vendor SDKs vs plain HTTP?
**Answer:** SDKs track API changes, handle batching and retries, and are what vendors support. Plain
HTTP gives one batched POST per finished execution under our own timeout and redaction, no background
threads (the Langfuse adapter docstring cites the v3 SDK's OpenTelemetry pipeline), and an
`httpx.MockTransport`-testable client shared with the app. Cost: we own the wire format, and it was
validated only against docs and mocks.
Pointer: `langfuse_adapter.py:1-9`; `langsmith_adapter.py:1-9`.

### Q37. Fire-and-forget in-process tasks vs a durable outbox?
**Answer:** In-process tasks: no new infrastructure, export never delays a run, bounded memory.
Cost: traces lost on crash, when the backlog is full, or if `flush` times out at shutdown; no
retries. An outbox (row written with the execution, exported by a worker with retries) gives
at-least-once delivery at the cost of a table, a worker and more moving parts — the right answer at
scale, overkill for a single-box project.
Pointer: `telemetry.py:260-278`.

### Q38. Send everything to both vendors vs route by source?
**Answer:** Everything to both: easiest comparison, but production data at two vendors, double cost,
double compliance surface. Routing by `source` (LangSmith = eval only): the comparison happens on
the same eval subset, production stays with the primary. `scope="all"` exists for debugging.
Pointer: `config.py:95-96`; `langsmith_adapter.py:104-108`.

---

## Justification

### Q39. "Reconstructing timings — isn't that just making numbers up?"
**Answer:** No: durations are measured (`perf_counter` latency in the ledger); only the start is
derived from end time minus duration, and clamped to the parent. The one placement that is inferred
rather than measured is the order of retrieval sub-spans; it follows the retriever's actual
sequential order (embed → vector → keyword) and is pinned by a test after the review found the first
version wrong (Q22).
Pointer: `telemetry.py` (`build_trace`, `_retrieval_spans`).

### Q40. "Why a log sink instead of a pure no-op?"
**Answer:** §10 allows "a no-op or local logging implementation". One structured line per run gives
a developer the trace id, status, duration and span counts in the normal log stream without any
vendor; it costs almost nothing. The pure no-op is available too: `AGENTOPS_TELEMETRY_LOG=false` and
no keys → no sinks, `submit` returns at once.
Pointer: `telemetry.py:198-226, 245-246`.

### Q41. "Why not put the HTTP request id on the trace instead of the execution id?"
**Answer:** An execution can span several requests (start, then approve/reject later), and runs also
come from the CLI and, soon, the eval runner. The execution id is the one id that exists for every
run and stays the same across a resume; the request id is kept in metadata for log correlation.
Pointer: `telemetry.py:160, 175`; `service.py:127-141`.

### Q42. "You shipped LangSmith support without a live test. Why ship it?"
**Answer:** It is off unless a key is set, it can only receive eval runs by default, and every
failure is contained by `_guarded`. The payload shape, auth header, routing rules and tree encoding
are unit-tested against mocks. The journal and the module docstring state plainly that it was not
checked against a real project; the first real eval run in M8 with a key set is the live check.
Pointer: `langsmith_adapter.py:1-9`; `tests/unit/test_observability.py`.

---

## System design

### Q43. Walk through the trace of a streamed graph run from request to Langfuse.
**Answer:** `POST /agent/run/stream` → `new_context` (source `api`, request id from middleware) →
`AgentService.run` → graph nodes; the harness records `step`, `model_call`, `tool_call`
(`document_search` adds `data.result` with chunk/document ids and `timings_ms`) and `final` into
`ctx.ledger`, each also streamed over SSE → `_execute` saves the execution row → `_trace` →
`Telemetry.submit` builds the `TraceRecord` (step spans, generations, `tool:document_search` with
`embed_query`/`vector_search`/`keyword_search` children, events) → `LogSink` and `LangfuseSink` accept
(LangSmith rejects: source ≠ eval) → background tasks POST the batch with a 10 s timeout → at
shutdown `Services.aclose()` flushes.
Pointer: `app/agents/service.py`; `app/observability/telemetry.py`; `app/services.py`.

### Q44. How would you redesign observability for 100x traffic?
**Answer:** Write an outbox row (execution id) in the same transaction as the execution save; a
separate exporter worker builds traces from the persisted ledger and ships them with retries,
backoff and per-vendor rate limits; use OpenTelemetry/OTLP as the transport so routing and sampling
live in a collector (keep errors, cancellations and slow runs; sample successes); give telemetry its
own HTTP pool; optionally stream events as they are recorded (ledger listeners) for live spans, with
the post-run build as reconciliation; and run nightly contract tests against sandbox vendor projects.
Pointer: journal 07 §9.
