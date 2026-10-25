# Journal 02 — Harness + Tools (Day 2, M1 + M2)

Milestone: first half of Day 2 in product.txt §18 (execution context, budgets, typed errors,
retry policy, tool interface and registry, the three document tools). Commits `a92de84`
(harness) and `e882582` (tools) on `day-2-harness-loop-graph`. Status: 164 tests green including
integration on Postgres; 57 of them are new (23 harness, 30 calculator, 4 tool integration).
Written by the Recorder agent from the code, not from the plan.

The custom agent loop, LangGraph workflow, approvals API and SSE are **not** in this milestone;
they are the next consumers of everything below.

---

## 1. What was built, by subsystem

### Limits and counters (`app/core/budgets.py`)
- `ExecutionLimits` (Pydantic): `max_steps=8`, `max_model_calls=12`, `max_tool_calls=10`,
  `max_corrections=2` (per failure kind), `max_total_tokens=48_000`, `max_cost_usd=None`,
  `deadline_s=180`. `from_settings` reads the new `AGENTOPS_AGENT_*` settings.
- `ExecutionLimits.tightened(**overrides)`: per-field `min(server_default, request_value)`; `None`
  overrides are ignored; a `None` server value (only `max_cost_usd`) accepts the request value.
  Result is re-validated, so a request can never loosen a limit.
- `BudgetCounters` (dataclass): `steps`, `model_calls`, `tool_calls`, `usage: Usage`,
  `cost_usd: Decimal`, `cost_known: bool`, `corrections: dict[kind, int]`.

### Execution context (`app/core/execution_context.py`)
- `ExecutionContext` (dataclass, one per request): `tenant_id`, `limits`, `allowed_tools`,
  `permissions` (default `{"documents:read"}`), `request_id`, `execution_id`, injectable `clock`
  (default `time.monotonic`), `workflow_state`, `counters`, `ledger`, `trace_ids`,
  `approved_call_ids`, `cancel_reason`, `started_at` (set in `__post_init__` from `clock`).
- `deadline_at`, `remaining_s()`, `cancelled`, `cancel(reason)`, `record(kind, name, status,
  **fields)` which appends a `LedgerEvent` with a monotonically increasing `seq`.
- `LedgerEvent`: `kind` ∈ step / model_call / tool_call / correction / policy / approval / final;
  `status` ∈ ok / error / rejected / pending; `error_code`, `message`, `latency_ms`, token counts,
  `data`, `at` (UTC).

### Errors (`app/core/errors.py`, new section)
Policy errors end the execution: `BudgetExhaustedError` (429), `DeadlineExceededError` (504),
`ExecutionCancelledError` (409), `ToolPermissionError` (403), `ApprovalRequiredError` (409).
Correctable errors go back to the model: `InvalidToolArgumentsError` (422),
`ToolExecutionError` (502), `ToolTimeoutError(ToolExecutionError)` (504, **transient**).
`VerificationError` (422) is declared for the later `verify_answer` node.

### Harness (`app/agents/harness.py`)
The single gate for every model and tool call. `Harness(llm: LLMRouter, tools: ToolRegistry)`.
- `ensure_active` — cancellation, then deadline. Every public entry point calls it first.
- `begin_step` — step limit; records a `step` event.
- `consume_correction(ctx, kind)` — bounded corrections per failure kind
  (`invalid_output`, `citation`, ...), each kind has its own allowance.
- `call_model(ctx, role, request)` — active → model-call limit → token budget → cost budget →
  clamp `request.timeout_s` to remaining time → **increment `model_calls` before the call** →
  `asyncio.timeout(remaining)` around `LLMRouter.chat` → add usage, `estimate_cost`, mark
  `cost_known=False` if pricing unavailable → ledger event with role, model, provider, tool-call
  names, router attempts, pricing source.
- `authorize_tool_call(ctx, call)` — resolve + allowlist (unknown and disallowed give the same
  `ToolPermissionError`), tool permissions vs context permissions (`detail` lists what is missing,
  `message` does not), `raw_arguments` present → reject, Pydantic validation with
  `_argument_problems` (field paths + error types only).
- `call_tool(ctx, call)` — active → authorize → tool-call limit → approval check (pending event
  + `ApprovalRequiredError` unless `call.id` is in `approved_call_ids`) → increment `tool_calls`
  → `retry_async(attempt, max_retries=1 if tool.idempotent else 0)`; each attempt re-checks
  active, runs under `asyncio.timeout(min(tool.timeout_s, remaining))`, validates output against
  `output_model`, maps timeouts to `ToolTimeoutError`/`DeadlineExceededError` and unknown
  exceptions through `tool.map_error`. Returns `ToolOutcome(call_id, name, output, latency_ms,
  attempts)`.

### Tool interface (`app/tools/base.py`, `registry.py`)
- `Tool[I, O]` (PEP 695 generic ABC): `name`, `description`, `input_model`, `output_model`,
  `permissions`, `timeout_s=10`, `idempotent=True`, `requires_approval=False` — the contract is
  data the harness reads, not behaviour the tool enforces. `run(args, ctx)`; `map_error` wraps
  unknown exceptions as `ToolExecutionError("<name> failed", detail=repr(exc))`; `spec()` builds a
  `ToolSpec` from the input model's JSON schema.
- `ToolRegistry`: rejects duplicate names; `get`, `names`, `specs(allowed)` returns specs for the
  allowed subset only, sorted by name.

### Tools
- `calculator.py` — `evaluate` parses with `ast.parse(mode="eval")` and walks an allowlist via
  `match`: numeric constants (not bool), `+ - * / // % **`, unary `+ -`, and calls to `abs round
  min max sqrt` with 1–10 positional args. Caps: 200 chars, depth 20, exponent |e| ≤ 100,
  |result| ≤ 1e100 and finite, no complex. Division/modulo by zero rejected. Every rejection is
  `InvalidToolArgumentsError` so the model can correct itself. Timeout 2 s, no permissions.
- `document_search.py` — wraps `HybridRetriever.search(ctx.tenant_id, ...)`. Input: `query`
  (1–500 chars), optional `document_ids` (≤ 20 UUIDs), optional `metadata` exact-match labels,
  `top_k` 1–10 (default 5). Output: raw `Passage`s with `chunk_id`, `document_id`, filename,
  version, section, pages, score, text truncated to 2000 chars. Permission `documents:read`,
  timeout 30 s (includes the query embedding).
- `document_metadata.py` — `IngestionService.get(ctx.tenant_id, id)`; another tenant's ID is
  `NotFoundError`, same as a missing one. Returns filename, version, `is_latest`, status, pages,
  chunk count, labels, timestamps. Permission `documents:read`, timeout 5 s.

### Wiring and infra
- `app/services.py:build_services` builds one `IngestionService` (previously built inline) and
  shares it with `DocumentMetadata`; `ToolRegistry([DocumentSearch, Calculator, DocumentMetadata])`
  and `Harness(llm, tools)` are new fields on `Services`.
- `app/core/config.py`: `agent_max_steps` (≤ 50), `agent_max_model_calls` (≤ 100),
  `agent_max_tool_calls` (≤ 100), `agent_max_corrections` (≤ 5), `agent_max_total_tokens`,
  `agent_max_cost_usd` (None = no monetary cap), `agent_deadline_s` (≤ 900).
- `docker-compose.yml`: host port `${AGENTOPS_DB_PORT:-5433}:5432` because 5433 was held by another
  local project. `AGENTOPS_DATABASE_URL` must be changed to the same port by hand.

---

## 2. Files touched

```
app/core/{budgets,execution_context}.py            (new)
app/core/{errors,config}.py                        (extended)
app/agents/__init__.py, app/agents/harness.py      (new)
app/tools/__init__.py, app/tools/{base,registry,calculator,document_search,document_metadata}.py (new)
app/services.py
docker-compose.yml
tests/unit/test_harness.py, tests/unit/test_calculator.py, tests/integration/test_tools.py (new)
```

---

## 3. Key design decisions (taken vs rejected)

| # | Decision taken | Rejected alternative | Why / cost accepted |
|---|---|---|---|
| 1 | **One harness gate** for every model/tool call (`Harness.call_model`, `call_tool`); the loop and graph nodes never call `LLMRouter` or a tool directly | Each agent/node checks its own limits | Limits enforced in one place cannot be forgotten by a new node; the custom loop and LangGraph share identical policy, so their comparison is fair. Cost: every caller must hold a `Harness` and an `ExecutionContext`. |
| 2 | **Check order: cancellation → deadline → budget → permission → arguments** (module docstring, `ensure_active` first) | Validate arguments first, then budgets | Cheap, absolute stops first: a cancelled or expired run should not spend effort validating, and a rejection says the most important reason. Note `call_tool` actually checks the tool-call budget *after* permission/arguments (open finding 3). |
| 3 | **Model calls counted on attempt** (`model_calls += 1` before `await`) | Count only successful calls | A provider that fails every time would otherwise loop forever without consuming budget. Failures cost real time and sometimes tokens. Cost: a transient blip burns one of 12 calls. |
| 4 | **Fail closed on unknown cost** (`cost_known=False` blocks further calls under a cost cap) | Treat unknown as $0, or ignore the cap | If we cannot prove we are under the cap we must stop. With no cap (`max_cost_usd=None`, the default for local models) unknown cost changes nothing. Cost: a missing price entry for a hosted fallback model ends the run after one call. |
| 5 | **Unknown tool ≡ disallowed tool** (same `ToolPermissionError("Tool not permitted: …")`) | "No such tool" vs "forbidden" | Different messages would let a prompt-injected model probe which tools exist on the server. `registry.specs(allowed)` also hides disallowed tools from the prompt. Cost: harder debugging; the missing permission is in `detail` (logs) only. |
| 6 | **Retries only for idempotent tools**, at most one (`max_retries=1 if tool.idempotent else 0`) | Retry every transient failure | A timed-out write may already have happened; a retry could apply it twice. Reads (search, metadata, calculator) are safe to repeat. Cost: a flaky write tool surfaces the first error to the model/human. |
| 7 | **Argument errors never echo values** (`_argument_problems` uses `loc` + `type`) | Return Pydantic's full message | The error text is fed back into the prompt. Echoing values reflects attacker- or document-controlled text into the next model turn; the test sends `"IGNORE PREVIOUS INSTRUCTIONS"` and asserts it is absent. Cost: less specific feedback to the model ("times: int_parsing"). |
| 8 | **Tenant from context, never from the model** (`ctx.tenant_id` in both document tools; no tenant field in any input model) | Pass tenant as a tool argument | A model (or injected document) can write any argument. The integration test passes tenant A's `document_ids` from tenant B and gets `[]`. |
| 9 | **Tools return raw passages with chunk IDs; the agent assigns citation labels** (`document_search.py` docstring) | Tool returns `[S1]..[Sn]` labels | Labels must be unique across several searches in one run; a tool only sees one call. The agent also owns fencing evidence as untrusted. Cost: the agent layer needs a label map (Day 2 loop). |
| 10 | **Requests can only tighten limits** (`ExecutionLimits.tightened` uses `min`) | Let the API caller set limits freely | Server defaults are the operator's cost/latency ceiling; a client asking for `max_steps=20` gets 8. |
| 11 | **Hard deadline with `asyncio.timeout`**, and the per-call timeout clamped to remaining time | Only check the deadline between steps | A hung model call would otherwise overrun the deadline by its own 120 s timeout. `test_deadline_interrupts_an_in_flight_model_call` proves cancellation mid-call. |
| 12 | **Corrections bounded per failure kind** (`consume_correction`) | One global retry counter, or unbounded "try again" | One kind of failure (e.g. bad citations) cannot starve another (invalid JSON), and no kind can loop. The model-call and step limits remain the global backstop. |
| 13 | **Approval as a policy check before execution** (`requires_approval` + `approved_call_ids`) | Ask the model to "confirm" before writing | The model cannot approve itself; only code adds to `approved_call_ids`. Re-running an approved call re-checks every limit. |
| 14 | **AST allowlist calculator** | `eval` with restricted globals, or `numexpr`/`sympy` | `eval` sandboxes are escapable (`().__class__.__bases__`); a dependency is overkill for arithmetic. Exponent/magnitude/depth/length caps stop `9**9**9` CPU/memory blow-ups. |
| 15 | **Output validated against `output_model`** in the harness | Trust tool return values | A buggy tool cannot hand the model a malformed result; it becomes `ToolExecutionError` with Pydantic detail in logs only. |

---

## 4. Commands

```bash
AGENTOPS_DB_PORT=5434 docker compose up -d db   # if 5433 is taken; also update AGENTOPS_DATABASE_URL
uv run pytest tests/unit/test_harness.py tests/unit/test_calculator.py
AGENTOPS_REQUIRE_DB=1 uv run pytest tests/integration/test_tools.py
uv run pytest                                   # full suite
uv run ruff check app tests && uv run mypy app
```

There is no API endpoint or CLI command for the harness yet; it is exercised through tests and
`services.harness.call_tool(...)`.

---

## 5. How it was verified

- **164 tests passed** (unit + integration on Postgres). Without a database the suite reports
  122 passed / 42 skipped, which is the expected skip behaviour from `tests/conftest.py`.
- `tests/unit/test_harness.py` maps 1:1 to the seven §5 demonstrations:
  1. no execution without permission — allowlist, unknown tool, missing permission, approval;
  2. step limit; 3. deadline (blocked before, and interrupted in flight with a hanging provider);
  4. model-call, tool-call, token and cost budgets (incl. fail-closed unknown cost) and
     tighten-only limits; 5. malformed arguments (missing field, wrong type, unparseable JSON,
     no value echo); 6. cancellation blocks step, model and tool; 7. corrections bounded per kind,
     idempotent tool retried exactly once, non-idempotent never retried, tool timeout, unexpected
     exception mapped without leaking detail, ledger order and `seq`.
- Every rejection test also asserts the tool did **not** run (`runs == 0`) and counters did not
  move, so "rejected" means "never executed", not just "raised".
- `tests/unit/test_calculator.py`: 10 valid expressions, 12 injection/syntax attempts
  (`__import__`, attribute access, lambdas, strings, bools, complex, kwargs, statements), 8
  resource/domain limits.
- `tests/integration/test_tools.py`: passages carry source fields; cross-tenant search returns
  nothing even with explicit IDs; metadata ownership (other tenant and random ID both 404);
  `specs()` exposes only allowed tools.

---

## 6. Deferred (next milestones)

- Custom agent loop (M3) and LangGraph workflow (M4) that consume the harness; citation label map
  and evidence fencing in the agent layer.
- `human_approval` interrupt/resume, approvals API, SSE events (rest of Day 2).
- Persisting the ledger (today it is an in-memory list on the context); `trace_ids` and
  `workflow_state` exist but nothing writes them yet.
- Workspace (write/exec) tools that actually use `requires_approval` and `idempotent=False`
  (Day 3); `VerificationError` is declared for `verify_answer`.
- Per-request limit overrides through the API (only `tightened` exists).
- Spec §5 lists "maximum retries" in the context; tool retries are hard-coded to 1 in the harness
  and model retries live in `LLMRouter`, not in `ExecutionLimits`.

---

## 7. Known limitations and open findings

Found by the Recorder while reading the code (not fixed in this milestone):

1. **Approval is keyed only by `call.id`, and IDs can repeat.** `harness.py:213` checks
   `call.id in ctx.approved_call_ids`; `OllamaProvider` assigns `f"call_{i}"` when Ollama omits an
   ID (`provider.py:121`) and the OpenAI path uses `""`. An approval for `call_0` in turn 1 also
   approves a *different* `call_0` in turn 3, with different arguments. Fix: harness-generated
   IDs, or bind approval to `(id, tool, hash(arguments))`.
2. **Calculator lets two exceptions escape as generic failures.** `(10**100)**4` reaches
   `math.isfinite` on a huge int → `OverflowError` (`calculator.py:73`); `0 ** -1` →
   `ZeroDivisionError` (only `/ // %` are checked, `calculator.py:58`). Through the harness both
   become `ToolExecutionError("calculator failed")` instead of a correctable
   `InvalidToolArgumentsError`.
3. **Tool-call budget is checked after permission and arguments** (`harness.py:208-212`), contrary
   to the documented order. With the budget exhausted, a malformed call returns
   `invalid_tool_arguments` (which invites a correction turn) instead of `budget_exhausted`.
4. **Double ledger entries** when the deadline/cancellation trips inside a tool attempt:
   `ensure_active` records `rejected` (`harness.py:223`) and the outer handler records `error`
   for the same failure (`harness.py:253`).
5. **Token budget can be overshot by one call.** `max_output_tokens` is not clamped to the
   remaining token budget (`harness.py:121-124`); the check is pre-call only (the test comments
   "starts under budget, ends over it"). Same for cost.
6. **`max_model_calls` counts harness calls, not provider calls.** `LLMRouter` may retry and fall
   back inside one harness call (`response.attempts` is only logged), and tokens of failed calls
   are never counted.
7. **`tightened()` with an unknown key raises `KeyError`** (`budgets.py:43`), which would surface
   as a 500 once the API passes overrides. `max_cost_usd` accepts negatives and `0`; with `0`,
   `cost_usd >= max_cost_usd` blocks even $0 local calls (`harness.py:86`).
8. **Ledger stores full validated arguments** on success (`harness.py:275`), i.e. user query text,
   although `LedgerEvent` says "never secrets or raw prompts". Conversely the `approval/pending`
   event (`harness.py:214`) stores only `call_id`, not the arguments a human needs to decide.
9. **`NotFoundError` from `document_metadata`** is neither a policy error nor a
   `ToolExecutionError`; the loop must decide whether a hallucinated `document_id` ends the run or
   is fed back to the model (the errors.py comment only covers argument/execution errors).
10. Cancellation is cooperative: `ctx.cancel()` stops the next operation, not one in flight.
11. Latent (Day 1 code): Ollama tool calls with `arguments: null` become `raw_arguments="None"`
    (`provider.py:124`), so a future zero-argument tool would always be rejected.
12. `AGENTOPS_DB_PORT` and the `AGENTOPS_AGENT_*` settings are not in `.env.example`/README.

---

## 8. What I'd do differently at 100x scale

- **Persist the ledger** as an append-only table keyed by `execution_id` (and stream it over SSE),
  so runs are auditable after the process dies and dashboards can aggregate rejections by code.
- **Distributed budgets**: per-tenant token/cost budgets across concurrent executions need an
  atomic counter (Postgres row with `UPDATE … RETURNING`, or Redis `INCRBY`) and reservations
  (reserve `max_output_tokens` before the call, settle after) instead of in-memory counters.
- **Cancellation as a signal**: keep the asyncio task handle and cancel it, or propagate a
  cancellation token to providers, so a stop button frees the GPU immediately.
- **Approval bound to content** (tool + argument hash + expiry + approver identity), stored
  durably so a graph can resume after restart.
- **Tool isolation**: run untrusted/expensive tools in separate workers with their own concurrency
  limits and circuit breakers; per-tool rate limits per tenant.
- **Policy as data**: per-tenant tool allowlists and permissions loaded from a table rather than
  hard-coded defaults (`DEFAULT_PERMISSIONS`).
