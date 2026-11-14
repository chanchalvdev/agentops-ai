# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

- **AI / platform engineers operating the system** (delegated by the owner, who asked for "what
  would be best"): they ask questions of their documents, approve or reject the agent's proposed
  workspace changes, read execution traces, and watch evaluation scores and drift. They need
  density, speed, and exact numbers.
- **The project owner presenting it** in live walkthroughs (interviews, reviews) on a shared
  screen: every screen must make the engineering legible at a glance — what the agent did, why
  it is trustworthy, what it cost.
- Both audiences use the same screens; there is no separate marketing surface.

## Product Purpose

AgentOps AI is a production-minded agentic RAG platform running entirely on local, free models
(Ollama: qwen3:8b, qwen3:1.7b, nomic-embed-text). It answers questions from a tenant's documents
with validated citations, runs a LangGraph workflow (and a hand-written agent loop) under a
harness that enforces budgets and permissions in code, lets the agent edit a sandboxed workspace
only after a human approves, and measures itself: traces, a 38-case golden evaluation set, a
regression gate, a cost/latency benchmark, and drift monitoring. Success means a user can see
what the agent did and verify it, not just read an answer.

## Positioning

Not a chat wrapper: an observable, governed agent. Every answer is traceable to the exact source
passage and to a ledger of every model call, tool call, rejection, and decision; every write waits
for a human; every quality claim comes from a measured run. The UI's job is to make that evidence
visible.

## Operating Context

- Runs locally on an Apple M4 / 16 GB: agent answers take 20–90 s, so streaming progress and
  honest waiting states matter.
- Desktop browser, often screen-shared; keyboard and mouse.
- The UI is a pure client of the public REST/SSE/WebSocket API (`/docs`); it holds no business
  logic and no credential except the user's API key. Development keys: `dev-key-tenant-a`
  (operator), `dev-key-tenant-b` (non-operator, no workspace).
- Workflows: ask (graph or loop mode) → watch steps stream → open citations; upload/reindex
  documents; browse and edit the workspace, run allowlisted commands; approve/reject proposed
  writes from a diff; inspect an execution's ledger; compare eval runs, run a drift check.

## Capabilities and Constraints

- Screens: Chat, Documents, Workspace (tree, editor, terminal, audit), Approvals, Executions,
  Evaluations & Monitoring (operator-only; other tenants get 403).
- Stack is fixed by the existing code: React 18 + TypeScript (strict) + Vite 5 + Tailwind 3 in
  `web/`; Vite proxies `/api`. No other runtime dependencies so far.
- Terminology: execution, ledger, step, model call, tool call, approval, citation `[S1]`, tool
  reference `[T1]`, tenant, operator, profile, drift signal (measured / suspected /
  insufficient data), regression gate.
- Statuses the UI must distinguish: answered, insufficient_evidence, failed_validation,
  awaiting_approval, timeout, budget_exhausted, cancelled, failed; documents pending /
  processing / ready / failed; approvals pending / approved / rejected.
- Undecided: authentication beyond API keys; mobile layout (desktop is the target).

## Evidence on Hand

Real, measured data in the running system and `datasets/baselines/`, `docs/benchmarks/`:
graph baseline 33/38 vs optimized 36/38 (p50 43.1 s → 27.5 s, tokens −21%); loop vs graph on
33 cases (loop 1/33 strict, 24/33 without structured-output); drift demo (answer coverage
0.971 → 0.882). Sample corpus in `datasets/corpus/`. No users, customers, testimonials, or
pricing exist; never fabricate them.

## Product Principles

1. **Evidence over assertion**: show the source, the trace, the measurement behind every claim.
2. **Humans decide side effects**: approvals are explicit, previewed, and never bypassed.
3. **Honest states**: slow, failed, refused, and "insufficient data" are first-class, clearly
   labelled outcomes, never hidden or dressed up as success.
4. **The server is the authority**: the UI shows what the API decides and explains refusals.
