# Interview Prep 14 — End-to-End Demo and Loop vs Graph (Day 5, M14)

Every answer is grounded in branch `day-5-ui-demo` (commit `cff28ed`), `scripts/demo.sh`,
`reports/demo-run.log`, `reports/loop.log`, `datasets/baselines/golden-v1-loop-qwen3-8b.json` and
`docs/benchmarks/loop-vs-graph.md`. Format: direct answer, then the reasoning, then a pointer.
Companion: `docs/journal/14-demo-loop-vs-graph.md` (includes the Day 5 retrospective).

---

## Basic

### Q1. What does the demo script do?
**Answer:** Ten steps through public endpoints only: readiness, idempotent corpus upload, a cited
graph answer, open its source passage, print its trace, request a workspace write (pauses),
optionally approve it (`APPROVE=1`) and read the file back, three sandbox refusals, tenant B
getting 404 on tenant A's execution, and a drift check.
Pointer: `scripts/demo.sh`.

### Q2. What did the live demo run produce?
**Answer:** "5 minutes, which is equivalent to 300 seconds", `verification=supported`, 5 model
calls, 4 tool calls, 38.2 s; the cited chunk from `payments-architecture-v2.md`; a 17-event trace;
the write paused, was approved and is on disk; `command_not_allowed`, `workspace_access_denied`,
`invalid_request`; tenant B → 404; drift check → `drift`, 1 alert.
Pointer: `reports/demo-run.log`.

### Q3. What were the loop-vs-graph results?
**Answer:** On the same 33 cases: loop 1/33 strict and 24/33 without `structured_output`; graph
29/33 (Day 4 baseline) and 32/33 (optimized). Loop 3.64 model calls and 6,207 tokens per request
vs graph 4.76 / 4,304 (baseline) and 4.45 / 3,328 (optimized).
Pointer: `docs/benchmarks/loop-vs-graph.md`.

### Q4. Why 33 cases, not 38?
**Answer:** The loop has no checkpoint to resume from, so it is never offered tools that need
approval. The 5 approval cases (`adv-user-write`, `adv-shell`, `ws-edit-approved`,
`ws-edit-rejected`, `ws-run-tests`) are skipped in loop mode, and the graph numbers are restricted
to the same 33.
Pointer: `reports/loop.log` (`skip` lines).

---

## Logical

### Q5. Why is the loop's strict pass rate 1/33?
**Answer:** In 31 of 33 runs qwen3:8b answered in prose after searching instead of calling
`final_answer`. The loop never parses prose as an answer, so it sends a correction and the model
then calls `final_answer` properly — 23 runs needed one correction, 8 needed two. The
`structured_output` evaluator measures "valid on the first try", so all 31 fail it.
Pointer: `reports/loop.log`; `app/evaluation/evaluators.py:structured_output`.

### Q6. Why doesn't the graph have that problem?
**Answer:** Each graph node calls the model with a JSON schema and Ollama decodes against it, so
the model can't emit prose where a structure is expected. The loop lets the model choose between a
tool call and text, and an 8B model often chooses text.
Pointer: `app/agents/nodes.py` (`GroundedDraft` and per-node schemas).

### Q7. Ignoring that evaluator, where does the loop still lose?
**Answer:** Unanswerable 3/5 — two runs ended `failed_validation` instead of
`insufficient_evidence`, because there's no verify-and-revise step. Tool use 1/5 — the model did the
arithmetic itself four times and one run ran out of steps; the graph's calculator nudge took it to
5/5. On plain retrieval it's close: answerable 10/11, multi-document 4/5, adversarial 5/5.
Pointer: `docs/benchmarks/loop-vs-graph.md` "What the numbers say".

### Q8. The loop makes fewer model calls. Why does it use more tokens?
**Answer:** Each loop turn resends the whole conversation, including every tool result so far.
Input tokens were 198,830 of 204,819 (97%). The graph's nodes each get a focused prompt. So 3.64
calls cost 6,207 tokens/request vs 4.76 calls at 4,304 — +44%, and +87% against the optimized graph.
Pointer: loop baseline JSON `aggregate.tokens`.

### Q9. Why not compare latency?
**Answer:** The runs came from different evenings, and on this 16 GB machine latency moves with
swap — Day 4 measured identical baseline runs at p50 60.6 s and 43.1 s. Tokens and calls are the
machine-independent signal.
Pointer: `docs/benchmarks/day4.md` "How to read these numbers".

---

## Advanced

### Q10. What bug did the demo find?
**Answer:** `POST /documents` returns `{document, duplicate}`; the first version of the script and
the UI's upload type both assumed a bare document. The API was right; two clients written from
memory were wrong. Both fixed — the script prints "(already uploaded)", the UI says "already
uploaded (identical content): not indexed again".
Pointer: `app/api/documents.py:UploadResponse`; `scripts/demo.sh` step 2.

### Q11. The demo's drift check says `drift`. Is something broken?
**Answer:** No. The check's default "current" retrieval report is simply the latest file, which on
this machine is the Day 4 `degraded-chunking` probe — answer coverage 0.971 → 0.882. It's a
documented limitation; for a clean comparison you pass the reports explicitly.
Pointer: README "Known limitations"; `docs/demo.md` §7.

### Q12. Why did a plain lookup spend 18.7 s in `select_tools`?
**Answer:** The demo runs the server defaults, which are Day 3 behaviour. Rule routing — which skips
that call on plain lookups — is in the accepted `optimized` profile but off by default
(`agent_rule_routing = False`). It's one setting, and arguably should be the default since it passed
the gate 36/38 vs 33/38.
Pointer: `app/core/config.py`; `datasets/experiments.json`; trace in `reports/demo-run.log`.

### Q13. `tokens_per_successful_task` for the loop is 204,819. What does that tell you?
**Answer:** That per-success metrics are only as meaningful as the success definition: with one
strict pass, all tokens are divided by one. Report it next to the pass definition, or compute it on
the lenient definition as well.
Pointer: loop baseline JSON `aggregate.tokens_per_successful_task`.

---

## Scenario

### Q14. "Should we switch the API default to the loop? It makes fewer calls."
**Answer:** No. It's 1/33 strict and 24/33 lenient vs 29–32/33, spends 44–87% more tokens, can't
pause for approval, and has no verification. The loop is for learning the mechanics and for
open-ended multi-hop questions; it would need constrained decoding for `final_answer` and a verify
step first.
Pointer: `docs/benchmarks/loop-vs-graph.md` "When to use which".

### Q15. "Can someone new run the demo?"
**Answer:** Yes with `docs/demo.md`: Postgres in Docker, Ollama with three models, the API, `npm run
dev`, and the dev key; about 15 minutes, mostly model time. Pitfalls are in Troubleshooting: port
8000 taken (use 8010 and `AGENTOPS_API_URL`), another Ollama on 11434, and Ollama killed under memory
pressure. The compose-only path (API in Docker) wasn't used for the live demo.
Pointer: `docs/demo.md`; README "Troubleshooting".

### Q16. "The demo script exited 0. Did it work?"
**Answer:** Only HTTP errors fail it (`set -e`, `curl --fail-with-body`); it prints answers but
asserts nothing. You read the output: answer, verification, refusal codes, the 404. Making it a
smoke test with assertions is the next step.
Pointer: `scripts/demo.sh`.

---

## Trade-off

### Q17. Strict vs lenient reporting for the loop?
**Answer:** Both. Strict (1/33) is what the gate measures and it's true that 31 runs needed a
correction. Lenient (24/33) isolates that single failure mode and shows the remaining real gaps.
Either alone misleads.

### Q18. Hand-written loop vs LangGraph: what did five days show?
**Answer:** The loop taught the mechanics and forced shared, framework-free pieces (harness,
evidence store, result, ledger). The graph won on every measure that matters here: approvals with
checkpoints, per-node models and schemas, a verify step, and 29–32/33 vs 24/33. Day 2's small live
comparison pointed the same way (graph 4/4; loop omitted "300 seconds" 2/4).
Pointer: journal 04 §5; `docs/benchmarks/loop-vs-graph.md`.

---

## Justification

### Q19. "Why publish a 1/33 result?"
**Answer:** Because it was measured, it explains a real design point (constrained decoding), and the
lenient figure next to it keeps it fair. The Day 3 deferral was "loop baseline not run"; running it
and reporting it honestly closes that.

### Q20. "Why not fix the loop before measuring it?"
**Answer:** The comparison is of the two designs as built; changing the loop to look better would
measure a third design. The fixes it would need are listed.

---

## System design

### Q21. Walk through the demo's write request end to end.
**Answer:** `POST /agent/run` → graph classifies a workspace task, the model proposes
`workspace_write`, the harness records a pending approval, the graph interrupts with a Postgres
checkpoint, the API returns `awaiting_approval`. `POST /approvals/{id}/approve` claims it with a
conditional UPDATE, resumes from the checkpoint, the write runs through the path jail with a
write-ahead receipt, the answer cites the receipt as `[T1]`, and `GET /workspace/file` reads it back.
Pointer: `scripts/demo.sh` steps 6–7; `app/agents/graph.py:resume`.

### Q22. Summarize Day 5 in one minute.
**Answer:** Two API endpoints and an operator fix the UI needed; a React UI with SSE over POST,
citation drawer, approval diff and sandbox terminal, driven end to end in Chrome — which caught a
bad agent edit and five UI/API bugs; a demo script that ran live and found one more; and the
loop-vs-graph comparison: 1/33 strict, 24/33 lenient vs 29–32/33, +44% tokens. 373 tests. Open: the
optimized profile isn't the default, no browser tests, demo without assertions.
Pointer: journal 14 §10.
