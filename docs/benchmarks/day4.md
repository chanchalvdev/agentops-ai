# Day 4 benchmark: baseline vs optimized

Golden set `golden-v1` (38 cases), LangGraph mode, `qwen3:8b` (main) + `qwen3:1.7b` (small),
`nomic-embed-text`, Apple M4 / 16 GB, Ollama 0.40.7, temperature 0. Reproduce:

```bash
uv run agentops experiment run baseline  --kind agent --judge --out reports/bench-baseline.json
uv run agentops experiment run optimized --kind agent --judge --out reports/bench-optimized.json
uv run agentops experiment benchmark reports/bench-baseline.json reports/bench-optimized.json
```

The table below uses the back-to-back baseline run (`reports/bench-baseline-rerun.json`, committed
as `datasets/baselines/golden-v1-graph-qwen3-8b-day4.json`).

**Result: `optimized` is accepted.** 36/38 vs 33/38, no newly failing case, judge scores unchanged,
21% fewer tokens per request, 9% fewer `qwen3:8b` calls (3.03 → 2.76 per request), median latency
43.1 s → 27.5 s (−36%), p95 65.4 s → 60.0 s (−8%).

## Results

| profile | success | quality (judge) | p50 s | p95 s | model calls/req | tool calls/req | tokens/req | tokens/success | cost/req | cost/success |
|---|---|---|---|---|---|---|---|---|---|---|
| baseline | 87% | 0.969 | 43.1 | 65.4 | 4.7 | 2.8 | 4,110.8 | 4,733.7 | $0.000 | $0.000 |
| optimized | 95% | 0.969 | 27.5 | 60.0 | 4.5 | 2.5 | 3,263.4 | 3,444.7 | $0.000 | $0.000 |
| optimized-no-rewrite | 82% | 0.953 | 31.4 | 58.8 | 3.7 | 1.1 | 2,780.1 | 3,407.8 | $0.000 | $0.000 |
| degraded-chunking | 82% | 0.998 | 40.3 | 60.2 | 4.8 | 2.7 | 3,213.5 | 3,939.1 | $0.000 | $0.000 |

Verdicts against `baseline`:
- **optimized**: SUCCESS (p50 -36%, p95 -8%, tokens_per_request -21%, model_calls_per_request -6%); quality breaches: none; newly failing: none; newly passing: ['tool-rpo-seconds', 'tool-rto-diff', 'tool-settlement-seconds']
- **optimized-no-rewrite**: NOT ACCEPTED (p50 -27%, p95 -10%, tokens_per_request -32%, model_calls_per_request -22%); quality breaches: ['pass rate dropped -5.26% (limit -5%)', 'category multi_document dropped -40.00%', 'evaluator answer_content dropped -13.80% (limit -10%)', 'evaluator required_citations dropped -18.75% (limit -10%)', "newly failing cases: ['ans-rpo-v2', 'multi-rpo-change', 'multi-availability', 'adv-injected-rpo']"]; newly failing: ['ans-rpo-v2', 'multi-rpo-change', 'multi-availability', 'adv-injected-rpo']; newly passing: ['tool-rto-diff', 'tool-settlement-seconds']
- **degraded-chunking**: NOT ACCEPTED (p50 -7%, p95 -8%, tokens_per_request -22%, model_calls_per_request 2%); quality breaches: ['pass rate dropped -5.26% (limit -5%)', 'evaluator answer_content dropped -13.80% (limit -10%)', 'evaluator expected_behavior dropped -12.12% (limit -10%)', 'evaluator required_citations dropped -12.50% (limit -10%)', "newly failing cases: ['ans-rpo-v2', 'ans-escalation', 'multi-failover']"]; newly failing: ['ans-rpo-v2', 'ans-escalation', 'multi-failover']; newly passing: ['multi-retention']

Local models cost $0 per token (pricing source `local`); tokens and latency are the real cost on this hardware. Hosted-equivalent cost is reported only when reference prices are configured, and is labelled hypothetical.

## How to read these numbers

- **Quality is deterministic, latency is not.** Decoding runs at temperature 0: the baseline,
  re-run 23 hours later, produced identical answers, tokens, and judge scores. A quality
  difference between two profiles is therefore a real behavioural difference, not sampling noise.
  Latency depends on machine state: the same baseline measured p50 60.6 s one evening and 43.1 s
  the next day (swap was 7–11 GB with another project's containers running). `baseline` and
  `optimized` above ran back to back (18:26 and 17:56 on 13 Nov); `optimized-no-rewrite` and
  `degraded-chunking` ran on 12 Nov, so compare their latency with care. Tokens and call counts
  are the machine-independent signal.
- **Cost is $0** (local models). Hosted-equivalent cost appears only with configured reference
  prices; none were configured, so none is reported.
- **Caches did not contribute here**: each golden question runs once, so tool-result and
  embedding caches had ~0 hits. Their benefit is for repeated questions and is covered by tests,
  not measured in this benchmark.

## What changed, and what was measured and rejected

| Change | Effect |
|---|---|
| Rule routing: plain lookups skip the `select_tools` qwen3:8b call | fewer main-model calls and tokens |
| Calculator nudge when a calculation question got no calculator call | tool_use 2/5 → **5/5** |
| `insufficient_evidence` drafts return fixed text | no unverified claims (Day 2/3 deferral) |
| Query rewrite **off** (`optimized-no-rewrite`) | **rejected**: multi_document 4/5 → 2/5. "RPO" in the question does not retrieve the "Availability and Recovery" passages; the rewrite expands it. This run also had the earlier routing that skipped comparisons, so its `multi-availability` failure is partly that bug; `ans-rpo-v2`, `multi-rpo-change` and `adv-injected-rpo` are the rewrite |
| Rule routing also skipping comparisons (first `optimized` run, `reports/bench-optimized-v1routing.json`) | **fixed**: `multi-availability` failed 3/3 (baseline 3/3 pass). Comparisons now keep `select_tools`, which adds `document_metadata` so the model knows which passage is v1 and which v2 |
| Relevance floor on vector score | **not adopted**: unanswerable top score up to 0.766, answerable down to 0.651, the ranges overlap |

## Retrieval probes (no model, k = 5, 21 cases)

| profile | hit@k | recall@k | MRR | answer coverage@k |
|---|---|---|---|---|
| baseline (hybrid) | 1.0 | 1.0 | 0.952 | 0.971 |
| vector-only | 1.0 | 1.0 | 0.921 | 1.0 |
| keyword-only | 1.0 | 1.0 | 0.921 | 0.971 |
| degraded-chunking (250-char fixed, vector) | 1.0 | 1.0 | 1.0 | **0.882** |

File-level metrics saturate on this 5-document corpus; answer coverage (answer strings found in
retrieved passages *from the expected documents*) is what separates the degraded configuration.
The probe uses the raw question, without the agent's query rewrite, which is why baseline coverage
is below 1.0 (`multi-rpo-change`).

## Still failing in `optimized`

- `multi-retention`: insufficient evidence (also fails in the baseline).
- `ws-edit-approved`: the model writes "could not" after a successful retried edit (also baseline).
