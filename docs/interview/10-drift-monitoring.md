# Interview Prep 10 — Drift and Operational Monitoring, Retrieval Evaluation, Regression Demo (Day 4, M10)

Every answer is grounded in the working tree of branch `day-4-optimization-drift-hardening`
(uncommitted), the committed baselines in `datasets/baselines/` and the demo output
`reports/drift-degraded.txt`. Format: direct answer, then the reasoning, then a code pointer.
Companion: `docs/journal/10-drift-monitoring.md`.

---

## Basic

### Q1. What does the monitoring subsystem consist of?
**Answer:** A model-free retrieval evaluation, a deterministic threshold detector over four signal
families (quality, retrieval, data, operational), operational and data metrics read from Postgres,
persisted checks and alert records, thresholds in `datasets/drift_thresholds.json`, an API
(`/metrics`, `/monitoring/summary|drift|alerts|check`) and a CLI (`agentops drift check`,
`agentops metrics`).
Pointer: `app/monitoring/{drift,metrics,alerts}.py`; `app/evaluation/retrieval_eval.py`;
`app/api/monitoring.py`.

### Q2. What are the four drift families and their references?
**Answer:** Quality: latest full eval run vs the committed eval baseline. Retrieval: latest
retrieval eval vs the retrieval baseline. Data: corpus now vs the snapshot saved by the previous
check. Operational: the current 24 h window of executions vs the previous 24 h.
Pointer: `app/monitoring/alerts.py:MonitoringService.check`.

### Q3. What does the retrieval evaluation measure?
**Answer:** For the 21 golden cases with expected sources: hit@k, recall@k, MRR, answer coverage@k,
source distribution and latency — by searching the raw question through the agent's retriever, no
model involved. It runs in about a second.
Pointer: `app/evaluation/retrieval_eval.py` module docstring, `retrieval_metrics`.

### Q4. What is a `Signal`?
**Answer:** One comparison: category, metric, status (`ok`, `drift`, `insufficient_data`),
evidence (`measured`/`suspected`) and severity when drifting, baseline, current, delta, threshold
text, sample size and a message. Every breached signal becomes a `monitoring_alerts` row linked to
its `drift_checks` row.
Pointer: `app/monitoring/drift.py:Signal`; migration `0005_monitoring.py`.

### Q5. What did the regression demo show?
**Answer:** The `degraded-chunking` profile (250-char fixed chunks, no overlap, vector only) checked
against the Day 4 baseline: drift, exit 1. Pass rate 0.8684 → 0.8158 and answer coverage 0.9706 →
0.8824, both measured; recall@k and hit rate stayed 1.0 and MRR rose to 1.0.
Pointer: `reports/drift-degraded.txt`.

### Q6. How do you run a check?
**Answer:** `agentops drift check` (latest stored runs vs baselines) or with
`--eval-report`/`--retrieval-report` files; `POST /api/v1/monitoring/check` for admins. The CLI
exits 1 on measured drift; suspected drift is printed but does not fail.
Pointer: `app/cli.py:drift_check`.

---

## Logical

### Q7. Why is answer coverage needed when you have recall@k?
**Answer:** On a 5-document, 10-chunk corpus, top-5 returns half the corpus; file-level metrics
saturate. The degraded configuration still scored recall 1.0 and hit rate 1.0. Coverage asks the
question the answer step needs: did we retrieve the passage that contains the answer string?
Pointer: `app/evaluation/retrieval_eval.py:answer_coverage`; `reports/retrieval-*.json`.

### Q8. What exactly counts as "covered"?
**Answer:** Of the case's `must_contain` groups that occur in its expected source documents (so
computed values like "900 seconds" are excluded), the fraction that occur in retrieved passages
**from those expected documents**. A match in an unrelated file doesn't count.
Pointer: `run_retrieval_eval` (`wanted` filter);
`tests/unit/test_monitoring.py::test_answer_coverage_ignores_values_the_documents_never_state`.

### Q9. When is a breach `measured` and when `suspected`?
**Answer:** Measured when the inputs are comparable and the sample is big enough (≥ 10 eval cases,
≥ 10 retrieval cases, ≥ 20 executions in both windows). Otherwise suspected, with the reason in the
message. The judge score is always suspected; zero-tolerance safety evaluators are measured at any
sample size.
Pointer: `app/monitoring/drift.py:_signal`, `quality_signals`.

### Q10. What makes two runs "comparable"?
**Answer:** Quality: same dataset sha, `mode` and `case_filter`. Retrieval: same dataset sha and
k. A changed retrieval *config* doesn't make it incomparable — it's usually the cause — so the
message lists every changed config field instead.
Pointer: `quality_signals` (`differences`); `retrieval_signals` (`changed`, `cause`).

### Q11. Why is the judge score never a measured signal?
**Answer:** It's a model's opinion, same family as the answerer. In the demo it went *up* under
degradation, 0.969 → 0.998, because the degraded run refused more often and the judge prompt rates
a correct-sounding refusal highly. Treating it as fact would have reported improvement.
Pointer: `quality_signals` (`comparable=False` for `judge_quality_score`).

### Q12. Why measure source-distribution shift with total variation distance?
**Answer:** It's simple, bounded 0-1 and interpretable: half the sum of absolute differences in
each file's share of retrieved chunks. A shifted corpus or ranking moves it; the demo moved 0.0857,
under the 0.25 limit.
Pointer: `app/monitoring/drift.py:total_variation`;
`tests/unit/test_monitoring.py::test_total_variation_distance`.

### Q13. Why operational "previous window vs current window" and not a fixed baseline?
**Answer:** Latency, errors and tokens are about what changed recently and depend on load and
hardware; a months-old baseline would be meaningless. p95 and tokens are relative (+50%, +30%);
timeout, failure, provider-error and retry rates are absolute limits on the current window.
Pointer: `app/monitoring/drift.py:operational_signals`; `app/monitoring/metrics.py:window_pair`.

---

## Advanced

### Q14. Walk through `MonitoringService.check`.
**Answer:** Load thresholds; resolve the eval baseline file and the latest completed full run in its
mode (or the given report); the retrieval baseline and the latest retrieval run; data metrics now;
the previous and current operational windows; the golden dataset's sha. Build quality, retrieval,
data (with the previous check's snapshot) and operational signals, compute `overall`, record
`references` and a snapshot, then persist one `drift_checks` row and an alert row per drifting
signal in one transaction.
Pointer: `app/monitoring/alerts.py:check`.

### Q15. How are operational metrics computed?
**Answer:** Two SQL aggregates per window. Over `agent_executions`: counts by status,
`percentile_cont` p50/p95 of `total_ms` over finished runs, token and call sums, cost and unpriced
count. Over `execution_events`: ok model calls, retries as `attempts − 1`, and provider error codes
(`provider_error`, `provider_unavailable`, `provider_timeout`, `rate_limited`).
Pointer: `app/monitoring/metrics.py:operational_metrics`.

### Q16. What went wrong with the mixed-chunking signal?
**Answer:** It was computed over the whole corpus. The experiment's `eval_degraded_chunks` tenant
legitimately uses `fixed:250:0` while everyone else uses `structured:1200:200`, so the detector
flagged the experiment itself. A mix only breaks retrieval *inside* one tenant's corpus, so it's
now grouped per tenant; the demo reports the overall 10 + 5 mix as `ok`.
Pointer: `app/monitoring/metrics.py:data_metrics`, `_mixed`;
`tests/unit/test_monitoring.py::test_mixed_embedding_models_are_critical`.

### Q17. The demo raised a tokens-per-execution alert. Is that a regression?
**Answer:** No. Tokens/execution went 750.8 → 3,516.3, measured, because the previous window was a
provider outage — failure rate 0.8333, provider errors 0.5227 — and failed executions spend few
tokens. The current window is the recovery. The detector correctly reports a change; deciding
"recovery, not regression" is a human reading the other signals.
Pointer: `reports/drift-degraded.txt` (operational lines).

### Q18. How was the coverage bug found?
**Answer:** Indirectly. The retrieval probe said rewriting queries didn't matter (coverage 1.0), the
live run said it did (multi_document 4/5 → 2/5). Explaining the disagreement showed "15 minutes"
being matched in the failover runbook for an RPO question. Fixed and tested with a cafeteria
document that contains "5 minutes".
Pointer: `tests/integration/test_monitoring_db.py::test_answer_coverage_ignores_matches_in_unrelated_documents`.

---

## Scenario

### Q19. "Drift check says ok. Are we fine?"
**Answer:** Check the per-signal lines first. `overall()` returns `ok` even if every signal is
`insufficient_data` — the summary has no "nothing measured" state, and the exit code is 0. An empty
database or a first check reads "ok". That's an open finding; the fix is an `insufficient_data`
overall status.
Pointer: `app/monitoring/drift.py:overall`;
`tests/unit/test_monitoring.py::test_no_run_means_insufficient_data_not_ok`.

### Q20. "One golden case regressed. Will the drift check catch it?"
**Answer:** Probably not. Quality drift is rate-based: one case out of 38 is −2.6%, under the 5%
pass-rate limit, and only zero-tolerance evaluators are per-evaluator signals. The eval gate
(`max_newly_failing = 0`) catches it. In the demo pass-rate drift tripped at −5.26% on a net change
of two cases.
Pointer: `app/monitoring/drift.py:quality_signals`; `app/evaluation/regression.py:compare`.

### Q21. "Someone uploaded a document with a different embedding model."
**Answer:** `mixed_embedding_models` fires as critical, measured, naming the tenant whose ready corpus
now holds two models — vector search across two embedding spaces is meaningless. Fix by reindexing
that tenant on one model.
Pointer: `data_metrics` (`mixed_embedding_models`); `data_signals` (`critical=key ==
"embedding_models"`).

### Q22. "The golden dataset was edited. What happens to quality drift?"
**Answer:** `question_set_changed` is raised as a suspicion, and any quality breach against the
baseline is labelled `suspected` ("inputs not strictly comparable") because the dataset sha differs. The
baseline must be re-run before quality comparisons count.
Pointer: `data_signals` (`question_set_changed`);
`tests/unit/test_monitoring.py::test_changed_question_set_is_a_suspicion`.

### Q23. "Provider errors spiked last night; the morning check is green."
**Answer:** Rates are judged on the current window only, and the night is now the previous window.
No check ran during the outage, and there's no scheduler. That's exactly what the demo shows: the
outage's 83% failure rate appears only as the "before" of an `ok` line. Scheduled checks and
alerting would close it.
Pointer: `operational_signals`; journal 10 §8 items 4-5.

---

## Trade-off

### Q24. Deterministic thresholds vs statistical drift detection?
**Answer:** With 38 cases, single runs and a 24 h window of a few dozen executions, statistics would
add false precision. Thresholds are explainable and versioned; the evidence label carries the
uncertainty. With repeated runs or production samples, I'd add bootstrap intervals and PSI.
Pointer: `app/monitoring/drift.py` docstring; product.txt §13.

### Q25. Retrieval eval without a model vs measuring retrieval inside agent runs?
**Answer:** Model-free is deterministic and takes seconds, so it can run often and isolate retrieval
from generation. The cost: it searches the raw question, not the agent's rewritten queries, so its
baseline coverage (0.9706) understates what the agent retrieves.
Pointer: `run_retrieval_eval`; `reports/retrieval-baseline.json` (`answer_misses`).

### Q26. Gate vs detector — why both?
**Answer:** The gate is per-change and strict (per case, many evaluators, newly failing = breach);
it decides whether a change ships. The detector watches the system over time across quality,
retrieval, data and operations, with evidence labels. They overlap on quality but answer different
questions.
Pointer: `app/evaluation/regression.py`; `app/monitoring/drift.py`.

---

## Justification

### Q27. "Why call a config change 'measured' drift instead of 'not comparable'?"
**Answer:** Because a config change is the most common cause of retrieval drift, and the
regression demo is one. Refusing to judge would hide it. The detector judges the metric on the same
dataset and k, and names every changed field so the reader sees the likely cause.
Pointer: `retrieval_signals` (`cause`).

### Q28. "Why trust a drift demo built from one run per side?"
**Answer:** At temperature 0 the agent is deterministic for quality on this setup — the baseline
rerun 23 h later matched case for case — and the retrieval eval has no model at all. The prediction
was stated first (file-level metrics flat, coverage and pass rate down) and it held.
Pointer: journal 09 §4; `reports/drift-degraded.txt`.

### Q29. "Why are the monitoring endpoints admin-only but `/metrics` isn't?"
**Answer:** `/metrics` filters to the caller's executions, so it leaks nothing. Drift checks,
alerts and eval history aggregate across all tenants. Tested: 403 for a non-admin on summary,
drift, check, experiments and evaluation runs.
Pointer: `app/api/monitoring.py`; `tests/integration/test_monitoring_db.py::test_monitoring_is_operator_only`.

---

## System design

### Q30. How would you build this at 100x scale?
**Answer:** Scheduled checks with alert dedupe, acknowledge/resolve and paging only on measured
critical signals; production sampling for quality (judge + user feedback) with the golden set as
reference; per-tenant and per-question-type segments; per-case drift on golden runs; a
comparability key including corpus manifest and model/runtime versions; operational metrics in a
time-series store with rolling windows; statistical tests once there are enough samples.
Pointer: journal 10 §9.

### Q31. What did this milestone teach?
**Answer:** Pick the metric that can see the failure (recall stayed 1.0, MRR rose); metrics are
code and need tests; your own experiments can trip your detector; "changed" isn't "worse"; the judge
moves the wrong way under degradation; and a detector of aggregates is weaker than a per-case gate.
Pointer: journal 10 §7.
