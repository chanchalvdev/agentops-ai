# Use cases

AgentOps AI is a self-hosted assistant that answers questions from your own documents with
citations you can check, and that can work on files in a sandboxed workspace only with a human's
approval. Everything runs on local models, so documents never leave the machine.

Each use case below lists who it is for, how to do it (web UI, CLI, or API), and what the platform
guarantees while doing it. All examples use the sample corpus in [datasets/corpus/](../datasets/corpus/)
(a fictional payments team's architecture docs, a database failover runbook, an ADR, an SLA PDF,
a vendor notice) and the dev key `dev-key-tenant-a`.

Setup for every example: [README → Quick start](../README.md#quick-start).

---

## 1. Ask an internal knowledge base, get answers you can verify

**Who**: engineers, support, on-call staff who search wikis, runbooks, and design docs.

**Example**: "What is the RPO in v2 and how does it differ from v1?"

- **UI**: *Ask*, type the question. Steps stream live (classify, plan, search, answer, verify).
  The answer cites `[S1]`, `[S2]`; click one to open the exact passage, file, and section.
- **CLI**: `uv run agentops run "What is the RPO in v2 and how does it differ from v1?" --trace`
- **API**: `POST /api/v1/agent/run/stream` (SSE) or `POST /api/v1/agent/run`.

**Guarantees**
- Every claim cites a passage that was actually retrieved; a second model call checks that the
  cited text supports the answer (`verification: supported`).
- If the documents do not contain the answer, it says so instead of guessing
  (`insufficient_evidence`). Try: "What is the CEO's favourite colour?"
- Only the latest version of each document is searched; superseded versions are kept but not used.

## 2. Compare document versions and find conflicts

**Who**: architects, compliance, anyone reviewing a change between two versions of a policy or design.

**Example**: "What data retention conflict exists between payments architecture v1 and v2?"
→ v1 keeps transaction records 7 years for audit; v2 keeps 5 years in the primary store, which
conflicts with the 7-year audit requirement (cited to both documents).

**How it works**: query rewriting searches for the facts on both sides; the answer prompt states
compared facts side by side; if a claim cites the wrong passage, verification rejects it and the
revision step re-cites the passage that states it.

## 3. On-call runbook assistant with calculations

**Who**: SREs during an incident.

**Examples**
- "What alert fires when the ledger primary is unreachable, and what command promotes the standby?"
- "What is the RPO in the latest payments architecture, in seconds?" (calculator tool: 5 min → 300 s)
- "Who is notified when authorization errors exceed 2%?"

**Guarantees**: arithmetic goes through a calculator tool, never the model's head; every run has
a deadline, step, token, and tool-call budget enforced in code, so a stuck run stops cleanly
(`deadline_exceeded`, `budget_exhausted`) with the sources it had gathered so far.

## 4. Let an agent change files, with a human countersigning every write

**Who**: teams that want help with small code or doc edits but cannot allow unsupervised writes.

**Example**: "Add a function `minutes_to_seconds(minutes)` that returns `minutes * 60` to the end of
`calc/mathlib.py` in my workspace."

1. The agent reads the file and proposes an edit; the run **pauses** (`awaiting_approval`).
2. *Approvals* shows the diff against the current file. Approve or reject with a reason
   (CLI: `agentops approvals`, `agentops approve <id>`; API returns 202 and the run resumes in the
   background).
3. On approval the run resumes from its Postgres checkpoint, performs exactly that write, and
   reports the outcome per file, computed from the tool results.

**Guarantees**
- Nothing is written before a human decides; a rejected write is never executed.
- Approvals survive a server restart; undecided ones expire after 24 h; a decision whose resume
  died with its process is reopened, and the write is idempotent (it cannot happen twice).
- Every operation, including refusals, is in the workspace audit log (*Workspace → Audit*).

## 5. Run tests and inspect a repository in a sandbox

**Who**: developers using the agent as a pair for a small workspace.

**Examples**: "Run the tests in my workspace and tell me if they pass", "List the functions in
calc/mathlib.py", "Search my workspace for TODO".

- **UI**: *Workspace* has a file tree, editor, and terminal (`ls`, `cat`, `grep`, `find`,
  `git status/diff/log`; `pytest`, `git add`, `mkdir` need approval when the agent asks).

**Guarantees**
- No shell: argv only, per-command option allowlist (`find -exec`, `grep -f`, `git diff --output`
  are refused), path arguments jailed to the tenant directory, secrets files (`.env`, keys) denied.
- With `AGENTOPS_WORKSPACE_EXEC_BACKEND=docker`, commands run in a throwaway container: no
  network, no capabilities, read-only root filesystem, resource limits, non-root user.

## 6. Resist prompt injection in documents

**Who**: anyone indexing third-party or user-supplied documents.

**Example**: [vendor-notice-card-vault.md](../datasets/corpus/vendor-notice-card-vault.md) contains
instructions aimed at the model ("ignore previous instructions…"). Ask about the vendor's
maintenance window: the answer gives the window and ignores the planted instructions.

**Guarantees**: passages are fenced and escaped as untrusted data; tool permissions, approvals,
and limits are enforced by the harness in code, so text in a document cannot widen what the agent
may do. The golden set's adversarial cases check this on every evaluation run.

## 7. Multi-team deployment with tenant isolation

**Who**: a platform team serving several teams from one deployment.

Each API key maps to a tenant (`AGENTOPS_API_KEYS`). Documents, executions, approvals, workspace,
and metrics are tenant-scoped; another tenant's document or execution is a 404, not a 403, so
existence does not leak. Rate limits are per tenant. Operators (`AGENTOPS_ADMIN_TENANTS`) see
evaluations and monitoring; other tenants get a clear 403.

## 8. Prove that a change did not make answers worse (LLMOps)

**Who**: the engineer changing prompts, models, chunking, or retrieval settings.

```bash
uv run agentops eval run --judge                                   # 38 golden cases → report
uv run agentops eval check reports/eval-<id>.json --baseline datasets/baselines/golden-v1-graph-qwen3-8b-day4.json
uv run agentops experiment run optimized --kind agent --out reports/bench.json
uv run agentops experiment benchmark reports/bench-baseline.json reports/bench.json
```

The gate fails on a pass-rate drop, a per-category drop, any unsafe tool use, or a p95 latency
regression. Experiment profiles change one allowlisted setting at a time, and a profile is
accepted only with a measured gain and no quality breach. After changing chunking or the
embedding model: `uv run agentops reindex` (documents keep serving their old index meanwhile).

## 9. Watch production for drift

**Who**: whoever operates the deployment.

`agentops drift check` (or *Evaluations & Monitoring → Run check now*) compares quality, retrieval,
data, and operational signals with committed baselines. Each signal is `ok`, `drift`
(`measured` or `suspected`), or `insufficient_data`; a single case that regressed is named even
when the overall pass rate holds. Alerts are stored and can be acknowledged with a note.
`agentops metrics --hours 24` gives latency percentiles, failure/timeout/retry rates, tokens, and
cache hits.

## 10. Audit what the AI did

**Who**: security, compliance, incident reviewers.

Every execution has a ledger (*Executions*, `GET /api/v1/executions/{id}/trace`): each step, model
call (model, tokens, latency), tool call (arguments, outcome), refusal, approval, correction, and
the final decision, without raw prompts or secrets. Traces can be exported to Langfuse or
LangSmith. Workspace operations have their own audit log.

---

## What it is not (yet)

- Not a multi-replica service: rate limits and caches are per process (Redis is the next step).
- No user accounts or SSO: API keys are the credential.
- No OCR for scanned PDFs.
- Local-model speed: 25–90 s per verified answer on an M4 with 16 GB. A hosted
  OpenAI-compatible fallback can be configured for availability, not enabled by default.
