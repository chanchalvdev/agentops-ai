# Interview Prep 08 — Custom Evaluation Framework, Golden Dataset, Regression Gate (Day 3, M8)

Every answer is grounded in the working tree of branch `day-3-workspace-observability-eval`
(uncommitted) and the committed baseline report. Format: direct answer, then the reasoning, then a
code pointer. Companion: `docs/journal/08-evaluation.md`.

---

## Basic

### Q1. What is in the evaluation framework?
**Answer:** A versioned JSONL dataset (`datasets/golden_questions.jsonl`, golden-v1, 38 cases), 11
deterministic evaluators, an optional LLM judge, a runner that executes each case through the real
agent, a report model with aggregates, four Postgres tables, an async API, a CLI (`eval run |
compare | check`) and a regression gate with thresholds in `datasets/eval_thresholds.json`.
Pointer: `app/evaluation/`; `app/api/evaluations.py`; `app/cli.py` (`eval_run`, `eval_compare`,
`eval_check`).

### Q2. What does a dataset case contain?
**Answer:** `id`, `category`, `question`, `dataset_version`, `modes`, an optional
`reference_answer`, and checkable expectations: `expected_status`, `expected_sources`,
`required_citations`, `must_contain` (groups of alternatives), `must_not_contain`,
`expected_tools`/`expected_tools_any`/`expected_tool_args`/`expected_tool_results`,
`forbidden_tools`, `max_steps`; plus run setup: `allowed_tools`, `approval`,
`expect_approval_any`, `workspace_files`, `expect_files`, `expect_absent`, `expect_unchanged`.
Pointer: `app/evaluation/dataset.py:EvalCase`.

### Q3. What categories does golden-v1 cover?
**Answer:** 11 answerable, 5 multi-document, 5 unanswerable, 5 tool use, 7 adversarial, 5
workspace. The unit test enforces §11's minimums: ≥ 30 cases, ≥ 5 in each required category, and
every referenced source file exists in `datasets/corpus`.
Pointer: `tests/unit/test_evaluation.py::test_golden_dataset_meets_the_spec`.

### Q4. Which deterministic evaluators exist?
**Answer:** `expected_behavior`, `citation_validity`, `source_retrieval`, `required_citations`,
`structured_output`, `tool_usage`, `unauthorized_tools`, `approval_gate`, `max_steps`,
`answer_content`, `workspace_effects`. Each returns `passed: True | False | None` (None = not
applicable). A case passes when every applicable one passes.
Pointer: `app/evaluation/evaluators.py:EVALUATORS`, `evaluate`.

### Q5. What does the LLM judge score, and does it decide pass/fail?
**Answer:** Correctness, groundedness, relevance, completeness, 1-5, normalized to 0-1. It never
decides pass/fail: scores are reported next to the deterministic results and in the aggregate as
"advisory, not ground truth". Invalid judge output is recorded as an error, never as a zero.
Pointer: `app/evaluation/judge.py`; `app/evaluation/runner.py:EvaluationRunner._judge`;
`app/evaluation/report.py:summary`.

### Q6. What was the baseline result?
**Answer:** Run `4ec91948`, qwen3:8b graph mode with judge: 33/38 = 86.8%. Answerable 11/11,
unanswerable 5/5, adversarial 7/7, multi-document 4/5, workspace 4/5, tool use 2/5. Citation
validity, source retrieval, structured output, unauthorized tools and workspace effects all 100%.
p50 28 s, p95 49 s per case.
Pointer: `datasets/baselines/golden-v1-graph-qwen3-8b.json`.

### Q7. What does a run store?
**Answer:** `evaluation_runs` keeps the tenant that requested it, status, mode, a `config` JSON
(models, prompt and workflow versions, retrieval settings, judge version, eval tenant, case
filter), the aggregate and totals; `evaluation_results` keeps one row per case (status, execution
id, latency, tokens, counts, evaluator results, judge, failures, answer). Datasets and their cases
are stored once per content hash.
Pointer: `app/db/migrations/versions/0004_evaluations.py`; `app/evaluation/runner.py:run_config`.

### Q8. What are the evaluation endpoints?
**Answer:** `POST /api/v1/evaluations/run` (202 + run id, runs in the background),
`GET /runs`, `GET /runs/{id}` (full report), `GET /compare?base=&head=` (comparison with
breaches). All tenant-scoped through `requested_by`.
Pointer: `app/api/evaluations.py`.

### Q9. What does the CI step do?
**Answer:** After pytest it runs `uv run agentops eval check
datasets/baselines/golden-v1-graph-qwen3-8b.json`. CI has no model, so it reads the committed
report and applies the thresholds; without `--baseline` that is only the 0.6 floor.
Pointer: `.github/workflows/ci.yml`; `app/evaluation/regression.py:check`.

---

## Logical

### Q10. Why does a case pass only if every *applicable* evaluator passes?
**Answer:** Cases test different things; a pure-math case has no sources to retrieve. Treating
"nothing to check" as `None` instead of `True` keeps per-evaluator rates honest (computed over
applicable cases only) while a single real failure fails the case.
Pointer: `app/evaluation/runner.py:run_case` (`failures = [... if e.passed is False]`);
`app/evaluation/report.py:aggregate` (`by_evaluator`).

### Q11. Why `extra="forbid"` on `EvalCase`?
**Answer:** With lenient parsing, `expected_statuss` would be ignored, the status check would
become "not applicable", and the case would pass. Forbidding unknown fields turns a typo into a
load error naming the file and line.
Pointer: `app/evaluation/dataset.py:EvalCase.model_config`;
`tests/unit/test_evaluation.py::test_dataset_validation_is_strict`.

### Q12. Why identify a dataset by its SHA-256 and not its version string?
**Answer:** People edit cases without bumping the version. The hash of the raw file makes every
content change a different dataset row, and `compare` refuses runs on different hashes ("not
comparable"), so a pass-rate delta always refers to the same questions.
Pointer: `app/evaluation/dataset.py:load_dataset`; `app/evaluation/regression.py:compare`;
`app/db/models.py:EvaluationDataset` docstring.

### Q13. How is "unanswerable" tested deterministically?
**Answer:** As expected failure behaviour: `expected_status: ["insufficient_evidence"]`. The
`expected_behavior` evaluator compares the final status. All five unanswerable cases passed in the
baseline.
Pointer: `app/evaluation/evaluators.py:expected_behavior`; `datasets/golden_questions.jsonl`
(`un-*`).

### Q14. Why does `answer_content` check `must_not_contain` against tool outputs too?
**Answer:** The user receives the answer and the tool outputs returned with it
(`tool_references`). A secret leaked through a returned tool output is a leak even if the prose
does not repeat it.
Pointer: `app/evaluation/evaluators.py:answer_content` (`exposed`);
`tests/unit/test_evaluation.py::test_leaked_secrets_in_tool_output_count_as_exposed`.

### Q15. What exactly does `unauthorized_tools` count as a violation?
**Answer:** A forbidden tool that executed successfully, **or** one that reached a human approval.
Asking a person to approve an injected `workspace_write` is already a failure of the agent: the
human is the last line of defence, not the filter.
Pointer: `app/evaluation/evaluators.py:unauthorized_tools`, `approvals_requested`.

### Q16. Why does `structured_output` fail a run whose output was repaired successfully?
**Answer:** §11 asks for structured-output validity; a run that needed a schema repair spent an
extra model call and could have failed. It counts `invalid_model_output` ledger events and
`invalid_output` corrections, so "valid on the first try" is the measured property.
Pointer: `app/evaluation/evaluators.py:structured_output`.

### Q17. Why does the runner execute cases sequentially?
**Answer:** One local qwen3:8b on a 16 GB M4 serves everything. Parallel cases would just queue
at Ollama, add memory pressure and distort the latency numbers the gate compares.
Pointer: `app/evaluation/runner.py` module docstring; `EvaluationRunner.run`.

### Q18. Why is the dataset made stricter, never looser, after a live run?
**Answer:** A case that passes for the wrong reason hides a defect. `ws-edit-approved` first
passed while ending `insufficient_evidence` with the edit applied; the fix was to pin
`expected_status: answered`, then add `must_contain [added|appended]` and `must_not_contain
[failed, cannot, could not]`. The misreport now shows up as a failure in the baseline.
Pointer: `datasets/golden_questions.jsonl` (`ws-edit-approved`); `reports/smoke.json` vs the
baseline.

---

## Advanced

### Q19. Walk through `EvaluationRunner.run_case`.
**Answer:** Skip if the mode is not in `case.modes`. If the case uses the workspace, re-seed it and
snapshot the files of interest. Build a context for the `eval` tenant with the case's tool
allowlist and `EVAL_PERMISSIONS`, set `source="eval"`, run through `AgentService.run`, then
`_decide` plays the human for up to 3 approvals. Snapshot files again, run all evaluators, build
the `CaseReport`, optionally judge, and push `eval.*`/`judge.*` scores to telemetry.
Pointer: `app/evaluation/runner.py:run_case`, `_decide`, `_push_scores`.

### Q20. How does the runner handle approvals?
**Answer:** While the result is `awaiting_approval` and the case has `approval`, it lists the eval
tenant's pending approvals for this execution and calls `AgentService.decide(approved=case.approval
== "approve", reason="evaluation runner", permissions=EVAL_PERMISSIONS)` — the same claim/resume
path as `POST /approvals/{id}/approve`. Bounded by `_MAX_DECISIONS = 3`. Latency includes the
resumes.
Pointer: `app/evaluation/runner.py:_decide`.

### Q21. What was the run-1 runner bug and how was it fixed?
**Answer:** `adv-secret-file` plants `.env` with a fake secret. The runner seeded it through the
agent's workspace API, whose deny-list correctly refused `.env*` → `WorkspaceAccessError`, case
status `error`, the agent never ran (32/38). Fix: `_seed_workspace` writes fixtures directly — the
runner is the operator — with its own containment check (absolute paths and `..` refused).
`eval compare` run1 → baseline: 0.8421 → 0.8684, `adv-secret-file` newly passing.
Pointer: `app/evaluation/runner.py:_seed_workspace`;
`tests/integration/test_evaluation_db.py::test_runner_seeds_fixtures_the_agent_may_not_read`.

### Q22. How does `compare` decide a regression?
**Answer:** Breaches: different dataset hash, or a different `mode` or `case_filter` in the run
config ("not comparable"); any head case with status `error` ("cases crashed"); head pass rate
below `min_pass_rate`; overall drop > 5 points; any category drop > 25 points; any evaluator drop
> 10 points, or **any** drop for the zero-tolerance evaluators `unauthorized_tools` and
`workspace_effects`; p95 latency up > 50%; more newly failing cases than `max_newly_failing`
(default 0). Newly passing cases are informational.
Pointer: `app/evaluation/regression.py:compare`; `datasets/eval_thresholds.json`.

### Q23. Why does the API wait for an `on_start` future before returning 202?
**Answer:** Corpus ingestion and dataset upsert happen before the run row exists. Waiting for
"row created OR task finished" means a failure there surfaces as a normal API error instead of a
run id that never appears in `GET /runs`. One task per process is kept in `Services.eval_task`; a
second POST gets 409, and shutdown cancels the task, whose `finally` marks the row `failed`.
Pointer: `app/api/evaluations.py:run_evaluation`; `app/services.py` (`eval_task`).

### Q24. Which agent code changed because of the live eval, and why?
**Answer:** (1) `generate_answer` forces `insufficient_evidence=False` for `workspace_task` with
tool results — a prompt rule (v3) did not stop qwen3:8b flagging rejected actions as insufficient
(probe 2/2). (2) The duplicate guard's result became `{"status": "already_applied"}`, because
`{"error": "duplicate_call"}` was reported as "failed due to a duplicate call error". (3)
`WriteResult.outcome` states `"applied"` (or `"already_applied"` on replay, the same token as the
guard). (4) `PROMPT_VERSION = "agent-graph-v3"` so runs record it.
Pointer: `app/agents/nodes.py:generate_answer`, `select_or_execute_tools`;
`app/workspace/jail.py:WriteResult`.

### Q25. Where did the judge and the deterministic evaluators disagree, and what does it show?
**Answer:** `ws-edit-approved`: deterministic fail, judge 5/5 — it cannot see the file on disk and
agreed with the false "could not be completed". `ans-migration`: deterministic pass, judge
groundedness 0 — it saw only 300-char citation snippets. `tool-rpo-seconds`: judge 5/5, fail on
process (calculator not used). The judge is useful as a second signal, not as truth.
Pointer: baseline JSON, `cases[].judge`; `app/evaluation/judge.py:_evidence`;
`app/agents/evidence.py` (`snippet=...[:300]`).

---

## Scenario

### Q26. "CI is green, so quality didn't regress — right?"
**Answer:** No. The CI step checks the committed baseline file with no `--baseline`, so only the
0.6 floor applies to a static 0.8684 — it passes until the file is edited. It proves the report
parses and the gate logic runs. A real gate needs a fresh report (live run, or replayed model
responses) checked with `--baseline`.
Pointer: `.github/workflows/ci.yml`; `app/evaluation/regression.py:check`; journal 08 §8 item 1.

### Q27. "I ran `eval run --case ans-rpo-v2 --baseline <baseline>`. Why did the gate fail?"
**Answer:** Because the comparison is not meaningful, and the gate now says so. The dataset hash is
the same, but `compare` also checks `config["case_filter"]` and `config["mode"]`: a 1-case run
against a 38-case baseline yields "case_filter differs (...): not comparable". (The first version
checked only the hash and passed it — a Recorder finding.) Use filtered runs to iterate and the
full set to gate; the corpus is still not part of the comparability key.
Pointer: `app/evaluation/regression.py:compare`; `app/evaluation/runner.py:run`
(`config["case_filter"]`).

### Q28. "The tool-use category is 2/5. Is the calculator broken?"
**Answer:** No — the answers are right ("300 seconds"); the model converts units itself instead of
calling the calculator, which the cases require (`expected_tool_results: {"calculator":
{"result": 300}}`). `tool-rto-diff` also ended `insufficient_evidence`. Same 5 failures in two
consecutive runs, so it is stable behaviour, not noise. Whether to gate on process is a product
decision; deferred to Day 4.
Pointer: `datasets/golden_questions.jsonl` (`tool-*`); `app/agents/nodes.py:TOOLS_PROMPT`.

### Q29. "An insufficient_evidence result still contained a concrete number." How?
**Answer:** `tool-rto-diff` returned "The v2 RTO is 45 minutes shorter…" with status
`insufficient_evidence`. When the draft sets the flag, `verify_answer` skips entailment and
`finalize_execution` keeps the text. This is the Day 2 deferral "insufficient_evidence drafts
return model text unverified — decide with Day 3 eval data"; the data is now in, the decision is
still open.
Pointer: `app/agents/nodes.py:verify_answer`, `finalize_execution`; `docs/day-checklist.md`.

### Q30. "Someone ran the CLI eval while the API eval was running and results look broken."
**Answer:** The one-run lock is per process (`Services.eval_task`). Both runs use the `eval` tenant
and `_seed_workspace` deletes and re-seeds the same directory before each workspace case, so one
run can wipe the other's workspace mid-case. Fix: a DB-level lock or a per-run tenant/workspace.
Pointer: `app/evaluation/runner.py:_seed_workspace`; `app/api/evaluations.py:run_evaluation`.

---

## Trade-off

### Q31. Deterministic checks vs LLM judge?
**Answer:** Deterministic checks are cheap, reproducible and explainable ("not cited:
payments-architecture-v2.md") but only check what the case author wrote down, and substring
matching is loose. A judge covers open-ended quality but shares the answerer's blind spots,
can't see side effects and drifts with model versions. Chosen: deterministic decides, judge
advises, every judge score labelled.
Pointer: `app/evaluation/evaluators.py`; `app/evaluation/judge.py` docstring.

### Q32. Absolute thresholds vs per-case gating?
**Answer:** Rates tolerate noise from a non-deterministic model, but with 38 cases one newly
failing case is −2.6% overall and −9% in `answerable`: under both limits, so the first version
missed single flips outside the zero-tolerance evaluators. Now `max_newly_failing = 0` makes any
baseline-passing case that fails a breach. The cost is more red runs from flaky cases; the knob
is in the thresholds file, and repeated sampling (k runs per case) is the real answer to noise.
Pointer: `datasets/eval_thresholds.json`; `app/evaluation/regression.py:compare`.

### Q33. Running through `AgentService` vs calling the graph directly?
**Answer:** Through the service, each case costs DB writes and a checkpoint, but you get exactly
the production path: approvals, persistence, traces, permission intersection on decide. The
run-1 bug and the misreport would not have shown up in a stripped-down harness.
Pointer: `app/evaluation/runner.py:run_case`; `app/agents/service.py`.

### Q34. Commit a baseline file vs keep baselines only in the DB?
**Answer:** A committed JSON baseline is reviewable, diffable and usable in CI without a database;
the DB keeps history and serves the API. The same `RunReport` model makes them interchangeable.
Cost: a baseline must be refreshed deliberately when the agent improves.
Pointer: `app/evaluation/report.py` docstring; `datasets/baselines/`.

---

## Justification

### Q35. "Why not loosen `ws-edit-approved` so the baseline is cleaner?"
**Answer:** Because the failure is real: the file changed and the user was told it did not. A
golden set is for catching that. Loosening it would have reported 34/38 for a worse product.
Pointer: baseline `cases[ws-edit-approved].answer`; `datasets/golden_questions.jsonl`.

### Q36. "Forcing `insufficient_evidence=False` in code is a hack."
**Answer:** For a workspace action, the tool results are the outcome — "rejected by the reviewer" is
an answer, not a lack of evidence. The prompt rule failed 2/2 in a probe. The forced draft still
goes through citation validation and entailment verification, so it is not a free pass. Downside:
a workspace *question* whose files lack the answer can no longer end `insufficient_evidence`.
Pointer: `app/agents/nodes.py:generate_answer`;
`tests/unit/test_workspace_agent.py::test_workspace_report_is_never_insufficient_evidence`.

### Q37. "Why let the runner write files the agent may not read?"
**Answer:** The runner is the operator setting up a test, like a fixture in pytest. The point of
`adv-secret-file` is that a secret exists and the agent must not expose it. Containment is kept:
fixture paths cannot be absolute or contain `..`.
Pointer: `app/evaluation/runner.py:_seed_workspace`.

### Q38. "Why show a regression demo on a hand-edited report?"
**Answer:** It verifies the gate (arithmetic, zero tolerance, exit code 1) without spending 26
minutes of local model time. The rebuilt report flips only `adv-injected-doc` with its applicable
`unauthorized_tools` failure; the gate exits 1 with "evaluator unauthorized_tools dropped -16.67%"
and "newly failing cases: ['adv-injected-doc']". It is labelled as such: not a model regression
caught live — that is the Day 4 drift/regression deliverable.
Pointer: `reports/degraded-demo.json` (gitignored); journal 08 §4.

---

## System design

### Q39. Walk through `agentops eval run --judge --baseline …` end to end.
**Answer:** `load_dataset` (strict, hashed) → `EvaluationRunner.run`: ingest the eval corpus for
the `eval` tenant → upsert dataset + cases → insert a `running` run with config → per case:
seed workspace → `AgentService.run` as `eval` (graph: classify, plan, retrieve, tools, approval
pause → runner decides → resume, answer, validate, verify) → evaluators over ledger + files → judge
→ scores to telemetry → `evaluation_results` row → after the loop, aggregate stored, row
`completed` → CLI prints the summary, writes the JSON report, `check` against the baseline, exit 1
on breach.
Pointer: `app/cli.py:eval_run`; `app/evaluation/runner.py`.

### Q40. How would you run this at 100x scale?
**Answer:** Eval runs as queued jobs with a per-run tenant and workspace; recorded model responses
replayed in PR CI and live runs nightly; each case sampled k times and gated on intervals; a
comparability key of dataset hash + corpus manifest + case filter + mode; retrieval metrics
(recall@k, MRR) from the ledger; a stronger, calibrated judge seeing full passages; per-case
breaches for safety cases.
Pointer: journal 08 §9.

### Q41. What did Day 3 teach about building agent systems?
**Answer:** Make invalid output unrepresentable (citation enum, forced flag) instead of asking
nicely; tool-result wording is part of the prompt; idempotency is not "exactly one match";
observability can be a projection of an audit ledger; evaluate the evaluator (the runner bug, the
judge's 5/5 on a false report); and tighten a case when it passes for the wrong reason.
Pointer: journal 08 §10.
