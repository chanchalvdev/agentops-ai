# Custom agent loop vs LangGraph workflow

Golden set `golden-v1`, `qwen3:8b` + `qwen3:1.7b`, `nomic-embed-text`, Apple M4 / 16 GB,
temperature 0. Reports: `datasets/baselines/golden-v1-loop-qwen3-8b.json` (loop, prompt
`agent-loop-v2`, run 13 Nov 2026 20:45 UTC), `golden-v1-graph-qwen3-8b-day4.json` (graph
baseline) and `golden-v1-graph-qwen3-8b-optimized.json` (graph, accepted Day 4 profile).

```bash
uv run agentops eval run --mode loop --judge --out reports/eval-loop.json
```

The loop cannot pause for a human (it has no checkpoint to resume from), so it never gets the
tools that need approval. The 5 cases that require an approval are skipped in loop mode; every
number below compares the **same 33 cases**.

## Results

| | Loop | Graph baseline | Graph optimized |
|---|---|---|---|
| Pass rate, all evaluators | **1/33** | 29/33 | 32/33 |
| Pass rate without `structured_output` | **24/33** | 29/33 | 32/33 |
| answerable | 10/11 | 11/11 | 11/11 |
| multi_document | 4/5 | 4/5 | 4/5 |
| unanswerable | 3/5 | 5/5 | 5/5 |
| tool_use | 1/5 | 2/5 | 5/5 |
| adversarial | 5/5 | 5/5 | 5/5 |
| workspace (read-only cases) | 1/2 | 2/2 | 2/2 |
| Model calls / request | 3.64 | 4.76 | 4.45 |
| Tokens / request | 6,207 | 4,304 | 3,328 |
| Median latency | 31.1 s | 45.5 s | 29.2 s |
| Judge correctness / groundedness (advisory; graph over all 38 cases) | 0.88 / 0.79 | 0.99 / 0.95 | 0.99 / 0.95 |

Category rows for the loop use the lenient count (without `structured_output`). Latency comes from
runs on different evenings and this machine's latency varies with memory pressure (see
`day4.md`); tokens and call counts are the comparable signal.

## What the numbers say

- **The loop needs a correction on almost every answer.** In 31 of 33 runs, qwen3:8b answered
  in plain prose after searching instead of calling `final_answer`. The loop refuses prose by
  design (an answer is either a tool call or a validated final answer, never parsed text), sends
  one correction, and the model then calls `final_answer` correctly. The strict
  `structured_output` evaluator ("valid on the first try") fails all of those runs. The graph
  never has this problem: each of its steps uses JSON-schema-constrained decoding, so the
  model cannot produce anything but the schema.
- **Without that evaluator the loop is close on plain retrieval** (10/11 answerable, 4/5
  multi-document, 5/5 adversarial) but **weaker where the graph has a dedicated step**:
  - unanswerable 3/5: two runs ended in `failed_validation` instead of reporting insufficient
    evidence, because the loop has no separate verify-and-revise step to recover;
  - tool use 1/5: the model did the arithmetic itself in 4 of 5 cases (one ran out of steps);
    the graph's rule routing and calculator nudge (Day 4) are what raised it to 5/5.
- **Fewer calls, more tokens.** The loop makes fewer model calls (no classify, plan, or verify),
  but each call carries the whole conversation so far, so it spends 44% more tokens per request
  than the graph baseline and 87% more than the optimized graph.

## When to use which

- **Graph** when steps have a fixed order, a run must pause for a human (workspace writes), each
  step needs its own model and test, or answers must be verified. That is every production path
  here, which is why it is the API default.
- **Loop** to learn and demonstrate the mechanics (bounded steps, repeated-call refusal, budget
  enforcement in the harness), and for open-ended tool sequences where the order is not known in
  advance. It would need constrained decoding for its final answer and a verify step before it
  could match the graph's reliability.
