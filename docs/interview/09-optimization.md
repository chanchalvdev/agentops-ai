# Interview Prep 09 — Cost and Latency Optimization, Experiment Profiles, Benchmark (Day 4, M9)

Every answer is grounded in the working tree of branch `day-4-optimization-drift-hardening`
(uncommitted), the committed benchmark reports in `datasets/baselines/` and the run logs in
`reports/`. Format: direct answer, then the reasoning, then a code pointer. Companion:
`docs/journal/09-optimization.md`.

---

## Basic

### Q1. What did the optimization milestone deliver?
**Answer:** Usage and cost accounting from the ledger, five graph switches (`AgentTuning`), rule
routing in code with a calculator nudge, three caches (query embeddings, tool results, optional
semantic answers), a fixed `insufficient_evidence` text, experiment profiles with an allowlist, a
profile runner, a benchmark verdict, CLI + API, and a live baseline-vs-optimized benchmark in
which `optimized` was accepted: 36/38 vs 33/38, tokens/request −21%, p50 −36%.
Pointer: `app/agents/{tuning,policies,semantic_cache}.py`; `app/core/cache.py`;
`app/monitoring/cost.py`; `app/experiments/`; `docs/benchmarks/day4.md`.

### Q2. What does "cost" mean for a local model?
**Answer:** Money is $0 with pricing source `local`; tokens and latency are the real cost on a
16 GB M4. A hosted-equivalent figure is computed only when both reference prices are configured,
and labelled "hypothetical: tokens x configured reference price, not a real bill". None were
configured, so none is reported.
Pointer: `app/monitoring/cost.py:hosted_equivalent`, `HOSTED_EQUIVALENT_LABEL`.

### Q3. What is tracked per request?
**Answer:** Model calls (total and by model), failed calls, retries inside successful calls,
input/output tokens, tool calls, tool cache hits, embedding calls and embedding cache hits,
semantic cache hit, hosted-equivalent cost. All counted from the execution ledger; the eval
aggregate adds cost/request, cost/success, tokens/request and /success, retry rate and the judge
quality score.
Pointer: `app/monitoring/cost.py:summarize`; `app/evaluation/report.py:aggregate`.

### Q4. What are the five tuning switches?
**Answer:** `query_rewrite` (on), `search_top_k` (5), `max_evidence_chars` (30,000),
`rule_routing` (off), `route_simple_to_small` (off). Every default is the Day 3 behaviour; the
Day 4 baseline reproduced Day 3's token totals exactly (149,532 in / 6,680 out).
Pointer: `app/agents/tuning.py:AgentTuning`.

### Q5. What is an experiment profile?
**Answer:** A named entry in `datasets/experiments.json` with a description, a purpose
(benchmark, probe, regression-demo, rejected) and setting overrides limited to an allowlist. A
profile that changes chunking must use its own eval tenant.
Pointer: `app/experiments/configurations.py:Profile`, `OVERRIDABLE`.

### Q6. What were the final numbers?
**Answer:** `optimized` `06794c01` vs `baseline` `cc2aa74e`, run back to back: 36/38 vs 33/38;
tokens/request 4,110.8 → 3,263.4 (−21%); qwen3:8b calls/request 3.03 → 2.76; p50 43.1 → 27.5 s
(−36%); p95 65.4 → 60.0 s (−8%); tool_use 2/5 → 5/5; judge quality 0.969 both; no newly failing
case.
Pointer: `datasets/baselines/golden-v1-graph-qwen3-8b-{day4,optimized}.json`.

### Q7. Which caches exist and what are their keys?
**Answer:** Query embeddings: `(tenant, embedding model, query)`. Tool results: `(tool, tenant,
sorted permissions, canonical arguments, tool.cache_key)` where the tool adds corpus version and
retriever config. Semantic answers (off): tenant, permissions, allowed tools, mode, corpus version,
config fingerprint, cosine ≥ 0.97.
Pointer: `app/retrieval/hybrid_search.py:_embed_query`; `app/agents/harness.py:_cache_key`;
`app/agents/semantic_cache.py:SemanticCache.scope`.

---

## Logical

### Q8. Why put the tenant in the embedding cache key when the vector doesn't depend on it?
**Answer:** A shared entry makes the second asker's request faster, so one tenant could learn from
latency that another tenant asked the same question. Tenant isolation includes side channels.
Pointer: `app/retrieval/hybrid_search.py:_embed_query` comment.

### Q9. How do caches avoid serving stale evidence?
**Answer:** Versioned keys. `document_search` and `document_metadata` include the tenant's corpus
version (`count:max(updated_at)`, read from the database), and `document_search` also the
retriever config. An upload, delete or reindex changes the version, so old entries are simply
never looked up again. TTL (600 s) is a backstop.
Pointer: `app/ingestion/pipeline.py:corpus_version`; `app/tools/document_search.py:cache_key`;
`tests/integration/test_monitoring_db.py::test_corpus_version_changes_with_the_corpus`.

### Q10. Why read the corpus version from the database instead of an in-memory counter?
**Answer:** Ingestion also happens from the CLI in another process. An in-memory counter in the
API would never see that change and would keep serving pre-ingest results.
Pointer: `app/ingestion/pipeline.py:corpus_version` docstring.

### Q11. Can a cache hit bypass a permission check?
**Answer:** No. The tool cache is consulted inside `call_tool` after the policy gate (deadline,
budget, permission, argument validation, approval). Only idempotent tools are looked up, and a
tool returns `None` from `cache_key` by default, meaning never cache. Permissions are also in the
key.
Pointer: `app/agents/harness.py` (`key = ... if tool.idempotent`); `app/tools/base.py:cache_key`;
`tests/unit/test_optimization.py::test_tool_cache_hits_within_a_tenant_and_never_across`.

### Q12. What does rule routing replace, and why?
**Answer:** The small model's `needs_tools` flag. In the Day 3 baseline it was true on nearly every
plain lookup, which cost one qwen3:8b `select_tools` call per question. With rule routing, regexes
decide: tools are needed for calculations, metadata questions and comparisons.
Pointer: `app/agents/policies.py`; `app/agents/nodes.py:classify_request`.

### Q13. What is the calculator nudge?
**Answer:** With rule routing on, if the question needs a calculation, the calculator is offered,
and the model neither called it nor has a calculator result in evidence, one more `main` call
appends "Call the calculator… do not compute the result yourself." If the retry has tool calls they
are used; otherwise the run continues as before. tool_use went 2/5 → 5/5.
Pointer: `app/agents/nodes.py:select_or_execute_tools`, `CALCULATOR_NUDGE`.

### Q14. Why is `INSUFFICIENT_TEXT` always on rather than a switch?
**Answer:** It is a correctness fix, not an optimization: `insufficient_evidence` drafts skip
entailment verification, and Day 3's `tool-rto-diff` returned an unverified "45 minutes shorter"
claim under that status. Replacing the text with a fixed sentence and emptying citations means an
unverified claim can never be returned.
Pointer: `app/agents/nodes.py:generate_answer`, `INSUFFICIENT_TEXT`;
`tests/unit/test_optimization.py::test_insufficient_draft_text_is_replaced`.

### Q15. Why does the evidence cap matter for citations?
**Answer:** `_bounded` decides which passages fit the prompt, and the citation enum is built from
that bounded store. So the model can only cite what it was shown — a passage that was cut cannot
appear as a citation. Closes the Day 2 "no total evidence cap" deferral.
Pointer: `app/agents/nodes.py:_bounded`, `_draft_schema`;
`tests/unit/test_optimization.py::test_evidence_cap_limits_what_is_shown_and_citable`.

### Q16. What decides whether an optimization "wins"?
**Answer:** `benchmark.verdict`: at least one of p50, p95, tokens/request, model calls/request
improves by more than 5%, **and** `regression.compare` reports no breach other than p95 — same
thresholds as the eval gate, including `max_newly_failing = 0`.
Pointer: `app/experiments/benchmark.py:verdict`;
`tests/unit/test_monitoring.py::test_benchmark_verdict_requires_a_gain_without_quality_loss`.

---

## Advanced

### Q17. Walk through the first rejected profile.
**Answer:** The first `optimized` turned query rewrite off, because the retrieval probe (raw
question, no model) reported answer coverage 1.0. Live: 31/38, multi_document 4/5 → 2/5, newly
failing `ans-rpo-v2`, `multi-rpo-change`, `multi-availability`, `adv-injected-rpo`; the gate
rejected it on pass rate −5.26%, category −40%, two evaluators and newly failing cases. The raw
"RPO" question doesn't retrieve the "Availability and Recovery" chunks; the small-model rewrite
expands it. Kept as `optimized-no-rewrite`, purpose `rejected`.
Pointer: `reports/bench.log`; `datasets/experiments.json`; `docs/benchmarks/day4.md`.

### Q18. Why was the retrieval probe wrong?
**Answer:** Answer coverage checked whether the expected answer string appeared anywhere in the
retrieved text. "15 minutes" also appears in the failover runbook, so `multi-rpo-change` counted
as covered from the wrong document. Fix: only passages from the case's expected documents count.
After the fix baseline coverage is 0.9706 with `multi-rpo-change` as the miss — consistent with
the live run.
Pointer: `app/evaluation/retrieval_eval.py:run_retrieval_eval` (`wanted` filter);
`tests/integration/test_monitoring_db.py::test_answer_coverage_ignores_matches_in_unrelated_documents`.

### Q19. Walk through the second rejected profile.
**Answer:** With rewrite back on: 35/38 and tool_use 5/5, but `multi-availability` newly failed
with `failed_validation`. Repeated 3× per profile: baseline 3/3 pass, optimized 0/3. The reports
showed baseline 4 tool calls / 7 model calls vs optimized 3 / 8. "Compare the availability targets
of v1 and v2" matched no rule, so `select_tools` was skipped — and in the baseline that step always
added `document_metadata`, which tells the model which passage is v1 and which v2. Fix:
`is_comparison` keeps tool selection for comparisons. Then 3/3 pass and 36/38.
Pointer: `reports/flaky.log`, `reports/bench3.log`; `app/agents/policies.py:is_comparison`.

### Q20. How did you know `multi-availability` wasn't flaky?
**Answer:** Two things. Decoding is temperature 0, and the baseline rerun 23 hours later produced
identical answers, tokens and judge scores for all 38 cases — so the agent is deterministic for
quality on this setup. And the 3×-per-profile run gave 3/3 vs 0/3. At temperature 0 the repeat
confirms behaviour rather than sampling it; the cause was then found in the call counts.
Pointer: `reports/bench-baseline.json` vs `reports/bench-baseline-rerun.json`; `reports/flaky-*.json`.

### Q21. If quality is deterministic, why isn't latency?
**Answer:** Latency depends on machine state. The same baseline measured p50 60.6 s on 12 Nov and
43.1 s on 13 Nov; `bench4.sh` logged swap at 9.3 GB → 11.0 GB, with another project's containers
running. So latency claims come only from back-to-back runs (optimized 17:56, baseline 18:26);
tokens and call counts are the machine-independent signal.
Pointer: `reports/bench4.log`; `docs/benchmarks/day4.md` "How to read these numbers".

### Q22. What did the comparison fix cost?
**Answer:** About 200 tokens/request (3,063.3 in the rejected run → 3,263.4) and 0.05 qwen3:8b
calls/request (2.71 → 2.76), p50 25.2 → 27.5 s. That's the price of getting the comparison right,
and the result still beats the baseline on every cost metric.
Pointer: `reports/bench-optimized-v1routing.json` vs `datasets/baselines/…-optimized.json`.

### Q23. How is the semantic cache kept safe?
**Answer:** Off by default and in every profile. When on: scope includes tenant, permissions,
allowed tools, mode, corpus version and a config fingerprint; threshold 0.97 because "RPO in v1"
and "RPO in v2" embed close together; only graph answers that are `answered`, entailment
`supported`, had no approval and used only `document_search`/`document_metadata`/`calculator` are
stored. Workspace actions and refusals are never reused.
Pointer: `app/agents/semantic_cache.py:cacheable`;
`tests/unit/test_monitoring.py::test_semantic_cache_scopes_and_threshold`.

### Q24. How does a profile run avoid affecting the server?
**Answer:** `run_profile` builds a fresh `Services` from `apply(settings, profile)` — its own
engine, caches and graph — and closes it afterwards. Caches start cold for every profile, and the
running API's settings are never mutated. The API accepts only a profile name.
Pointer: `app/experiments/runner.py:run_profile`; `app/api/experiments.py`.

---

## Scenario

### Q25. "The benchmark says p50 −36%. Is the agent 36% faster?"
**Answer:** On those two back-to-back runs, on this machine, yes. The same baseline was 60.6 s p50
the evening before, so absolute latency moves by 40% with machine state alone. The robust claims
are −21% tokens/request and 3.03 → 2.76 qwen3:8b calls/request, which don't depend on swap.
Pointer: `docs/benchmarks/day4.md`.

### Q26. "Did caching help?"
**Answer:** Not in this benchmark: each golden question runs once with cold caches, so optimized
had 1 tool-cache hit and 0 embedding hits (81 embedding calls in both runs). The gains came from
routing and the nudge. Caching's benefit for repeated questions is shown by tests only; I'd
measure it with a replayed query log that contains repeats.
Pointer: `reports/bench-optimized.json` `aggregate.cache_hits`.

### Q27. "Someone uploads a new version of a document. Can a cached search return the old text?"
**Answer:** No: the upload changes the tenant's document count/`updated_at`, so the corpus version
in every `document_search` key changes and the old entries are never hit. Same for a reindex, a
delete, or a change in retriever config (it's in the key too).
Pointer: `tests/unit/test_optimization.py::test_document_search_cache_key_follows_the_corpus_version`.

### Q28. "A new comparison question fails only in the optimized profile."
**Answer:** Check whether `is_comparison` matched it. The rules are regexes over English phrasings;
a miss means no `select_tools` and therefore no `document_metadata` call — exactly the
`multi-availability` failure. Compare tool/model call counts between profiles in the two reports,
then add the phrasing (with a test) or add a routing-only eval case.
Pointer: `app/agents/policies.py`; `tests/unit/test_optimization.py::test_routing_rules`.

### Q29. "Why not set a relevance floor so unanswerable questions skip the model?"
**Answer:** It was measured: top vector scores for unanswerable questions reached 0.766, while
answerable ones went down to 0.651. The ranges overlap, so any floor that drops the unanswerable
ones also drops answerable ones. The floor stays off; the model reports insufficient evidence.
Pointer: `docs/benchmarks/day4.md`; `retrieval_min_vector_score` default `None`.

---

## Trade-off

### Q30. Regex routing vs a model router?
**Answer:** Regexes cost nothing, are deterministic and testable, and a miss only falls back to the
normal path. But they're phrasing-specific — the comparison miss proved that. A model router
generalizes better but costs a call (the thing being removed) and needs its own eval. For a
38-case local setup the regex plus a gate was the right first step.
Pointer: `app/agents/policies.py` docstring.

### Q31. Query rewrite: an extra small-model call vs recall?
**Answer:** Turning it off saved 32% tokens and 22% model calls but lost 2 of 5 multi-document cases
and three RPO questions. The small-model call is cheap (qwen3:1.7b) and the raw question is
always searched too, so the rewrite can only add recall. Kept on.
Pointer: `app/agents/nodes.py:plan_workflow`; benchmark verdict for `optimized-no-rewrite`.

### Q32. Small-model drafts: why built but not adopted?
**Answer:** `route_simple_to_small` drafts simple lookups on qwen3:1.7b with verification and all
revisions on qwen3:8b, so a bad draft costs one revision rather than a wrong answer. The
`optimized-small` profile exists but was never run live, so it isn't in the accepted profile —
nothing goes in without a benchmark.
Pointer: `app/agents/nodes.py:generate_answer`; `datasets/experiments.json`.

### Q33. Per-process TTL caches vs Redis?
**Answer:** In-process is simple, fast and correct for one API process: versioned keys mean no
cross-process invalidation is needed. With replicas each would have its own entries and hit rate.
Redis is the documented next step; the key design carries over unchanged.
Pointer: `app/core/cache.py`; README "Known limitations".

---

## Justification

### Q34. "Why trust a single 38-case run per profile?"
**Answer:** Because at temperature 0 the agent proved deterministic for quality: the baseline rerun
matched case for case. The one case where a profile looked different was rerun 3× and was 3/3 vs
0/3. That justifies single runs for quality comparison on this setup — not for latency, and not
across Ollama or model versions.
Pointer: `reports/bench-baseline*.json`; `reports/flaky.log`.

### Q35. "Why keep the rejected profiles in the experiments file?"
**Answer:** They're evidence. `optimized-no-rewrite` documents why the extra call stays; the
rejected routing run is kept as `reports/bench-optimized-v1routing.json`. A future "let's remove
the rewrite call" proposal can be re-run against the same baseline in one command.
Pointer: `datasets/experiments.json` (`purpose: rejected`).

### Q36. "Isn't the calculator nudge a second model call on every calculation question?"
**Answer:** Only when the model skipped the calculator. It brought tool_use from 2/5 to 5/5 and the
three tool cases from wrong process (or `insufficient_evidence`) to passing answers, and the total
still dropped 21% in tokens. Process correctness was a Day 3 open question; this answers it.
Pointer: `app/agents/nodes.py:select_or_execute_tools`.

---

## System design

### Q37. How would you run this optimization loop at 100x scale?
**Answer:** Profiles as the unit of change, gated by the same verdict; a replayed production log
(with repeats) to measure caches; per-question-type metrics (lookup, comparison, calculation,
workspace) because every regression here was type-specific; a routing-only eval set; Redis caches
with transactional per-tenant corpus counters; recorded machine state and pinned model/runtime
versions in every report; dedicated benchmark hardware for latency.
Pointer: journal 09 §9.

### Q38. What did this milestone teach about optimizing agents?
**Answer:** The gate rejected two of three speedups, each for breaking a multi-document case; a
probe that disagrees with the live run is suspect first; a "redundant" call can carry hidden work
(version metadata); temperature 0 makes quality comparisons deterministic but not latency; and
don't credit caches you didn't measure.
Pointer: journal 09 §7.
