# Journal 14 — End-to-End Demo, Final Docs, Loop vs Graph Measured (Day 5, M14) + Day 5 Retrospective

Milestone: the last part of Day 5 in product.txt §18 — "end-to-end demo script, final
documentation, known limitations" with the deliverable "a person can run docker compose + ollama +
the UI and complete the demo flow" — plus product.txt §21 deliverable 11, "a comparison of custom
loop versus LangGraph orchestration", which had been deferred since Day 3 (the loop-mode baseline
was never run). Branch `day-5-ui-demo`, code and docs in commit `cff28ed` (14 Nov). Written by the
Recorder agent from `scripts/demo.sh`, `reports/demo-run.log`, `reports/loop.log`,
`reports/loop.sh`, `datasets/baselines/golden-v1-loop-qwen3-8b.json`,
`docs/benchmarks/loop-vs-graph.md`, `docs/demo.md` and the README; facts reported by the build
session and not visible in a file are marked as such.

Status (re-run by the Recorder on 14 Nov): **373 passed, 7 deselected** (`-W error`, Postgres
available); ruff, ruff format (132 files), mypy strict (98 files) clean; web: strict `tsc`, 6 vitest
tests. The Recorder did **not** re-run the demo or the loop evaluation (both need the live models).

Not in this milestone: a fresh graph run on the same evening as the loop run (graph numbers are the
committed Day 4 reports), constrained decoding or a verify step for the loop, a scripted UI demo.

---

## 1. What was built

### `scripts/demo.sh` (86 lines, bash + curl + python3)
The whole product through **public API endpoints only** — the same calls the UI makes. Env:
`AGENTOPS_URL` (default `http://localhost:8000`), `AGENTOPS_KEY` (default `dev-key-tenant-a`),
`APPROVE=1` to approve the write itself. `set -euo pipefail`; `curl --fail-with-body`.

| Step | What it calls | What it proves |
|---|---|---|
| 1 | `GET /ready` | database, Ollama and models are up |
| 2 | `POST /documents` per corpus file (skips `vendor-notice-*`, the injection fixture), polls until nothing is `pending`/`processing` | upload, duplicate detection, background indexing |
| 3 | `POST /agent/run` "What is the RPO in the latest payments architecture, in seconds?" | grounded, cited, verified graph answer |
| 4 | `GET /documents/{id}/chunks/{chunk_id}` (M12) | the citation opens its source passage |
| 5 | `GET /executions/{id}/trace` | the ledger: every step, model call and tool call |
| 6 | `POST /agent/run` "Create a new file notes/demo.md …" + `GET /approvals?status=pending` | the run pauses; nothing is written |
| 7 | `POST /approvals/{id}/approve` (only with `APPROVE=1`) + `GET /workspace/file` | resume from the Postgres checkpoint; the file is on disk |
| 8 | `POST /workspace/exec` × 3 | sandbox refusals |
| 9 | `GET /executions/{id}` as tenant B | tenant isolation (404) |
| 10 | `POST /monitoring/check` | drift check (operator) |

### `docs/demo.md` (105 lines)
A ~15-minute walkthrough of the UI: start the stack, upload the corpus, ask a cited question and
open the citation, ask the calculator question, switch to loop mode, request and review a workspace
write with the diff, try the terminal refusals, read a trace, compare the committed Day 4 runs
(87% → 95%), run a drift check, then switch to tenant B. Section 8 is the same flow from
`scripts/demo.sh`.

### README
"Web UI" section (stack, the thin-client rule, the proxy, a screen table, commands), the two M12
endpoints, two new Troubleshooting entries (port 8000 taken → `--port 8010` and
`AGENTOPS_API_URL`; `provider_unavailable` mid-run because macOS killed Ollama), and three new
Known limitations (UI: API key in local storage, one terminal command at a time, preview of the
first 20 KB, no mobile layout; an eval run killed with its process stays `running`; the drift
check's default retrieval head can be an experiment probe).

### Loop vs graph (`docs/benchmarks/loop-vs-graph.md`, `datasets/baselines/golden-v1-loop-qwen3-8b.json`)
`reports/loop.sh` ran `agentops eval run --mode loop --judge` against Ollama on :11435 from 00:45 to
01:06 local on 14 Nov (20:45–21:06 UTC on 13 Nov): run `bcb1b2d2`, prompt `agent-loop-v2`,
tuning switches at their defaults (`rule_routing: false`). The loop can't pause for a human, so the
5 approval cases (`adv-user-write`, `adv-shell`, `ws-edit-approved`, `ws-edit-rejected`,
`ws-run-tests`) are **skipped**; every
comparison is over the **same 33 cases** of the graph reports.

---

## 2. Files touched

New: `scripts/demo.sh`, `docs/demo.md`, `docs/benchmarks/loop-vs-graph.md`,
`datasets/baselines/golden-v1-loop-qwen3-8b.json` (3,508 lines).

Modified: `README.md`, `docs/day-checklist.md` (Day 5 section), `web/src/screens/Documents.tsx` and
`web/src/lib/api.ts` (upload response, §4.1).

Not in git: `reports/demo-run.log`, `reports/loop.log`, `reports/loop.sh`, `reports/eval-loop.json`.

---

## 3. Key design decisions (taken vs rejected)

| Taken | Rejected | Why |
|---|---|---|
| Demo through public endpoints only, with curl | A Python script importing the app | It proves the API a user gets, not internals; it doubles as an API usage example. |
| Approval off by default (`APPROVE=1` to approve) | Always auto-approve | The demo's point is that nothing is written until a human decides; auto-approve is opt-in. |
| Idempotent uploads in the demo | Wipe and re-seed the database | Re-running is safe and shows duplicate detection. |
| Compare loop and graph on the same 33 cases | Report loop over 38 with approval cases failed | The loop can't do approval by design; counting those as failures would measure the design, not the behaviour. |
| Report strict **and** lenient loop pass rates | Only strict (1/33) or only lenient (24/33) | Strict is what the gate measures; lenient isolates the one failure mode that dominates it. Hiding either would mislead. |
| Compare tokens and calls, not latency | Latency across evenings | Day 4 showed latency on this machine moves with swap (p50 60.6 s vs 43.1 s for identical runs). |
| Graph stays the API default | Make the loop default for fewer calls | 29–32/33 vs 1/33 strict and 24/33 lenient. |

---

## 4. Live results and what they found

### 4.1 The demo script, run live (`APPROVE=1`, `reports/demo-run.log`, API on 8010)
- `/ready`: database ok, Ollama ok.
- Uploads: the 4 Markdown files reported `ready (already uploaded)`; the list shows 5 documents
  (incl. `sla.pdf`), all `ready`.
- **Answer:** "The Recovery Point Objective (RPO) in the latest payments architecture is 5 minutes,
  which is equivalent to 300 seconds." `verification=supported`, 5 model calls, 4 tool calls,
  **38.2 s**.
- Citation opened: `payments-architecture-v2.md § Availability and Recovery | Data Retention |
  Migration Plan`, text containing "Recovery Point Objective (RPO): 5 minutes".
- Trace: **17 events**. The two big costs: `select_tools` on qwen3:8b **18.7 s** and
  `generate_answer` **12.7 s**; classify 2.2 s, plan 0.7 s, verify 3.2 s; three `document_search`
  calls ≤ 0.4 s; `document_metadata` after tool selection; no calculator call (the model converted
  minutes to seconds itself).
- Approval: `workspace_write {'path': 'notes/demo.md', 'content': 'written by the AgentOps demo'}`
  paused → approved → "The file notes/demo.md was created in your workspace … [T1]" → the file
  reads back.
- Sandbox: `rm -rf calc` → `command_not_allowed`, `cat ../../.env` → `workspace_access_denied`,
  `ls | wc -l` → `invalid_request`.
- Tenant B on tenant A's execution → **HTTP 404**.
- Drift check: `status: drift, alerts: 1` — `retrieval answer_coverage drift`, `data
  ingestion_failure_rate insufficient_data`.

**Bug found by the demo:** `POST /documents` returns `UploadResponse {document, duplicate}`
(`app/api/documents.py`, since Day 1's duplicate detection). The first version of the script — and
the UI's `upload` type — assumed a bare document. Both were fixed; the script prints
`(already uploaded)` and the Documents screen says "is already uploaded (identical content): not
indexed again" (`reports/ui-vite.log` 01:08). The API had always been right; two clients written
from memory of it were wrong.

**Two honest readings of the demo output (Recorder):**
1. The **drift** status is the documented limitation, not a regression: the check's default
   "current" retrieval report is the latest file, which on this machine is the Day 4
   `degraded-chunking` probe (answer coverage 0.971 → 0.882). `docs/demo.md` says so; the README
   lists it under Known limitations.
2. The demo runs the **server defaults**, which are the Day 3 behaviour: the accepted Day 4
   `optimized` profile (rule routing + caches) is a profile, not the default
   (`agent_rule_routing: bool = False`). That's why a plain lookup still spends 18.7 s of 38.2 s in
   `select_tools` — the call rule routing removes. Turning it on is one setting
   (`AGENTOPS_AGENT_RULE_ROUTING=true`).

### 4.2 Loop vs graph, same 33 cases

| | Loop | Graph baseline | Graph optimized |
|---|---|---|---|
| Pass rate, all evaluators | **1/33** | 29/33 | 32/33 |
| Pass rate without `structured_output` | **24/33** | 29/33 | 32/33 |
| answerable | 10/11 | 11/11 | 11/11 |
| multi_document | 4/5 | 4/5 | 4/5 |
| unanswerable | 3/5 | 5/5 | 5/5 |
| tool_use | 1/5 | 2/5 | 5/5 |
| adversarial | 5/5 | 5/5 | 5/5 |
| workspace (read-only) | 1/2 | 2/2 | 2/2 |
| Model calls / request | 3.64 | 4.76 | 4.45 |
| Tokens / request | 6,207 | 4,304 | 3,328 |
| Median latency | 31.1 s | 45.5 s | 29.2 s |

(Loop category rows use the lenient count. Graph figures are the committed Day 4 reports restricted
to the same 33 cases.)

What the run shows (`reports/loop.log`, the JSON aggregate):
- **`structured_output` fails 31/33.** Per the benchmark write-up, qwen3:8b answered in plain prose
  after searching instead of calling `final_answer`. The loop refuses prose by design (Day 2: an
  answer is a tool call or a validated final answer, never parsed text — interview 03 Q11), sends a
  correction, and the model then calls `final_answer`. 23 runs needed one invalid output corrected,
  8 needed two. The strict evaluator measures "valid on the first try" (interview 08 Q16), so all 31
  fail. The graph never hits this: each node decodes against a JSON schema, so the model can't emit
  anything else.
- **The only strict pass was `un-weather`** (4.5 s, `insufficient_evidence`).
- **Safety held**: `unauthorized_tools` 5/5, `citation_validity` 23/23.
- **Unanswerable 3/5**: `un-mobile` and `un-cloud-bill` ended `failed_validation` instead of
  `insufficient_evidence` — the loop has no verify-and-revise step to recover a bad draft.
- **Tool use 1/5**: the model did the arithmetic itself in four cases (calculator never returned
  300, 2.5, 300); `tool-rto-diff` ran out of steps (`budget_exhausted`, 8/8). The graph's Day 4 rule
  routing + calculator nudge is what took it to 5/5; the loop has neither.
- **`ans-promote-cmd`** ended `insufficient_evidence` in 7.5 s without retrieving the runbook — the
  model chose its own search and stopped early.
- **Fewer calls, more tokens.** 3.64 model calls/request vs 4.76 (no classify, plan or verify), but
  **6,207 tokens/request vs 4,304 (+44%) and 3,328 (+87%)**: input tokens are 198,830 of 204,819
  (97%), because every turn resends the conversation so far.
- **Judge (advisory)**: correctness 0.88, groundedness 0.787 over 27 judged cases, vs 0.99 / 0.95 for
  the graph (over all 38).
- **Latency is not comparable**: the loop's 31.1 s median comes from a different evening than the
  graph's 45.5 s and 29.2 s.
- **A metric that breaks down**: `tokens_per_successful_task` is **204,819** for the loop because
  only one case passed strictly. Per-success metrics need the pass definition next to them.

---

## 5. How it was verified

- `reports/demo-run.log`: one live `APPROVE=1` run end to end.
- `reports/loop.log` + `datasets/baselines/golden-v1-loop-qwen3-8b.json`: one live loop run with
  the judge; the Recorder recomputed the lenient count from the failure list (9 cases fail on
  something other than `structured_output` → 24/33) and the correction counts (23 × 1, 8 × 2).
- The graph side is the committed, already-gated Day 4 reports (journal 09).
- Test suite, lint and types re-run by the Recorder (Status). The demo itself has no automated test.

---

## 6. Commands

```bash
# Demo (API on 8010 because 8000 was taken)
uv run uvicorn --factory app.main:create_app --port 8010
AGENTOPS_URL=http://localhost:8010 APPROVE=1 scripts/demo.sh | tee reports/demo-run.log
# Loop evaluation (Ollama with the qwen3 models on 11435)
export AGENTOPS_OLLAMA_BASE_URL=http://localhost:11435
uv run agentops eval run --mode loop --judge --out reports/eval-loop.json
```

---

## 7. What this milestone taught

1. **A demo script is an integration test written for a person.** Its first run found a client bug
   (`{document, duplicate}`) in two places the backend tests could never see.
2. **Constrained decoding is a reliability feature, not just a format.** The same model, prompt
   family and tools: the graph's schema-constrained nodes never produced prose; the free loop did in
   31 of 33 runs.
3. **Report the strict and the lenient number together.** 1/33 alone says "broken"; 24/33 alone
   hides a correction on nearly every answer. Both are true.
4. **Fewer calls is not cheaper.** The loop saves calls and spends 44–87% more tokens, because
   history grows every turn. Count tokens.
5. **Fixed steps carry the wins.** Every category where the graph beats the loop maps to a node the
   loop lacks: verify-and-revise (unanswerable), rule routing and the calculator nudge (tool use).
6. **Read the demo's output, not just its exit code.** "drift" was a stale probe report; 18.7 s was
   a call the accepted optimization removes but the defaults keep.

---

## 8. Known limitations and open findings (Recorder review)

1. **The accepted `optimized` profile is not the server default.** The API, UI and demo run with
   rule routing off. Either make it the default (it passed the gate: 36/38 vs 33/38) or say in the
   demo how to turn it on. *Resolved after review:* defaults stay at the baseline on purpose (each
   benchmark measures one switch); the README now names the three settings that enable it.
2. **The drift check in the demo reports drift from a probe.** Pass the retrieval reports
   explicitly in the demo, or make the default head the latest non-experiment report.
3. **The README does not link `docs/benchmarks/loop-vs-graph.md`** (§21 deliverable 11); only the
   checklist does. *Resolved after review:* linked from the README's Optimization section.
4. **One loop run, one evening.** At temperature 0 that is behaviour, not a sample (journal 09), but
   the graph rows are from other evenings and the loop's latency can't be compared.
5. **The loop has no constrained final answer and no verify step**; its numbers describe this
   implementation, not "loops" in general.
6. **The demo script has no assertions**: it prints results; a wrong answer exits 0. Only HTTP
   errors fail it (`--fail-with-body`). *Resolved after review:* seven `check` assertions (answered,
   cited, file written after approval, three sandbox refusal codes, tenant_b 404); any failure exits
   1. Re-run live: all seven ok. On a re-run `notes/demo.md` already exists, so that check alone
   cannot tell a fresh write from the earlier one; the resumed answer reports the write.
7. **No compose-only path verified for the demo**: the live run used a native uvicorn on 8010 and
   native Ollama on 11435, not `docker compose --profile app` (journal 11: the container needs
   Ollama bound beyond loopback).

---

## 9. What I'd do differently at 100x scale

- Make the demo a **smoke test with assertions** (status, citation file, refusal codes, 404) that
  runs after each deploy against a fake-LLM stack and nightly against the real models.
- Run the **loop and graph comparison in the nightly benchmark** with both modes back to back on
  fixed hardware, so latency becomes comparable.
- Give the loop **schema-constrained `final_answer`** and a verify step before using it anywhere
  near production; keep it as the exploration path for multi-hop questions.
- Ship the **accepted profile as the default** through a config release with the benchmark attached,
  rather than leaving it as an opt-in.

---

## 10. Day 5 retrospective (M12 → M14)

Day 5 was planned for Sun 25 Oct and ran on 13–14 Nov (the evening after Day 4 finished): the UI
session started around 00:35 local on 14 Nov per `reports/ui-api.log`, the loop evaluation ran
00:45–01:06, and the demo run finished around 01:09.

### What Day 5 delivered
- **M12 — API for the UI** (journal 12): `GET /executions`, `GET /documents/{id}/chunks/{chunk_id}`
  (404 across tenants), operators see every evaluation run; 3 integration tests.
- **M13 — web UI** (journal 13): six §24 screens, SSE over POST, citation drawer, approval diff,
  sandbox terminal, operator screens with 403 explanations; 6 unit tests, strict types, 193 KB
  (60 KB gzip) bundle; CI `web` job. Driven end to end in Chrome against the live models.
- **M14 — demo and comparison** (this journal): `scripts/demo.sh` run live, `docs/demo.md`, README
  updates, and the loop-vs-graph measurement owed since Day 3.
- Tests: **373** backend (Day 4 ended at 370), `-W error`, ruff, mypy strict; web tsc + 6 tests.

### The mistakes, in order, and how each was caught
| # | Mistake | Caught by | Fix |
|---|---|---|---|
| 1 | Store method named `list` shadowed the builtin; `list[...]` annotations broke import | the app failing to start | renamed `recent` |
| 2 | Terminal effect returned a non-function cleanup → whole app blank | driving the Workspace screen | explicit effects + per-screen error boundary |
| 3 | StrictMode double mount leaked "Terminal closed (1006)" | driving it in dev | detach handlers before close |
| 4 | UI read `usage.total_tokens`, a Python-only `@property` | wrong token total on screen | sum input + output |
| 5 | Monitoring stats read the wrong nested keys | empty stats on screen | types aligned with the summary |
| 6 | Operator run list filtered by tenant; CLI runs invisible | empty Evaluations screen with real data | `None` filter for operators + regression test |
| 7 | Script and UI assumed upload returns a bare document | the demo's first live run | read `{document, duplicate}`; "already uploaded" |

Two incidents were the machine, not the code: port 8000 owned by another project's server (API on
8010), and macOS killing Ollama under memory pressure mid-session (resumed run →
`provider_unavailable`; restarted; README entry).

Pattern, the same for the fifth day running: every bug that mattered was found by **using the
system with real data** — a screen, a script, a live run — and each fix that touched the backend
got a regression test.

### What the live runs taught
1. **The reviewer's diff is the last line of defence for a plausible bad edit**: the duplicated
   `return` line passed every server check.
2. **Constrained decoding is why the graph is reliable** on an 8B local model: 1/33 vs 29/33 strict
   on the same cases, and the loop's lenient 24/33 still trails where the graph has a dedicated step.
3. **Token cost follows history length, not call count** (+44% tokens with −24% calls).
4. **A 16 GB machine is a production-shaped environment**: memory pressure killed the model server,
   and the API reported exactly that.

### Day 3/4 open items closed on Day 5
Loop-mode baseline (measured, finally); SSE handled in the UI (but still no reconnect);
`GET /executions` list; approval flow usable by a person (UI with diff); an end-to-end demo.

### Still open after Day 5 (from `docs/day-checklist.md` and the reviews)
- `multi-retention` and `ws-edit-approved` still fail in the graph; the accepted profile isn't the
  default.
- Durable job queue (indexing, evals; an eval run killed with its process stays `running`),
  approval expiry, synchronous approve, SSE reconnect, workspace not an OS sandbox.
- Rate limits and caches per process; terminal not rate limited; caches unmeasured live.
- CI gates committed reports only (no replay); no browser tests; the demo has no assertions.
- Drift: default retrieval head can be a probe; alerts can't be acknowledged; no scheduled checks.
- UI: API key in local storage, one terminal command at a time, no mobile layout, hand-written types.

### Commit state
Day 3 and Day 4 were committed after the fact as separate, back-dated commits: `468a3c2` (Day 3,
26 Oct — **reconstructed by stripping the Day 4 changes** from the combined working tree and
verified at that commit with 323 tests, ruff and mypy) and `4e28591` (Day 4, 13 Nov), each followed
by its Recorder docs commit. Day 5's code was committed as `cff28ed` on 14 Nov; these Recorder
documents (journals and interviews 12–14, the consolidated guide and the five-day journal) follow
in their own docs commit, after a fix commit for the review findings marked resolved in §8.
