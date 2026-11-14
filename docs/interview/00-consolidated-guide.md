# Consolidated Interview Guide — AgentOps AI (Milestones 01–14)

The strongest questions from the fourteen per-milestone files, grouped by theme, with answers
cut to what you'd say out loud. Every number is a measurement from this project (Apple M4, 16 GB,
Ollama, `qwen3:8b` + `qwen3:1.7b` + `nomic-embed-text`, temperature 0). Each answer links to the
milestone file with the full version, code pointers and follow-ups.

| # | File | Topic |
|---|---|---|
| 01 | [01-foundation-rag.md](01-foundation-rag.md) | Foundation, LLM layer, RAG |
| 02 | [02-harness-tools.md](02-harness-tools.md) | Harness, tools |
| 03 | [03-agent-loop.md](03-agent-loop.md) | Custom agent loop, ledger |
| 04 | [04-langgraph.md](04-langgraph.md) | LangGraph, checkpoints, approval |
| 05 | [05-agent-api.md](05-agent-api.md) | Agent API, SSE, approvals API |
| 06 | [06-workspace-tools.md](06-workspace-tools.md) | Sandboxed workspace |
| 07 | [07-observability.md](07-observability.md) | Traces, Langfuse/LangSmith |
| 08 | [08-evaluation.md](08-evaluation.md) | Evaluation framework |
| 09 | [09-optimization.md](09-optimization.md) | Cost/latency optimization |
| 10 | [10-drift-monitoring.md](10-drift-monitoring.md) | Drift monitoring |
| 11 | [11-hardening.md](11-hardening.md) | Rate limits, Docker, CI |
| 12 | [12-ui-api.md](12-ui-api.md) | API additions for the UI |
| 13 | [13-web-ui.md](13-web-ui.md) | React web UI |
| 14 | [14-demo-loop-vs-graph.md](14-demo-loop-vs-graph.md) | Demo, loop vs graph |

## Numbers to remember

| What | Value | Source |
|---|---|---|
| Golden set | 38 cases: 11 answerable, 5 multi-doc, 5 unanswerable, 5 tool use, 7 adversarial, 5 workspace | 08 |
| Day 3 graph baseline | 33/38 (87%); safety evaluators 100%; p50 28 s | 08 |
| Day 4 optimized vs baseline (back to back) | 36/38 vs 33/38; tokens/request −21%; p50 43.1 → 27.5 s; tool use 2/5 → 5/5 | 09 |
| Rejected candidates | query rewrite off: multi-doc 4/5 → 2/5; routing that skipped comparisons: 0/3 vs 3/3 | 09 |
| Drift demo | pass rate 0.868 → 0.816 and answer coverage 0.971 → 0.882 measured; recall@k and hit rate stayed 1.0 | 10 |
| Loop vs graph (same 33) | loop 1/33 strict, 24/33 lenient; graph 29/33 and 32/33; loop +44% tokens with 3.64 vs 4.76 calls | 14 |
| Same run, different day | p50 60.6 s vs 43.1 s, identical answers and tokens | 09 |
| Tests at the end of each day | 107 → 230 → 323 → 370 → 373 (`-W error`, incl. Postgres integration) | `docs/day-checklist.md` |
| Web bundle | 193 KB JS, 60 KB gzip; 2 runtime dependencies | 13 |

---

## 1. The harness (policy in code, not in the prompt)

### What is the harness?
The one piece of code every model call and tool call passes through. It enforces cancellation,
deadline, budgets, permissions and argument validation, and records every attempt in a ledger. The
model only *requests* actions. → [02 Q1](02-harness-tools.md)

### Why not tell the model "use at most 5 tools"?
A prompt is a suggestion; a limit is a guarantee. Models ignore instructions — especially when a
document tells them to. Every limit here is a counter checked before the call, with tests asserting
the tool never ran. → [02 Q36](02-harness-tools.md)

### Why is a model call counted before it's awaited?
So failures consume budget; otherwise a provider that always errors lets a loop retry forever. →
[02 Q11](02-harness-tools.md)

### Why do an unknown tool and a disallowed tool return the same error?
So a model steered by injected text can't probe which tools exist. The missing scope goes to logs,
not to the model. → [02 Q13](02-harness-tools.md)

### Why is the check order cancellation → deadline → budget → permission → arguments?
Absolute, cheap stops first; call-specific checks last. If time ran out, the rejection should say
so, not "invalid arguments". → [02 Q10](02-harness-tools.md)

---

## 2. The custom agent loop

### What is it?
A hand-written, bounded loop: the model returns a final answer or tool calls; allowed tools run
through the harness; results go back fenced; it stops on a validated answer or a limit. Built
before LangGraph to learn the mechanics and to be the baseline. → [03 Q1](03-agent-loop.md)

### Why is prose never parsed as an answer or instruction?
It's an injection path (the model may copy "call X" out of a document) and can't be validated. Prose
costs one correction. → [03 Q11](03-agent-loop.md), [03 Q37](03-agent-loop.md)

### What stops a model repeating the same search forever?
Calls are signed by name + canonical arguments; a repeat is answered "duplicate" without running and
costs a correction; corrections are bounded, then the run ends `budget_exhausted`. →
[03 Q13](03-agent-loop.md)

### How did the loop do on the golden set?
1/33 strict, 24/33 without `structured_output`, vs graph 29/33 and 32/33. In 31/33 runs qwen3:8b
answered in prose first and needed one or two corrections. It uses fewer calls (3.64 vs 4.76) but
44% more tokens, because each turn resends the history. → [14 Q5–Q8](14-demo-loop-vs-graph.md)

### Why keep the loop at all?
It forced framework-free shared pieces (harness, evidence store, result, ledger), it's the baseline,
and it fits open-ended multi-hop questions. For production it would need constrained `final_answer`
and a verify step. → [03 Q39](03-agent-loop.md), [14 Q14](14-demo-loop-vs-graph.md)

---

## 3. LangGraph workflow, checkpoints, approval

### What is the graph?
A fixed ten-node `StateGraph` — classify → plan → retrieve → tools ⇄ approval → generate →
validate → verify → revise → finalize — where code decides the path and the model fills in each node,
all through the harness. → [04 Q1](04-langgraph.md)

### Why did putting `citations` before `answer` in the schema matter?
With constrained decoding the model writes fields in schema order. Answer-first produced correct but
uncited answers 3/3; citations-first cited on the first try. Schema order is part of the prompt. →
[04 Q13](04-langgraph.md)

### Why does `verify_answer` use the 8B model?
qwen3:1.7b said "unsupported" to every answer, including verbatim ones, and turned correct answers
into refusals. A verifier that always says no is worse than none. → [04 Q15](04-langgraph.md)

### How does approval work?
The harness records a pending approval and raises; the graph `interrupt`s and the state is
checkpointed in Postgres. A decision resumes from the checkpoint; an approved call executes without a
new model call. → [04 Q7](04-langgraph.md)

### How do two concurrent approvals resume only once?
A conditional `UPDATE approvals … WHERE status='pending' RETURNING *`. Postgres serialises; one wins,
the rest get 409. Tested: four parallel approves → `[200, 409, 409, 409]`, one write. (Before this, a
probe ran a write twice.) → [05 Q17](05-agent-api.md), [04 Q23](04-langgraph.md)

### Loop or graph — which and when?
Graph when steps have an order, a run must pause for a human, each step needs its own model, or
answers must be verified — every production path here. Loop for unknown tool sequences. Measured:
graph 29–32/33 vs loop 24/33 lenient. → [04 Q35](04-langgraph.md),
[14 Q18](14-demo-loop-vs-graph.md)

---

## 4. RAG

### What does a grounded answer request do?
Authenticate to a tenant → hybrid retrieval (vector + keyword, RRF) → pack evidence under a budget →
JSON-schema answer with citations → validate citations, one correction → map `[S#]` to chunks. →
[01 Q1](01-foundation-rag.md)

### What is RRF and why use it?
Score = Σ 1/(k + rank), k = 60. Ranks only, so cosine and `ts_rank_cd` never need a common scale;
agreement between retrievers wins. → [01 Q2](01-foundation-rag.md)

### Tell me about the keyword search bug.
`websearch_to_tsquery` ANDs every term, so natural questions matched 0 chunks — seen live as
`keyword_candidates: 0`. Fix: rewrite `&` to `|`; ranking still favours more matches. →
[01 Q10](01-foundation-rag.md)

### What if the model cites `[S42]` when only S1–S3 exist?
Validation rejects it, one correction lists the valid ids, then `failed_validation` with a safe
message. Later the graph made citations an enum of shown ids, so invalid ones can't be decoded. →
[01 Q17](01-foundation-rag.md), [06 Q25](06-workspace-tools.md)

### Why did answer coverage matter more than recall@k?
On a 5-document corpus top-5 returns half of it; recall and hit rate stayed 1.0 under a clearly
worse chunking, while coverage (did we retrieve the passage holding the answer?) fell 0.971 → 0.882.
→ [10 Q7](10-drift-monitoring.md)

### Why turn query rewrite back on?
Turning it off looked free on a retrieval probe, but live multi-document fell 4/5 → 2/5: "RPO"
doesn't retrieve "Availability and Recovery" without the rewrite. The probe itself had a bug (it
credited "15 minutes" from the wrong document). → [09 Q17–Q18](09-optimization.md)

---

## 5. Tools and the sandboxed workspace

### What can the agent do, and what needs a human?
Read, list and search freely; every write/edit needs approval; exec needs approval unless the
command is read-only. → [06 Q1](06-workspace-tools.md)

### Why `realpath` before the containment check?
A symlink inside the root can point outside; only the resolved path shows it. →
[06 Q11](06-workspace-tools.md)

### Why allowlist options, not just programs?
"Read-only" programs hide writes and escapes in options: `find -delete`, `find -exec`,
`git diff --output`, `git -C /`. → [06 Q14](06-workspace-tools.md)

### Why is `pytest --collect-only` treated as exec?
Collection imports `conftest.py` and test modules — it runs workspace code. A deliberate deviation
from the spec. → [06 Q15](06-workspace-tools.md)

### Why not a shell with a blocklist?
Blocklists are never complete and a shell adds pipes, redirection, substitution. No shell at all; in
the demo `ls | wc -l` → `invalid_request`. → [06 Q38](06-workspace-tools.md)

### How is an approved write not applied twice after a crash?
Write-ahead idempotency receipts keyed by (execution, call): the receipt is written before the file
is replaced; a replay returns the stored result only if the file's hash matches. →
[06 Q21](06-workspace-tools.md)

### Is it a real sandbox?
No: a path jail + allowlist + human approval, not an OS boundary. An approved `pytest` runs code as
the server. The container is non-root (uid 10001) but has no seccomp or per-command network
isolation. → [06 Q34](06-workspace-tools.md), [11 Q21](11-hardening.md)

---

## 6. Observability

### Why derive traces from the ledger?
The harness already recorded latency, tokens, ids and status for every call, so the trace and the
audit trail can't disagree, nodes stay vendor-free, and export after the run can't break it. →
[07 Q11](07-observability.md)

### Why deterministic observation ids?
A paused run is exported at the pause and again at the end; `uuid5(execution, seq)` makes the second
export an update, not duplicates. → [07 Q13](07-observability.md)

### Can a failing exporter break a run?
No: export is scheduled after the save, each sink runs under a timeout, errors become one log line.
Tested with a raising and a hanging sink. → [07 Q15](07-observability.md)

### What's the honest gap?
Langfuse and LangSmith were verified against mocked HTTP only; no live project. Also no in-flight
visibility (SSE covers live progress). → [07 Q35](07-observability.md)

---

## 7. Evaluation

### What's in the framework?
A 38-case versioned golden set, 11 deterministic evaluators, an advisory LLM judge, a runner through
the real service as an `eval` tenant, persisted runs, and a regression gate. → [08 Q1](08-evaluation.md)

### Why does a repaired output still fail `structured_output`?
The property measured is "valid on the first try"; a repair costs a call and could have failed. It's
the evaluator that dropped the loop to 1/33. → [08 Q16](08-evaluation.md),
[14 Q5](14-demo-loop-vs-graph.md)

### How does the gate decide a regression?
Different dataset, mode or filter = not comparable; any crashed case; pass rate floor; drops beyond
thresholds overall/category/evaluator; any drop in the zero-tolerance safety evaluators; p95 +50%;
any newly failing case. → [08 Q22](08-evaluation.md)

### What did the first eval run catch?
A bug in the eval harness, not the model: a `.env` fixture was seeded through the agent's own jail,
which refused it. The runner now writes fixtures as the operator. → [08 Q21](08-evaluation.md)

### Is the LLM judge trustworthy?
Measurably not as truth: 5/5 to a false "could not complete" report it couldn't check on disk; 0
groundedness to a correct answer it saw only in snippets; and it scored a degraded run higher
(0.998 vs 0.969) because refusals score well. Advisory only. → [08 Q25](08-evaluation.md),
[10 Q11](10-drift-monitoring.md)

### "CI is green, so quality didn't regress?"
No. CI can't run qwen3:8b; it gates the committed reports (format and thresholds), not fresh model
behaviour. A fresh live run, or replayed responses, is needed. → [08 Q26](08-evaluation.md),
[11 Q17](11-hardening.md)

---

## 8. Optimization

### What does cost mean with local models?
Dollars are $0; the real costs are tokens, model calls and latency on one shared machine.
Hosted-equivalent cost is shown only with configured reference prices, labelled hypothetical. →
[09 Q2](09-optimization.md)

### What decides that an optimization wins?
A >5% gain in p50, p95, tokens or calls **and** no quality breach under the same gate, including zero
newly failing cases. → [09 Q16](09-optimization.md)

### What won?
Rule routing (code skips the tool-selection call on plain lookups) plus a calculator nudge: 36/38 vs
33/38, −21% tokens, p50 −36%, tool use 2/5 → 5/5. Two other candidates were rejected by the gate. →
[09 Q6](09-optimization.md)

### Why was the routing fix needed?
Skipping `select_tools` on comparisons also skipped the `document_metadata` call it triggered — the
step that told the model which passage was v1 and which v2. `multi-availability`: 0/3 vs 3/3. A
removed call can carry hidden work. → [09 Q19–Q20](09-optimization.md)

### Did caching help?
Not measurably: each golden question runs once, so ~0 hits. The benefit for repeated questions is
covered by tests only, and the benchmark says so. → [09 Q26](09-optimization.md)

### Is the system 36% faster?
On those back-to-back runs, the median was. Latency moves with swap — the same baseline measured
60.6 s and 43.1 s on different days with identical answers. Temperature 0 makes quality
deterministic; tokens and calls are the stable signal. → [09 Q21](09-optimization.md),
[09 Q25](09-optimization.md)

---

## 9. Drift and monitoring

### What does the detector watch?
Quality (eval runs), retrieval (model-free retrieval eval), data (embedding/chunking mix, ingestion
failures) and operations (latency, errors, tokens per window). Every breach is `measured` or
`suspected`; no data is `insufficient_data`. → [10 Q2](10-drift-monitoring.md),
[10 Q9](10-drift-monitoring.md)

### What did the regression demo show?
Degraded chunking vs the Day 4 baseline: `drift`, exit 1 — pass rate and answer coverage dropped
(measured) while recall@k and hit rate stayed 1.0. → [10 Q5](10-drift-monitoring.md)

### The demo also flagged tokens/execution 751 → 3,516. Regression?
No — the previous window was a provider outage (83% failures), and failed runs spend few tokens. The
detector says what changed; a person says why. → [10 Q17](10-drift-monitoring.md)

### Gate and detector — why both?
The gate decides whether a change ships (per case, strict). The detector watches the system over time
(aggregate, with evidence labels). A one-case regression fails the gate and can pass drift. →
[10 Q26](10-drift-monitoring.md)

---

## 10. Security and hardening

### How is tenant isolation enforced?
Tenant from the API key, never from arguments; every query scoped in the store; chunks and documents
both carry `tenant_id` and both are checked. Cross-tenant reads are 404, not 403, so ids can't be
probed — including the Day 5 chunk endpoint. → [01 Q14](01-foundation-rag.md),
[01 Q35](01-foundation-rag.md), [12 Q4–Q5](12-ui-api.md)

### How is prompt injection handled?
Invisible characters stripped at ingestion; evidence fenced with unforgeable tags and declared
untrusted; constrained output; citations validated; and — the real control — the harness decides
which tools run, writes need a human, and the golden set has 7 adversarial cases (7/7 in the Day 3
baseline and in both accepted Day 4 runs; `unauthorized_tools` is 100% in every stored run, graph and loop, including the rejected profiles). → [01 Q23](01-foundation-rag.md), [03 Q19](03-agent-loop.md)

### How is rate limiting done?
Token buckets per (tenant, route class) — agent 30/min, upload 60, expensive 6, workspace 120 — with
429 and `Retry-After`. In-process and documented as such; Redis for replicas. →
[11 Q7](11-hardening.md), [11 Q22](11-hardening.md)

### Who can run evaluations?
Admin tenants only, fail closed in prod (empty list = nobody). On Day 5 operators were also given
every run, because CLI runs are recorded as `cli` and were invisible. → [11 Q4](11-hardening.md),
[12 Q7](12-ui-api.md)

### What production-shaped incidents did a laptop produce?
Port 11434 owned by another project's Ollama without the models (every call `provider_error`); sleep
killing long runs (`caffeinate -ims`); port 8000 taken (API on 8010); macOS killing Ollama under
memory pressure mid-approval (`provider_unavailable`). Each became a README entry. →
[11 Q15–Q16](11-hardening.md), [13 Q13](13-web-ui.md)

---

## 11. The web UI

### What is the UI's security model?
A pure client of the public API holding only the user's API key; every rule is the server's. Vite
proxies `/api` so no CORS change was needed. Risk accepted and documented: the key sits in
`localStorage`. → [13 Q1–Q4](13-web-ui.md), [13 Q19](13-web-ui.md)

### Why not `EventSource`?
It only does GET and can't set headers; the stream is a POST with an API key. So `fetch` plus a
36-line incremental parser, unit-tested for chunk boundaries and keep-alive comments. →
[13 Q6–Q7](13-web-ui.md)

### What did the approval diff catch?
The agent proposed an edit to a file that already had the function; the diff showed a duplicated
`return` line; rejected. No server check would have refused it. → [13 Q12](13-web-ui.md)

### What did driving the UI find?
Five bugs no test caught: a non-function effect cleanup that blanked the app, a StrictMode
double-mount leaking a "closed" message, a token total read from a Python-only `@property`, wrong
nested keys, and an operator run list filtered by tenant. Plus the demo script's one: upload returns
`{document, duplicate}`. → [13 Q14–Q16](13-web-ui.md), [14 Q10](14-demo-loop-vs-graph.md)

---

## 12. Cross-cutting: what would you say about the whole project?

### Summarize the project in a minute.
A local agentic RAG platform: hybrid retrieval with citations; a harness that enforces budgets and
permissions in code; a hand-written loop and a LangGraph workflow with verification, checkpoints and
human approval; a sandboxed workspace; traces from the ledger; a 38-case eval with a regression gate;
optimizations accepted only through that gate (36/38 vs 33/38, −21% tokens); drift monitoring that
caught a deliberate regression recall missed; rate limits, Docker, CI; and a React UI. 373 backend
tests plus 6 web unit tests.

### What's the most important lesson?
Live runs disagreeing with expectations found nearly every bug that mattered — keyword search with 0
candidates, a verifier that said no to everything, a probe crediting the wrong document, routing that
dropped hidden work, an operator list that real data emptied. And at temperature 0, a disagreement is
behaviour, not noise, so it's worth explaining before fixing.

### What would you do at 100x?
Durable job queue for indexing, evals and runs; gateway/Redis rate limits plus a global fair-share
queue for model work; roles and scopes with audit; a real sandbox (container/microVM) for exec; CI
replay of recorded model responses and a nightly live benchmark on fixed hardware; resumable streams;
generated API types and browser tests. → [11 Q28](11-hardening.md), journals §9

### What's still open?
Two golden cases fail in the graph (`multi-retention`, `ws-edit-approved`); the accepted optimized
profile isn't the server default; caches unmeasured live; per-process limits; no OS sandbox; CI
gates static reports; the demo has no assertions; no browser tests. →
[journal 14 §10](../journal/14-demo-loop-vs-graph.md)
