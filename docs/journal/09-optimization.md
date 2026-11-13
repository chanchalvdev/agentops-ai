# Journal 09 — Cost and Latency Optimization, Experiment Profiles, Baseline vs Optimized Benchmark (Day 4, M9)

Milestone: the first part of Day 4 in product.txt §18 — "cost/latency accounting, model routing
(small vs large local model), tool/embedding/semantic caching, baseline vs optimized benchmark" —
implementing §12 (cost and latency optimization) and the experiment parts of §14. Branch
`day-4-optimization-drift-hardening`, **uncommitted** working tree on top of `b497479` (the Day 3
M6-M8 work is still uncommitted in the same tree). Written by the Recorder agent from the code,
the stored reports, the run logs in `reports/` and test output; facts reported by the build
session and not visible in a file are marked as such.

Status (re-run by the Recorder):
- DB-free: `uv run pytest -q -W error tests/unit tests/security` → **295 passed in 3.67s**.
- Collected: `uv run pytest --collect-only -q` → **370/377 collected (7 deselected = live Ollama)**.
  The 75 DB tests were **skipped** in the Recorder's run: the Postgres on port 5435 now belongs to
  another project (`password authentication failed for user "agentops"`). The full 370 with
  `-W error` is the build session's result; 295 + 75 = 370 is consistent with it.
- `ruff check`, `ruff format --check` (131 files), `mypy app` strict (98 files) → clean.
- M9 tests: `tests/unit/test_optimization.py` **18**; cache/benchmark/profile tests in
  `tests/unit/test_monitoring.py` (5 of its 19); `test_corpus_version_changes_with_the_corpus`
  and `test_eval_results_persist_cost_and_usage` in `tests/integration/test_monitoring_db.py`.
- Live result (qwen3:8b + qwen3:1.7b, graph, judge on): **`optimized` accepted** — 36/38 vs
  33/38, tokens/request −21%, qwen3:8b calls/request 3.03 → 2.76, p50 43.1 s → 27.5 s (−36%),
  p95 65.4 s → 60.0 s (−8%), no newly failing case. Written up in `docs/benchmarks/day4.md`.

Not in this milestone: a measured benefit from any cache (≈0 hits, §4), the `optimized-small`
profile (defined, never run live), the semantic cache in any benchmark (off in every profile), a
loop-mode benchmark (graph only, as on Day 3), hosted-equivalent cost (no reference prices
configured).

---

## 1. What was built, by subsystem

### Switches resolved once (`app/agents/tuning.py`, `app/core/config.py`)
- `AgentTuning` (frozen dataclass) built by `from_settings`: `query_rewrite=True`,
  `search_top_k=5`, `max_evidence_chars=30_000`, `rule_routing=False`,
  `route_simple_to_small=False`. **Every default is the Day 3 behaviour**, so a profile turns one
  thing on at a time against the same code.
- Proof that the defaults really reproduce Day 3: the Day 4 `baseline` run has the **same token
  totals as the Day 3 baseline `4ec91948`** — 149,532 in / 6,680 out — 17 days apart.
- New settings: `agent_query_rewrite`, `agent_search_top_k` (3-10), `agent_max_evidence_passage_chars`
  (≥ 500), `agent_rule_routing`, `agent_route_simple_to_small`, `cache_query_embeddings`,
  `cache_tool_results`, `cache_ttl_s` (600), `cache_max_entries` (1024), `semantic_cache_enabled`,
  `semantic_cache_min_similarity` (0.97, bounded 0.8-1), `semantic_cache_ttl_s` (3600),
  `reference_price_input_per_mtok` / `_output_per_mtok` (None).

### Rule routing (`app/agents/policies.py`, `app/agents/nodes.py`)
Regexes in code replace the small model's `needs_tools` flag, which was true on nearly every plain
lookup in the Day 3 baseline and cost one extra qwen3:8b `select_tools` call per question.

| Rule | Matches (examples from the regex) | Used for |
|---|---|---|
| `needs_calculation` | "…, in seconds?", "expressed in minutes", "how much shorter", "by what factor", "what is the ratio", `3 * 4` | `needs_tools`; calculator nudge |
| `needs_metadata` | "metadata", "document id", "which version", "latest version of", "indexed status" | `needs_tools` |
| `is_comparison` | compare/comparison, change(d/s), differ*, conflict*, between, versus/vs, "v1 … v2" | `needs_tools` (added after the gate rejection, §4) |
| `is_simple_lookup` | not calculation and not comparison | small-model drafts |

- `classify_request`: with `rule_routing`, `needs_tools = calculation or metadata or comparison`;
  otherwise the model flag decides as before. The classifier still runs for the intent.
- **Calculator nudge** (`select_or_execute_tools`): with `rule_routing`, a calculation question,
  the calculator offered, and no calculator call in the response or the evidence, one more
  `main` call with `CALCULATOR_NUDGE` ("Call the calculator with an expression built from the
  numbers in the evidence; do not compute the result yourself."). If it still declines, the run
  continues as before.
- Module docstring: "A rule that misses falls back to the normal path, so a false negative costs
  quality on that question, never safety." The checklist records `policies.py` as written on
  30 Oct and wired on Day 4.

### Other graph switches (`app/agents/nodes.py`)
- **Query rewrite off** (`plan_workflow`): searches only the question, no `small` plan call. With
  it on, the original question is always searched too ("query rewriting may only add recall"),
  up to 3 queries.
- **Evidence cap** `_bounded(store, max_chars)`: passages in label order until the passage-text
  budget is spent (each counted at its rendered size, ≤ 1,500 chars), plus every tool result.
  Used by both `select_or_execute_tools` and `generate_answer`, and **only shown passages are
  citable** because `_draft_schema` builds the citation enum from the bounded store. Closes the
  Day 2 deferral "no total evidence cap in the answer prompt".
- **Small-model drafts** (`route_simple_to_small`): first draft on `small` only when there is no
  feedback, intent is `document_question`, there are no tool results and `is_simple_lookup`.
  `verify_answer` stays on `main`, and every revision after a rejected draft goes to `main`.
- **`INSUFFICIENT_TEXT`** (always on, not a switch): when a draft sets
  `insufficient_evidence`, the answer becomes "Your documents do not contain enough information to
  answer this question." and citations are emptied. Closes the Day 2/3 deferral (`tool-rto-diff`
  returned an unverified "45 minutes shorter" under `insufficient_evidence` in run `4ec91948`).
  Visible in the reports: Day 3 baseline `tool-rto-diff` answer "The v2 RTO is 45 minutes shorter
  than the v1 RTO."; Day 4 baseline the fixed text.

### Caches (`app/core/cache.py`, `app/agents/harness.py`, `app/tools/*.py`, `app/retrieval/hybrid_search.py`)
- `TTLCache[V]`: in-process `OrderedDict`, TTL + LRU, `CacheStats(hits, misses, evictions)`. The
  docstring states the rule: "Expiry is a backstop; versioned keys are what actually invalidate
  entries."
- **Query embeddings** (`HybridRetriever._embed_query`): key `(tenant_id, embedder.model, query)`.
  Tenant is in the key although the vector does not depend on it — a shared entry would let one
  tenant learn from latency that another asked the same question.
- **Tool results** (`Harness._cache_key`): `(tool name, tenant, sorted permissions, canonical JSON
  arguments, tool.cache_key(args, ctx))`. Only idempotent tools are looked up, and the lookup
  happens **after** the permission/approval policy, so a cache hit never skips a check. A hit is
  recorded in the ledger as an ok `tool_call` with `cache: "hit"`, `attempts: 0`.
- `Tool.cache_key` default is `None` = never cache. `Calculator` → `"pure"`;
  `DocumentMetadata` → corpus version; `DocumentSearch` → `(corpus version, retriever config JSON,
  max_chars)`, or `None` when no corpus-version source is wired. A failing `cache_key` logs and
  does not cache.
- **Corpus version** (`IngestionService.corpus_version`): `f"{count}:{max(updated_at)}"` over the
  tenant's documents, **read from the database** on every key build, so an ingest by the CLI in
  another process invalidates the API's cache.
- **Semantic answer cache** (`app/agents/semantic_cache.py`, off by default and in every profile):
  scope = tenant, permissions, allowed tools, mode, corpus version, config fingerprint; cosine ≥
  0.97; TTL 3600 s; stores only `cacheable` results — graph mode, `answered`, verification
  `supported`, no approval, only `document_search` / `document_metadata` / `calculator` calls.
  The docstring explains the threshold: "RPO in v1" and "RPO in v2" embed close together.

### Usage and cost accounting (`app/monitoring/cost.py`, `app/evaluation/report.py`, migration `0005`)
- `summarize(ledger)` → `UsageSummary`: model calls, **calls by model**, failed calls, retries
  (`attempts - 1` inside successful calls), tokens, tool calls, tool cache hits, embedding calls and
  embedding cache hits (from `document_search` timings), semantic hit. Everything is counted from
  the ledger; nothing new is instrumented.
- `hosted_equivalent`: `None` unless **both** reference prices are set; labelled
  `"hypothetical: tokens x configured reference price, not a real bill"`. Local cost stays `0`
  with pricing source `local` (§12).
- Eval aggregate gains: cost/request, cost/successful task, tokens/request, tokens/successful task,
  retries, retry rate, failed model calls, mean embedding calls, `cache_hits {tool, embedding,
  semantic}`, `quality_score` (mean of the judge criteria), hosted-equivalent block.
- Migration `0005_monitoring.py` adds `evaluation_results.cost_usd` (Numeric 12,6, nullable) and
  `usage` (JSONB) — closes the Day 3 finding "per-case cost not persisted"
  (`test_eval_results_persist_cost_and_usage`).

### Experiment profiles, runner, benchmark (`app/experiments/`, `datasets/experiments.json`)
- `experiments-v1`: `baseline` (no overrides), `optimized` (`agent_rule_routing`,
  `cache_query_embeddings`, `cache_tool_results`), `optimized-small` (+ small drafts),
  `optimized-no-rewrite` (purpose `rejected`), `vector-only` / `keyword-only` (purpose `probe`),
  `degraded-chunking` (purpose `regression-demo`: fixed 250-char chunks, no overlap, vector only,
  own tenant `eval_degraded_chunks`).
- `Profile.overrides` is validated against `OVERRIDABLE` (19 retrieval/routing/caching/chunking/
  model settings plus `eval_tenant`); anything else — database, keys, workspace root, limits — is a
  load error. A profile that changes chunking **must** set its own `eval_tenant` (ingestion is
  idempotent by content, so the default tenant would keep the old chunks).
- `apply(settings, profile)` re-validates through `Settings.model_validate`.
- `run_profile` builds **its own `Services`** (engine, caches, graph) per run: caches start cold
  and nothing leaks into the server's configuration. `kind="retrieval"` runs the model-free
  retrieval eval (journal 10); `kind="agent"` the full golden set.
- `benchmark.verdict(base, head, t)`: relative change of p50, p95, tokens/request, model
  calls/request; quality breaches = `regression.compare` breaches **except** the p95 one;
  `successful = any gain < −5% and no quality breach`. `markdown()` prints the §12 table
  (success, judge quality, p50/p95, calls, tokens, cost) plus verdicts.
- CLI: `agentops experiment list | run <profile> [--kind agent|retrieval] [--judge] [--case]
  [--out] | benchmark base.json head.json…`. API: `GET /api/v1/experiments`, `POST
  /api/v1/experiments/run` (profile *name* only; retrieval → 200 + report, agent → 202 + run id;
  shares the one-run eval slot; admin only, journal 11).

---

## 2. Files touched

New: `app/agents/tuning.py` (32), `app/agents/policies.py` (49), `app/agents/semantic_cache.py`
(133), `app/core/cache.py` (47), `app/monitoring/cost.py` (84),
`app/experiments/{__init__,configurations,runner,benchmark}.py` (250), `app/api/experiments.py`
(107), `datasets/experiments.json`, `datasets/baselines/golden-v1-graph-qwen3-8b-day4.json`,
`datasets/baselines/golden-v1-graph-qwen3-8b-optimized.json`, `docs/benchmarks/day4.md`,
`tests/unit/test_optimization.py` (330).

Modified: `app/agents/nodes.py` (+170/−31), `app/agents/harness.py` (tool cache),
`app/agents/service.py` (semantic cache lookup/store), `app/agents/graph.py`, `app/agents/state.py`
(`needs_calculation`), `app/tools/{base,calculator,document_metadata,document_search}.py`
(`cache_key`), `app/retrieval/hybrid_search.py` (embedding cache, `embed_cached`),
`app/ingestion/pipeline.py` (`corpus_version`), `app/evaluation/{report,runner,store}.py` (usage,
cost, profile), `app/core/config.py`, `app/services.py`, `app/cli.py`, `.env.example`, `README.md`.

Not in git: `reports/` (gitignored) — `bench-*.json`, `flaky-*.json`, `bench*.log`, `bench*.sh`,
`flaky.{log,sh}`, `retrieval-*.json`.

---

## 3. Key design decisions (taken vs rejected)

| Taken | Rejected | Why |
|---|---|---|
| Every optimization is a setting defaulting to Day 3 behaviour, grouped in profiles | Editing the code path and re-running | Same code, one switch at a time; the Day 4 baseline reproduced Day 3's token totals exactly. |
| Accept an optimization only via `verdict`: >5% gain **and** no quality breach under the eval-gate thresholds | Judge by latency, or by pass rate alone | §12: "successful only when cost or latency improves without unacceptable quality loss". The gate rejected two of three candidates (§4). |
| Code decides `needs_tools` (regex rules) | Small-model flag; a bigger router model | The flag was true on nearly every lookup; a regex costs no call and a miss only falls back. |
| Keep query rewrite on | Rewrite off (−32% tokens, −22% model calls) | Measured: multi_document 4/5 → 2/5 (§4). |
| Comparisons keep `select_tools` | Skip it for every non-calculation question | Measured: `multi-availability` 0/3 vs 3/3 (§4). |
| Tokens and call counts as the headline cost; local $0 | Inventing a per-token price | §12; hosted-equivalent only from configured prices, labelled hypothetical. |
| Cache keys built by the caller, with tenant + permissions + corpus version + config | One generic key function; TTL-based invalidation | Only the tool knows what its result depends on; TTL is a backstop. |
| Corpus version from the database | An in-process counter bumped on ingest | A CLI ingest in another process must invalidate the API's cache. |
| Semantic cache off, 0.97, verified read-only graph answers only | On by default; a looser threshold | v1/v2 questions embed close together; a wrong-version answer is worse than a miss. |
| Profile-only overrides from an allowlist; API takes a name | Arbitrary settings in the request | A profile can never widen what the agent may do (journal 11). |
| Each profile run builds its own `Services` | Mutating the running server's settings | Cold caches per run, no leakage into the server. |
| Repeat a suspicious case 3× per profile before concluding | Treating a single newly failing case as noise | Turned "flaky?" into "0/3 vs 3/3", a deterministic cause (§4). |
| Latency compared only between back-to-back runs | Comparing any two stored runs | The same baseline measured p50 60.6 s and 43.1 s (§4). |

---

## 4. Live results (qwen3:8b + qwen3:1.7b, Apple M4 16 GB, graph, judge on) — runs by the build session

All runs: `golden-v1` (38 cases), Ollama on `localhost:11435` (`export AGENTOPS_OLLAMA_BASE_URL`
in every `reports/*.sh`; why, see journal 11 §4), temperature 0. Times are local (+04).

### Timeline of the runs
| When | Script / log | Run | Result |
|---|---|---|---|
| 12 Nov 19:53 | `bench.sh` / `bench.log` | retrieval probes ×4, then `baseline` `4fd8c941` | 33/38, p50 60.6 s, p95 96.0 s |
| 12 Nov 20:40 | same | first `optimized` `01d2c833` (rewrite **off**) | **31/38 — rejected** |
| 12 Nov 21:10 | same | `degraded-chunking` `f52ac638` | 31/38 (journal 10) |
| 13 Nov 09:14 | `bench2-aborted.log` | `optimized` (rewrite on) | stopped after 15 cases |
| 13 Nov 13:34 | `bench2.sh` / `bench2.log` | `optimized` `d60baa97` | **35/38 — rejected** (newly failing `multi-availability`) |
| 13 Nov 17:46 | `flaky.sh` / `flaky.log` | `multi-availability` ×3 per profile | baseline 3/3 pass, optimized **0/3** |
| 13 Nov 17:53 | `bench3.sh` / `bench3.log` | fix: 3× the case, then full `optimized` `06794c01` | 3/3, then **36/38 — accepted** |
| 13 Nov 18:25 | `bench4.sh` / `bench4.log` | `baseline` rerun `cc2aa74e` (swap logged) | 33/38, p50 43.1 s |

### Mistake 1: rewrite off, trusted from a wrong probe — 31/38, rejected
The first `optimized` profile turned query rewrite off. The reasoning (build session): the
retrieval probe, which searches the raw question, reported **answer coverage 1.0**, so the
small-model rewrite looked like a wasted call. The live run (now `reports/bench-optimized-no-rewrite.json`):

| | baseline `4fd8c941` | rewrite off `01d2c833` |
|---|---|---|
| passed | 33/38 | **31/38** |
| multi_document | 4/5 | **2/5** |
| tool_use | 2/5 | 4/5 |
| tokens in/out | 149,532 / 6,680 | 100,919 / 4,724 |
| qwen3:1.7b / qwen3:8b calls per case | 1.71 / 3.03 | 1.00 / 2.68 |
| p50 / p95 | 60.6 s / 96.0 s | 31.4 s / 58.8 s |

Benchmark verdict (`docs/benchmarks/day4.md`): NOT ACCEPTED — pass rate −5.26% (limit −5%),
multi_document −40%, `answer_content` −13.80%, `required_citations` −18.75%, newly failing
`ans-rpo-v2`, `multi-rpo-change`, `multi-availability`, `adv-injected-rpo`.

Root cause: the raw question ("…RPO…") does not retrieve the "Availability and Recovery" chunks
of the architecture documents; the small model's rewrite expands it. Then **why had the probe said
1.0?** Answer coverage matched the string "15 minutes" in an unrelated document — the failover
runbook — so `multi-rpo-change` looked covered. Fix in `run_retrieval_eval`: only passages
**from the expected documents** count (`retrieved_text` filtered by `wanted`). Regression test
`test_answer_coverage_ignores_matches_in_unrelated_documents` (a cafeteria doc says "Lunch is
served 5 minutes after the bell"; keyword retrieval at k=1 returns it; coverage must be 0 of 1).
After the fix (probes re-run 13 Nov 05:14 UTC): baseline coverage **0.9706**, with
`multi-rpo-change` the one miss — the probe now agrees with the live run.

Caveat the Recorder found in the reports: this run also had the **first** routing rules (no
`is_comparison`), so its `multi-availability` failure is at least partly mistake 2, not the
rewrite. What is attributable to the rewrite is `multi-rpo-change`, `ans-rpo-v2` and
`adv-injected-rpo` (all RPO questions). The rejection stands either way.

The pre-fix "1.0" is the build session's account; the pre-fix probe lines in `bench.log` do not
print coverage, and `reports/retrieval-*.json` were overwritten by the post-fix run.

### Mistake 2: rule routing skipped comparisons — 35/38, rejected
With rewrite back on, `optimized` `d60baa97`: 35/38, tool_use **5/5**, but `multi-availability`
ended `failed_validation` ("I could not produce an answer verifiably grounded in your
documents."), 8 model calls (2 small, 6 main). `max_newly_failing = 0` → rejected.

Flaky or real? `flaky.sh` ran the case 3× per profile, interleaved:

| run | baseline | optimized |
|---|---|---|
| 1 | PASS 55.5 s | FAIL failed_validation 46.7 s |
| 2 | PASS 78.0 s | FAIL failed_validation 52.4 s |
| 3 | PASS 70.8 s | FAIL failed_validation 61.2 s |

The reports show the difference: baseline 4 tool calls / 7 model calls, optimized 3 / 8. Cause:
"Compare the availability targets of payments architecture v1 and v2." matched no rule, so
`needs_tools` was false and `select_tools` was skipped. In the baseline that step always added a
`document_metadata` call, which tells the model **which passage is v1 and which is v2**; without
it the draft failed verification and revisions. Fix: `policies.is_comparison` and
`needs_tools = calculation or metadata or comparison`; tests
`test_rule_routing_keeps_tool_selection_for_comparisons`, `test_comparison_questions_stay_on_main`.
After the fix: `flaky-fixed-{1,2,3}` 3/3 PASS (4 tool / 7 model calls, same as baseline).

### Final: `optimized` `06794c01` vs `baseline` `cc2aa74e` — accepted
Run back to back (optimized 17:56, baseline 18:26, 13 Nov). From the two reports:

| | baseline | optimized |
|---|---|---|
| passed | 33/38 (86.8%) | **36/38 (94.7%)** |
| tool_use | 2/5 | **5/5** |
| multi_document | 4/5 | 4/5 |
| tokens/request | 4,110.8 | 3,263.4 (**−21%**) |
| tokens/successful task | 4,733.7 | 3,444.7 |
| model calls/request | 4.74 | 4.47 (−6%) |
| qwen3:8b calls/request | 3.03 | **2.76** (−9%) |
| qwen3:1.7b calls/request | 1.71 | 1.71 |
| tool calls/request | 2.82 | 2.55 |
| p50 / p95 | 43.1 s / 65.4 s | **27.5 s / 60.0 s** (−36% / −8%) |
| judge quality (advisory) | 0.969 | 0.969 |
| cost | $0 (local) | $0 (local) |

Verdict: SUCCESS; quality breaches none; newly failing none; newly passing `tool-rpo-seconds`,
`tool-rto-diff`, `tool-settlement-seconds` (the calculator nudge). Still failing in both:
`multi-retention` (insufficient evidence) and `ws-edit-approved` (says "could not" after a
successful retried edit). The fix for mistake 2 cost ≈ 200 tokens/request (3,063.3 in the
rejected run → 3,263.4) and 0.05 qwen3:8b calls/request (2.71 → 2.76) — the price of
`multi-availability`.

Committed: `datasets/baselines/golden-v1-graph-qwen3-8b-day4.json` (= `cc2aa74e`) and
`…-optimized.json` (= `06794c01`); CI re-checks the second against the first (journal 11).

### Temperature 0: quality is deterministic, latency is not
The baseline ran twice, 12 Nov 19:53 and 13 Nov 18:25 (≈ 23 h apart). The Recorder compared the
reports case by case: **identical answers, token counts and judge scores for all 38 cases**
(and the token totals equal Day 3's). Latency did not repeat:

| | `4fd8c941` (12 Nov) | `cc2aa74e` (13 Nov) |
|---|---|---|
| p50 / p95 / max | 60.6 / 96.0 / 109.8 s | 43.1 / 65.4 / 73.9 s |

`bench4.sh` logged swap at start and end: 9,264.75 MB → 10,969.19 MB used. Per the build
session, another project's containers were running and swap ranged 7-11 GB. Consequences:
- A quality difference between two profiles is a behavioural difference, not sampling noise. The
  "3× per profile" check above *confirms* behaviour; it does not sample a distribution.
- Latency claims use runs made back to back; tokens and calls are the machine-independent signal.
  `optimized-no-rewrite` and `degraded-chunking` ran on 12 Nov; their latency is not comparable
  with the final table.

### Caches: ≈ 0 hits, not claimed
Each golden question runs once with cold caches per profile, so: optimized `cache_hits = {tool: 1,
embedding: 0, semantic: 0}`; embedding calls 81 in both runs (2.13 per case). The gains above come
from routing and the nudge, not caching. The caches are covered by unit/integration tests only.

### Relevance floor: measured, not adopted
A minimum vector score was the Day 1 deferral. Measured top vector scores overlap: unanswerable
questions reached **0.766**, answerable ones went down to **0.651**. Any floor that drops the
unanswerable ones also drops answerable ones; `retrieval_min_vector_score` stays `None`.

---

## 5. How it was verified

`tests/unit/test_optimization.py` (18): routing rules on the golden questions; rewrite off skips the
plan call; rule routing skips `select_tools` for plain lookups and keeps it for comparisons; the
model flag still decides without rule routing; calculator nudge; small draft verified on main;
revision after a rejected small draft on main; comparisons stay on main; evidence cap limits what
is shown **and** citable; insufficient draft text replaced; TTL cache expiry/eviction; tool cache
hits within a tenant and **never across**; tools without a cache key are never cached;
`document_search` key follows the corpus version; usage summary counts retries, embeddings and
cache hits; hosted-equivalent needs both prices; tuning comes from settings.

`tests/unit/test_monitoring.py` (M9 part): semantic cache scopes and threshold; only verified
read-only answers are cacheable; profiles cannot override unsafe settings; committed profiles load
and apply; benchmark verdict requires a gain without quality loss.

`tests/integration/test_monitoring_db.py` (M9 part, Postgres): corpus version changes with the
corpus; eval results persist cost and usage; answer coverage ignores matches in unrelated
documents.

Recorder re-runs: DB-free **295 passed**; ruff, format, mypy clean; the report comparisons in §4
(determinism, per-model calls, cache hits, `multi-availability` call counts) computed from the
JSON reports.

---

## 6. Commands

```bash
export AGENTOPS_OLLAMA_BASE_URL=http://localhost:11435   # see journal 11 §4
caffeinate -ims zsh reports/bench.sh                      # keep the Mac awake for long runs
uv run agentops experiment list
uv run agentops experiment run baseline  --kind agent --judge --out reports/bench-baseline.json
uv run agentops experiment run optimized --kind agent --judge --out reports/bench-optimized.json
uv run agentops experiment run optimized --kind agent --case multi-availability   # 3x in flaky.sh
uv run agentops experiment run baseline --kind retrieval --out reports/retrieval-baseline.json
uv run agentops experiment benchmark reports/bench-baseline-rerun.json reports/bench-optimized.json \
  reports/bench-optimized-no-rewrite.json reports/bench-degraded-chunking.json
# API (admin tenant)
curl localhost:8000/api/v1/experiments -H "X-API-Key: $KEY"
curl -X POST localhost:8000/api/v1/experiments/run -H "X-API-Key: $KEY" \
  -d '{"profile":"optimized","kind":"agent","judge":true}'
```

Settings: `AGENTOPS_AGENT_RULE_ROUTING`, `AGENTOPS_AGENT_QUERY_REWRITE`,
`AGENTOPS_AGENT_ROUTE_SIMPLE_TO_SMALL`, `AGENTOPS_CACHE_QUERY_EMBEDDINGS`,
`AGENTOPS_CACHE_TOOL_RESULTS`, `AGENTOPS_SEMANTIC_CACHE_ENABLED`,
`AGENTOPS_REFERENCE_PRICE_INPUT_PER_MTOK` / `_OUTPUT_PER_MTOK`.

---

## 7. What this milestone taught

1. **The gate earned its keep twice.** Both rejected candidates looked like wins on speed (−27%
   and −41% p50 against the back-to-back baseline); both broke a multi-document case.
2. **A probe that disagrees with the live run is a bug in the probe until proven otherwise.**
   Explaining the rejection found the coverage metric counting "15 minutes" from the wrong file.
3. **A routing call can carry hidden work.** `select_tools` looked redundant, but on comparisons it
   was the step that fetched version metadata. Removing a call means checking what its side
   effects were, per question type.
4. **Temperature 0 turns "flaky" into "deterministic" for quality** — and leaves latency at the
   mercy of swap. Repeat runs confirm behaviour; back-to-back runs are needed for latency.
5. **A cheap nudge beat a prompt rule.** tool_use 2/5 → 5/5 with one extra call only on
   calculation questions where the model skipped the calculator.
6. **Don't claim what wasn't measured.** Caches had ≈ 0 hits on a set where every question runs
   once; the benchmark says so instead of attributing gains to them.

---

## 8. Known limitations and open findings (Recorder review of the working tree)

1. **Single runs per profile.** Determinism at temperature 0 makes this defensible for quality,
   but only on this machine, model build and Ollama version; nothing pins the Ollama version in the
   report config.
2. **Caches unmeasured live** (≈ 0 hits). Their value for repeated questions is a claim backed by
   tests only. No hit-rate target or repeated-question benchmark exists.
3. **`optimized-small` was never run**, and the semantic cache is in no profile. Both are code
   paths with unit tests and no live evidence.
4. **Stale description.** `optimized-no-rewrite` in `datasets/experiments.json` says "48% lower
   p50" — that is against the slow 12 Nov baseline (60.6 s → 31.4 s). Against the back-to-back
   baseline it is −27% (`day4.md`). The rejected run also had the pre-`is_comparison` routing
   (§4 caveat), which the description does not mention.
5. **Reproduction commands vs table.** `day4.md` reproduces with `--out reports/bench-baseline.json`,
   but the table's baseline row is `bench-baseline-rerun.json` (`cc2aa74e`); the committed
   `-day4.json` baseline is the rerun.
6. **Regex routing is English- and phrasing-specific.** "How long after v1 did v2 ship?" contains
   no comparison keyword the rules know; a miss falls back to "no tool selection", which on a
   comparison is exactly mistake 2. The golden set covers only its own phrasings.
7. **Corpus version costs a database query per cache lookup** (`count`, `max(updated_at)`),
   even on a hit. It is also an aggregate, not a counter: it relies on every corpus change moving
   either the document count or the newest `updated_at`.
8. **Per-process caches** — replicas each have their own; no cross-process invalidation is
   needed (versioned keys), but hit rates divide by the number of replicas.
9. **`multi-retention` and `ws-edit-approved` still fail** in every profile (Day 3 carry-over).
10. **Pre-fix probe numbers are not preserved**: the retrieval reports were overwritten, so the
    "coverage 1.0" that motivated mistake 1 exists only in the build session's account.

---

## 9. What I'd do differently at 100x scale

- **Repeated questions in the benchmark**: replay a production-like log (with repeats and
  near-duplicates) so cache hit rates and their latency effect are measured, not assumed.
- **A learned or small-model router with an eval of its own**: routing decisions logged per
  question type, with a routing-only golden set (expected `needs_tools`, expected tools), so a
  miss like `multi-availability` is caught before a 38-case agent run.
- **Shared caches** (Redis) with the same versioned keys; corpus version from a per-tenant counter
  bumped transactionally instead of an aggregate query per lookup.
- **Dedicated benchmark hardware** (or at least recorded machine state: swap, other processes,
  Ollama version) in the report config, so latency becomes comparable across days.
- **Hosted-equivalent pricing table** versioned in the repo, so a "move to a hosted model" decision
  has numbers.
- **Per-question-type accounting** in the benchmark (lookup / comparison / calculation /
  workspace), since every regression found here was specific to one type.
