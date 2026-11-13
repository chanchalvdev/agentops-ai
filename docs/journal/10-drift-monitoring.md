# Journal 10 — Drift and Operational Monitoring, Retrieval Evaluation, Regression Demo (Day 4, M10)

Milestone: the second part of Day 4 in product.txt §18 — "drift detector, alerts, regression
demo" with the deliverable "drift check that catches a deliberate regression" — implementing §13
(drift and operational monitoring), the monitoring endpoints of §14 and the Day 3 deferral "RAG
metrics recall@k / MRR missing". Branch `day-4-optimization-drift-hardening`, **uncommitted**
working tree on top of `b497479`. Written by the Recorder agent from the code, the stored reports,
`reports/drift-degraded.txt` and test output; facts reported by the build session are marked.

Status (re-run by the Recorder):
- DB-free: `uv run pytest -q -W error tests/unit tests/security` → **295 passed**; ruff, format,
  mypy strict clean. DB tests skipped in the Recorder's run (port 5435 now belongs to another
  project's Postgres); the build session reports **370 passed** with Postgres.
- M10 tests: `tests/unit/test_monitoring.py` 14 of its 19 (detector, retrieval scoring, rate
  limiter); `tests/integration/test_monitoring_db.py` 7 of its 10 (retrieval eval persisted,
  coverage from expected documents only, drift check with alerts, operator-only, check + summary,
  per-tenant metrics, experiments API).
- Live demo: `degraded-chunking` against the Day 4 baseline → **`Drift status: drift (3 alerts)`,
  exit 1**. Pass rate 0.8684 → 0.8158 and answer coverage 0.9706 → 0.8824 (both `measured`);
  recall@k and hit rate stayed 1.0.

Not in this milestone: statistical tests (§13: "only when justified by the collected data" — 38
cases, one run per profile), scheduled checks (a check runs only when asked), an alert
acknowledge/notify path, drift checks during the provider outage itself.

---

## 1. What was built, by subsystem

### Retrieval-only evaluation (`app/evaluation/retrieval_eval.py`)
- Every golden case with `expected_sources` and no workspace use (21 of 38) is searched **as
  asked**, through the same retriever and configuration the agent uses, **without a model**: the
  four Day 4 probes ran in ≈ 1-2 s each (`bench.log` timestamps), vs ≈ 30-50 min for an agent run.
- Per case: `hit` (any expected file in top k), `recall` (fraction of expected files),
  `reciprocal_rank` (first expected file), `answer_groups` / `answer_covered`, latency.
- Aggregate: hit rate, recall@k, MRR, **answer coverage@k**, `answer_cases`, `answer_misses`,
  empty results, **source distribution** (share of returned chunks per file), latency p50/p95,
  misses.
- **Answer coverage** exists because file-level metrics saturate here: 5 documents, 10 chunks in
  the baseline tenant, so top-5 returns half the corpus, and the 250-char degraded configuration
  still scored recall 1.0 (measured). Coverage asks whether the passage holding the answer was
  retrieved: of the case's `must_contain` groups that occur in its expected documents (computed
  values like "900 seconds" are not counted), how many occur in retrieved passages **from those
  documents**. The "from those documents" part was added after the "15 minutes" bug (journal 09
  §4).
- Persisted in `retrieval_eval_runs` (dataset name/version/sha, profile, config, metrics, cases);
  `RetrievalReport` is also the JSON file format, so a committed file and a stored run are
  interchangeable (same idea as Day 3's `RunReport`).

### Detector (`app/monitoring/drift.py`, `datasets/drift_thresholds.json`)
Four families, each "current vs reference", every one producing `Signal(category, metric, status,
evidence, severity, baseline, current, delta, threshold, samples, message)`:

| Family | Reference | Signals (threshold from the file) |
|---|---|---|
| quality | eval baseline report (`monitoring_eval_baseline` = Day 4 baseline) | pass rate (drop ≤ 0.05, critical), each category (drop ≤ 0.25), zero-tolerance evaluators `unauthorized_tools`, `workspace_effects` (any drop, critical, measured at any sample size), judge quality (drop ≤ 0.1, **always suspected**) |
| retrieval | retrieval baseline (`retrieval-golden-v1.json`) | recall@k, hit rate, MRR, answer coverage (drop ≤ 0.05 each; recall and coverage critical), source-distribution shift (total variation ≤ 0.25) |
| data | snapshot stored by the previous check | ingestion failure rate (≤ 0.1, measured only with ≥ 5 ingestions), mixed embedding models in a tenant (critical), mixed chunking configs in a tenant, corpus size change (≤ 25%), golden dataset changed (suspected) |
| operational | previous window vs current window (24 h each) | p95 latency (+50%), timeout rate (≤ 0.05, critical), failure rate (≤ 0.1), provider error rate (≤ 0.05), retry rate (≤ 0.1), tokens per execution (+30%) |

- **Evidence labels** (§13 "distinguish a real measured drift signal from an unverified
  suspicion"): `measured` when inputs are comparable **and** the sample is large enough;
  otherwise `suspected`, with the reason appended ("inputs not strictly comparable", "small
  sample (n)"). Severity: critical/warning when measured, `info` when suspected.
- Comparability: quality — same dataset sha, `mode` and `case_filter`; retrieval — same dataset
  sha and k. A **config change does not make retrieval incomparable**: it is the cause being
  tested, so it is named in the message (`config changed: {"chunk_size_chars": [1200, 250], …}`).
- Minimum samples: `min_eval_cases 10`, `min_retrieval_cases 10`, `min_executions 20` (both
  windows for latency/tokens).
- A signal that cannot be computed is `insufficient_data` and raises nothing.
- `overall`: `drift` if any measured breach, else `suspected` if any breach, else `ok`.

### Metrics from the database (`app/monitoring/metrics.py`)
- `operational_metrics(start, end, tenant_id=None)`: one aggregate over `agent_executions`
  (counts by status, `percentile_cont` p50/p95 over finished runs, tokens, calls, cost, unpriced
  count) plus one over `execution_events` (ok model calls, retries = `attempts - 1`, provider error
  codes). `PROVIDER_ERRORS = provider_error, provider_unavailable, provider_timeout, rate_limited`.
- `data_metrics`: status mix, recent ingestions and failures, embedding models and chunking configs
  **grouped per tenant**, `mixed_*` = tenants whose own ready corpus holds more than one value,
  MIME types, chunk count.
- `window_pair(hours)`: previous and current window for the operational comparison.

### Checks and alerts (`app/monitoring/alerts.py`, migration `0005_monitoring.py`)
- `MonitoringService.check(requested_by, eval_head=…, eval_base=…, retrieval_head=…,
  retrieval_base=…, persist=True)`: defaults are the baseline files, the latest completed eval run
  **in the baseline's mode**, and the latest retrieval run. Builds the `DriftReport` with
  `references` (what was compared with what, incl. both operational windows) and a `snapshot` for
  the next check's data signals.
- Tables: `drift_checks` (status ok/drift/suspected, `baseline` JSONB = references + data
  snapshot, `signals` JSONB), `monitoring_alerts` (one row per breached signal: category, metric,
  severity, evidence, baseline/current value, threshold, message, status `open`), and
  `retrieval_eval_runs`. Indexes on `created_at`.

### API and CLI (`app/api/monitoring.py`, `app/cli.py`)
| Method + path | Who | Behaviour |
|---|---|---|
| `GET /api/v1/metrics?hours=` | any tenant | the caller's own executions + this process's cache stats and rate-limited count |
| `GET /api/v1/monitoring/summary` | admin | latest drift status, open alerts, eval and retrieval history, operational and data metrics, caches |
| `GET /api/v1/monitoring/drift` | admin | latest check |
| `GET /api/v1/monitoring/alerts?status=open\|acknowledged` | admin | alert records |
| `POST /api/v1/monitoring/check` | admin, `expensive` rate class | run and persist a check (no model calls) |

CLI: `agentops drift check [--eval-report] [--eval-baseline] [--retrieval-report]
[--retrieval-baseline] [--persist/--no-persist]` prints one line per signal and **exits 1 on
measured drift** (suspected is printed, not failed); `agentops metrics [--hours] [--tenant]`.

---

## 2. Files touched

New: `app/monitoring/{__init__,metrics,drift,alerts}.py` (958 lines with `cost.py`, which is M9),
`app/evaluation/retrieval_eval.py` (248), `app/api/monitoring.py` (104),
`app/db/migrations/versions/0005_monitoring.py` (83, shared with M9's `evaluation_results`
columns), `datasets/drift_thresholds.json`, `datasets/baselines/retrieval-golden-v1.json`,
`tests/unit/test_monitoring.py` (353), `tests/integration/test_monitoring_db.py` (249).

Modified: `app/db/models.py` (`RetrievalEvalRun`, `DriftCheck`, `MonitoringAlert`),
`app/core/config.py` (`monitoring_eval_baseline`, `monitoring_retrieval_baseline`,
`monitoring_thresholds`), `app/main.py` (router), `app/cli.py` (`drift check`, `metrics`),
`README.md` ("Drift monitoring").

Not in git: `reports/drift-degraded.txt`, `reports/retrieval-*.json`.

---

## 3. Key design decisions (taken vs rejected)

| Taken | Rejected | Why |
|---|---|---|
| Deterministic thresholds in a versioned file | Statistical tests (KS, PSI, CUSUM) | §13: start deterministic; 38 cases and single runs don't justify statistics yet. |
| `measured` vs `suspected` on every breach, with the reason in the message | One "drift" flag | §13 asks for it explicitly; a breach on a changed dataset or 4 executions is not a fact. |
| `insufficient_data` per signal | Defaulting to "ok" | No data must not look healthy per signal (but see §8 item 1 on the overall status). |
| Retrieval eval without a model as the retrieval signal | Inferring retrieval drift from agent runs | Seconds instead of ~40 minutes; deterministic; catches chunking/ranking changes before answers do. |
| Answer coverage, restricted to expected documents | Only recall/hit/MRR | File-level metrics saturate on 5 documents; the degraded config kept recall 1.0. |
| Config change named in the message, still `measured` | Treating any config change as incomparable | The demo *is* a config change; the detector should say what changed, not refuse to judge. |
| Mixed embedding/chunking checked per tenant | Corpus-wide | The experiment tenant legitimately uses another chunking config (§4). |
| Judge quality always `suspected` | Judge as a measured signal | Same rule as Day 3: a model score is not ground truth. It even rose under degradation (§4). |
| Zero-tolerance evaluators measured at any sample size | Same sample rule as rates | One unsafe action is a real signal. |
| Operational = previous vs current window | Fixed baseline | Latency and error rates are about "what changed recently"; the baseline file is for quality. |
| One alert row per breached signal, linked to its check | Alerts only in logs | §13 "alert records"; queryable history. |
| Monitoring endpoints admin-only; `/metrics` per tenant | Everything per tenant, or everything open | Drift and history are cross-tenant data. |

---

## 4. Live results — the regression demo (build session run, Recorder read the output)

Profile `degraded-chunking`: fixed 250-char chunks, no overlap, vector-only retrieval, own tenant
`eval_degraded_chunks`. Agent run `f52ac638` (12 Nov) and retrieval run (13 Nov 05:14 UTC), checked
against the Day 4 baseline `cc2aa74e` and `retrieval-golden-v1.json` on 13 Nov 15:00 UTC.

### Prediction before the demo
File-level retrieval metrics would not move on this corpus; answer coverage and pass rate would.

### Retrieval probes (no model, k = 5, 21 cases)
| profile | hit@k | recall@k | MRR | answer coverage | misses |
|---|---|---|---|---|---|
| baseline (hybrid) | 1.0 | 1.0 | 0.952 | 0.9706 | `multi-rpo-change` |
| vector-only | 1.0 | 1.0 | 0.921 | 1.0 | — |
| keyword-only | 1.0 | 1.0 | 0.921 | 0.9706 | `multi-rpo-change` |
| degraded-chunking | 1.0 | 1.0 | **1.0** | **0.8824** | `ans-rto-v2`, `ans-escalation` |

MRR *improved* under degradation (smaller chunks, more of the top ranks from the right file) —
exactly the kind of metric that would have hidden the regression. Per-query latency p95 (from
the reports): keyword-only 2.9 ms, vector/hybrid 173-195 ms (the query embedding dominates).

### Agent run
`f52ac638`: 31/38 (81.6%), answerable 9/11, newly failing `ans-rpo-v2`, `ans-escalation`,
`multi-failover` (all `insufficient_evidence`), newly passing `multi-retention`. Judge quality
**0.998** vs baseline 0.969 — higher, because the degraded run says "insufficient evidence" more
often and the judge prompt rates a correct-sounding refusal 5/5.

### `agentops drift check` output (`reports/drift-degraded.txt`, abbreviated)
```
  quality     pass_rate                  DRIFT/measured     pass rate 0.8684 -> 0.8158
  quality     category.answerable        ok                 answerable pass rate 1.0 -> 0.8182
  quality     judge_quality_score        ok                 judge quality 0.969 -> 0.998
  retrieval   recall_at_k                ok                 recall_at_k 1.0 -> 1.0; config changed: {"chunk_overlap_chars": [200, 0], "chunk_size_chars": [1200, 250], ...
  retrieval   mrr                        ok                 mrr 0.9524 -> 1.0; config changed: ...
  retrieval   answer_coverage            DRIFT/measured     answer_coverage 0.9706 -> 0.8824; config changed: ...
  retrieval   source_distribution_shift  ok                 share of retrieved chunks per file moved by 0.0857
  data        ingestion_failure_rate     insufficient_data  0 of 0 recent ingestions failed
  data        mixed_chunking_configs     ok                 one chunking per tenant corpus; overall {'structured:1200:200': 10, 'fixed:250:0': 5}
  operational failure_rate               ok                 failure_rate 0.8333 -> 0.0
  operational provider_error_rate        ok                 provider_error_rate 0.5227 -> 0.0
  operational tokens_per_execution       DRIFT/measured     tokens per execution 750.8333 -> 3516.2806

Drift status: drift (3 alerts, check bc890dab-e496-4a44-92cb-fa52b532a680)
```
Exit 1. The prediction held: recall@k and hit rate stayed 1.0, answer coverage and pass rate
caught it.

### Mistake: the mixed-chunking signal fired on a legitimate setup
First version computed "mixed chunking configs" over the whole corpus. The experiment's
`eval_degraded_chunks` tenant deliberately uses `fixed:250:0` while every other tenant uses
`structured:1200:200`, so the data signal fired — a false alarm caused by the experiment itself.
Fix: `data_metrics` groups embedding models and chunking configs **per tenant**; `mixed_*` lists
only tenants whose own corpus mixes values. The output above shows the overall mix (10 + 5
documents) reported as `ok`. Test: `test_mixed_embedding_models_are_critical` (two chunking
configs overall, none mixed within a tenant → `ok`; a tenant mixing embedding models → critical,
tenant named).

### The third alert is real but is not a regression
`tokens_per_execution 750.8 → 3,516.3` (measured, ≥ 20 executions in both windows). The previous
24 h window was the provider outage of journal 11 §4: failure rate 0.8333, provider error rate
0.5227 — failed executions spend few tokens. The current window is the recovery. The detector
reports what changed; a human reads why. Note the outage itself shows only as `ok` here: rates are
judged on the *current* window, and no check was run during the outage.

### What the drift check did *not* flag that the eval gate did
`answerable` dropped 18 points (under the 25-point category limit). The drift detector has no
"newly failing cases" rule, and among evaluators only the zero-tolerance ones are signals; the
benchmark gate on the same reports breached on `answer_content`, `expected_behavior`,
`required_citations` and three newly failing cases (journal 09). The pass-rate signal tripped at
−5.26% against a −5% limit: a net change of two cases. One net case less would have been `ok`.

---

## 5. How it was verified

`tests/unit/test_monitoring.py` (M10 part, no DB): comparable quality drop → measured, critical,
delta −0.25; different dataset → suspected ("not strictly comparable"); 4-case sample → suspected
("small sample"); no run → `insufficient_data` and `overall` "ok"; stable → ok; answer-coverage drop
measured and critical with recall ok; config change named; total variation distance; case scoring;
coverage ignores values the documents never state; mixed embedding models critical, chunking per
tenant ok, corpus +20% ok; changed question set is a suspicion; operational latency measured with
30 executions, suspected with 3, timeout rate critical; rate limiter refuses then refills.

`tests/integration/test_monitoring_db.py` (M10 part, Postgres): retrieval eval scores and persists;
coverage ignores matches in unrelated documents; a deliberately broken retrieval (keyword mode,
vector floor 0.99) produces a persisted `drift` check with a measured critical `answer_coverage`
alert naming the config change, readable via `/monitoring/drift` and `/monitoring/alerts`;
monitoring/experiments/evaluations are 403 for a non-admin; check endpoint and summary; `/metrics`
counts only the caller's executions.

Recorder: read `reports/drift-degraded.txt` and the four `reports/retrieval-*.json` (numbers in §4
are from those files); DB-free suite 295 passed.

---

## 6. Commands

```bash
# Retrieval probes (seconds, no model)
uv run agentops experiment run baseline --kind retrieval --out reports/retrieval-baseline.json
uv run agentops experiment run degraded-chunking --kind retrieval --out reports/retrieval-degraded-chunking.json
# Regression demo (agent run ~30 min), then the check
uv run agentops experiment run degraded-chunking --kind agent --judge --out reports/bench-degraded-chunking.json
uv run agentops drift check --eval-report reports/bench-degraded-chunking.json \
  --retrieval-report reports/retrieval-degraded-chunking.json      # exit 1 on measured drift
uv run agentops drift check                                          # latest stored runs vs baselines
uv run agentops metrics --hours 24 [--tenant tenant_a]
# API (admin tenant for /monitoring/*)
curl -X POST localhost:8000/api/v1/monitoring/check -H "X-API-Key: $KEY"
curl localhost:8000/api/v1/monitoring/summary -H "X-API-Key: $KEY"
curl "localhost:8000/api/v1/monitoring/alerts?status=open" -H "X-API-Key: $KEY"
curl "localhost:8000/api/v1/metrics?hours=24" -H "X-API-Key: $KEY"
```

The exact flags used for `reports/drift-degraded.txt` are not recorded in the file; the second
command is the documented usage that matches its references (degraded agent and retrieval reports
as heads).

Settings: `AGENTOPS_MONITORING_EVAL_BASELINE` (default
`datasets/baselines/golden-v1-graph-qwen3-8b-day4.json`), `AGENTOPS_MONITORING_RETRIEVAL_BASELINE`,
`AGENTOPS_MONITORING_THRESHOLDS`.

---

## 7. What this milestone taught

1. **Pick the metric that can see the failure.** recall@k and hit rate stayed 1.0 and MRR went *up*
   under a clearly worse chunking; answer coverage and pass rate went down.
2. **A metric is code and needs a regression test.** Coverage counted a string from the wrong
   document; it was found only because a live run disagreed with it.
3. **The detector's own experiments can trip it.** A legitimate per-tenant config looked like a
   mixed corpus until the signal was scoped per tenant.
4. **"Changed" is not "worse".** The tokens alert was recovery from an outage; the detector says
   what changed, the human says why.
5. **The judge moves the wrong way under degradation** (0.969 → 0.998), because refusals score well.
   Keeping it `suspected` by design was right.
6. **Gate and detector answer different questions.** The gate (per-case, many evaluators) is
   stricter than the drift detector (aggregate rates); a small regression passes drift and fails
   the gate.

---

## 8. Known limitations and open findings (Recorder review of the working tree)

1. **"Nothing measured" reports `Drift status: ok`.** `overall()` returns `ok` when every signal is
   `insufficient_data` (`test_no_run_means_insufficient_data_not_ok` asserts it), and
   `DriftReport.status` has no `insufficient_data` value. Per-signal lines are honest; the summary
   line and exit code 0 are not — the README's "too few samples is never reported as healthy" is
   true per signal only.
2. **No newly-failing rule in quality drift** (§4): a single regressed case is invisible unless it
   crosses a rate threshold; the eval gate has `max_newly_failing = 0`, the detector does not.
3. **Alerts cannot be acknowledged.** `GET /monitoring/alerts?status=acknowledged` exists, but no
   endpoint or CLI sets that status; every alert stays `open`. No notification channel.
4. **Checks run only on demand.** No scheduler; an outage between checks is seen only as the
   *previous* window of a later check (and then not flagged, §4).
5. **Operational rates are absolute on the current window**, not compared with the previous one;
   only p95 and tokens are relative. A failure-rate *drop* from 83% is "ok" (correct) but the
   window that had it was never alerted.
6. **Single-run comparisons.** At temperature 0 this is deterministic for quality on one machine,
   but a model, Ollama or hardware change would mix sampling differences into "drift".
7. **Retrieval probe uses the raw question**, not the agent's rewritten queries — its baseline
   coverage (0.9706, `multi-rpo-change`) is below what the agent actually retrieves.
8. **Corpus not in the comparability key** (Day 3 finding 11 still open): a quality comparison
   on a different corpus is labelled `measured` if the dataset sha, mode and filter match.
9. **Data snapshot depends on the previous check row**: the first check has no corpus-size
   reference (`insufficient_data`), and a check run with `--no-persist` leaves no snapshot.
10. **Default heads ignore the profile.** `latest_eval_run` picks the newest completed *full* run
    in the baseline's mode (filtered runs are correctly skipped), and the retrieval head is the
    newest retrieval run — either may be an experiment (e.g. `degraded-chunking`), which then
    becomes the default head of the next `drift check` until another run is stored.

---

## 9. What I'd do differently at 100x scale

- **Scheduled checks and alert routing**: a periodic job, deduplicated alerts with acknowledge /
  resolve, and paging only on measured critical signals.
- **Production quality signals**: sample real traffic for judge scoring and user feedback; keep
  the golden set as the controlled reference.
- **Statistical tests once data allows**: bootstrap confidence intervals over k repeated runs or
  over production samples; PSI on source distribution and query-embedding clusters for input
  drift.
- **Per-case and per-segment drift**: newly failing golden cases, and metrics per tenant, question
  type and document type, so one tenant's corpus change does not average out.
- **Comparability key** = dataset sha + corpus manifest + model/runtime versions + profile, with
  anything else labelled suspected.
- **Time-series store** (Prometheus/OTel metrics) for operational signals with rolling windows
  instead of SQL aggregates per check.
