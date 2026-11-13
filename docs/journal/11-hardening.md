# Journal 11 — Hardening: Rate Limits, Operator-Only APIs, Docker, CI (Day 4, M11) + Day 4 Retrospective

Milestone: the last part of Day 4 in product.txt §18 — "rate limiting, error handling,
Dockerfile, CI workflow, README" with the deliverable "CI green" — implementing the §16 items
"rate limiting or an explicitly documented development-only alternative", the Day 3 finding "any
tenant can start an eval run", and packaging. Branch `day-4-optimization-drift-hardening`,
**uncommitted** working tree on top of `b497479` (Day 3 and Day 4 both uncommitted; nothing has
been committed since the Day 2 API fix). Written by the Recorder agent from the code,
`reports/docker-build.log`, `reports/ollama-11435.log`, the run scripts and test output; facts
reported by the build session and not visible in a file are marked as such.

Status (re-run by the Recorder):
- DB-free: `uv run pytest -q -W error tests/unit tests/security` → **295 passed in 3.67s**.
- `uv run pytest --collect-only -q` → **370/377 collected, 7 deselected** (live Ollama).
- `ruff check app tests` → all checks passed; `ruff format --check` → 131 files formatted;
  `mypy app` (strict) → no issues in 98 source files.
- DB tests: **75 skipped** in the Recorder's run — the Postgres listening on 5435 is now another
  project's container (`password authentication failed for user "agentops"`). The Recorder did not
  start or reconfigure containers. **370 passed with `-W error`** is the build session's result.
- Docker: `reports/docker-build.log` ends `naming to docker.io/library/agentops-ai:local done` and
  `build_exit=0`. Image size (919 MB), `agentops --help` as uid 10001 and the compose `/ready`
  check are the build session's results.

Not in this milestone: a durable job queue (indexing and eval runs are still in-process tasks),
approval expiry, rate limiting for the WebSocket terminal, an OS-level sandbox for workspace
exec, a shared (multi-replica) rate-limit store, a CI job that runs a model.

---

## 1. What was built, by subsystem

### Rate limiting (`app/core/ratelimit.py`, `app/api/deps.py`, `app/main.py`)
- `RateLimiter(limits, clock)`: one token bucket per **(tenant, route class)**, capacity =
  `per_minute`, refilled continuously at `per_minute / 60` tokens per second. `acquire` returns
  `None` (allowed) or the seconds until a token is available, and counts `rejected`.
- `rate_limited(route)` dependency: skipped when `rate_limit_enabled` is false; otherwise raises
  `TooManyRequestsError` (`code "too_many_requests"`, 429). The app's error handler adds
  `Retry-After: ceil(retry_after_s)`.
- Route classes and defaults (`AGENTOPS_RATE_LIMIT_*`):

| Class | Default/min | Applied to |
|---|---|---|
| `agent` | 30 | `POST /agent/run`, `/agent/run/stream`, approve, reject; `POST /rag/answer`; `POST /llm/chat` (every route that calls a model) |
| `upload` | 60 | document upload, reindex |
| `expensive` | 6 | `POST /evaluations/run`, `/experiments/run`, `/monitoring/check` |
| `workspace` | 120 | workspace `PUT` file (write) and exec |

  Reads are not limited: document list/get, executions, traces, approvals list — and
  `POST /retrieval/search`, which does call the embedding model for the query (§8 item 2).

- Per-tenant buckets: tenant B is unaffected when tenant A is limited
  (`test_rate_limit_returns_429_with_retry_after`). `GET /metrics` reports this process's
  `rate_limited_requests`.
- **In-process by design**, and documented as such (§16 allows "an explicitly documented
  alternative"): correct for the one API process this project deploys; replicas would each allow the
  full rate. The WebSocket terminal is not rate limited (README "Known limitations").

### Operator-only APIs (`app/api/deps.py:require_admin`, `app/core/config.py`)
- `AdminDep` on every evaluation endpoint (run, runs, run detail, compare), every experiment
  endpoint, and `/monitoring/summary|drift|alerts|check`. Non-admins get **403**
  `permission_denied`. Closes Day 3 finding 10 ("any authenticated tenant can start an eval run").
- `admin_tenants` (comma-separated). Empty outside prod → `tenant_a` (dev/test); **empty in prod →
  nobody**: fail closed.
- `/api/v1/metrics` stays open to every tenant because it is filtered to the caller's executions.
- Profiles (journal 09): the experiments API takes a profile *name*; profile overrides are limited
  to `OVERRIDABLE` (no database, keys, workspace root or limits), and chunking changes require
  their own eval tenant.

### Container (`Dockerfile`, `.dockerignore`, `docker-compose.yml`)
- `python:3.13-slim`; `git` installed (on the workspace executor's allowlist); `uv` copied from
  `ghcr.io/astral-sh/uv:0.12.19`; `uv sync --frozen --no-dev --no-install-project` **before**
  copying code (dependency layer cached), then the app, `alembic.ini`, `README.md`, `datasets`.
- Non-root `agentops` user, **uid 10001**, owning `/app` and `/data/workspace`;
  `AGENTOPS_WORKSPACE_ROOT=/data/workspace` (a volume in compose).
- `HEALTHCHECK` hits `/health` with Python's `urllib` (no curl in the image).
- `CMD`: `alembic upgrade head && exec uvicorn --factory app.main:create_app` — migrations are
  idempotent, so a fresh database is usable on first start.
- **No models in the image**: Ollama stays native on the host for Apple Silicon GPU acceleration;
  the container reaches it at `AGENTOPS_OLLAMA_BASE_URL_FROM_DOCKER` (default
  `http://host.docker.internal:11434`).
- `.dockerignore` excludes `.venv`, `.git`, `.github`, caches, `.env`, `workspace`, `reports`,
  `docs`, `tests`, `product.txt`.
- Compose: `api` service under the `app` profile (`docker compose --profile app up -d api`),
  depends on a healthy `db`, ports `${AGENTOPS_API_PORT:-8000}`, `AGENTOPS_API_KEYS` passed through.

### CI (`.github/workflows/ci.yml`)
- `test` job: pgvector service on 5433, `AGENTOPS_REQUIRE_DB=1` (missing DB fails instead of
  skipping), ruff check, ruff format check, mypy, pytest, then three file-only gates:
  1. `agentops eval check datasets/baselines/golden-v1-graph-qwen3-8b.json` (Day 3 floor check),
  2. **`agentops eval check …-optimized.json --baseline …-day4.json`** — the accepted Day 4
     benchmark must still satisfy the same regression thresholds (it does: 36/38 vs 33/38, no
     newly failing case),
  3. `agentops experiment list` (the experiments file loads and validates).
- `docker` job: `docker build` and `docker run --rm agentops-ai:ci agentops --help` (the image
  imports the app).

### Error handling touched in Day 4
- New typed errors: `TooManyRequestsError` (429), and the admin check reuses
  `PermissionDeniedError` (403). Both go through the existing safe-error envelope
  (`{"error": {"code", "message"}}`).
- Experiments API: a startup failure before the run row exists re-raises as a normal API error
  (`task.result()`), the same pattern as Day 3's eval API.

---

## 2. Files touched

New: `app/core/ratelimit.py` (39), `Dockerfile` (38), `.dockerignore`.

Modified: `app/api/deps.py` (`rate_limited`, `require_admin`, `AdminDep`),
`app/api/{agent,documents,retrieval,llm,workspace,evaluations,experiments,monitoring}.py`
(dependencies), `app/core/errors.py` (`TooManyRequestsError`), `app/main.py` (`Retry-After`),
`app/core/config.py` (`admin_tenants`, `rate_limit_*`), `app/services.py` (`rate_limiter`),
`docker-compose.yml` (`api` service), `.github/workflows/ci.yml` (gates + docker job),
`.env.example`, `README.md` (Hardening, Troubleshooting, Known limitations),
`docs/day-checklist.md`, `tests/integration/test_evaluation_db.py` (other tenant now 403),
`tests/conftest.py`.

Not in git: `reports/docker-build.log`, `reports/ollama-11435.log`, `reports/*.sh`.

---

## 3. Key design decisions (taken vs rejected)

| Taken | Rejected | Why |
|---|---|---|
| Token bucket per (tenant, route class) | Fixed window; one global limit | Smooth refill, bursts up to the class capacity; one tenant cannot starve another; cheap reads are not throttled like evals. |
| In-process buckets, documented | Redis now | One API process is the deployment; §16 allows a documented alternative. The interface (`acquire`) is what a Redis version would keep. |
| 429 + `Retry-After` | 503 or a silent queue | Tells the client exactly when to retry; distinguishes our limit from a provider's (`rate_limited` provider error). |
| A separate `expensive` class (6/min) | Same limit as agent runs | An eval or experiment occupies the local model for ~30 minutes. |
| Admin-only eval/experiments/monitoring, fail closed in prod | Per-tenant evaluation | They run the shared model and read cross-tenant data. |
| Profile names over the API, allowlisted overrides | Settings in the request body | A request can never widen permissions, limits or paths. |
| Ollama native on the host, container talks to it | Ollama in Docker | Docker on macOS has no Apple GPU; the 8b model would run on CPU. |
| Migrations at container start | A separate migration job | Single-service deployment; Alembic is idempotent. |
| Non-root uid 10001, workspace on a volume | Root container | Workspace exec runs commands; least privilege inside the container. |
| CI gates committed live reports | Running Ollama in CI | No model in CI; the gates prove the report format and thresholds, not the model (§8 item 1). |
| `AGENTOPS_REQUIRE_DB=1` in CI | Skip DB tests when Postgres is missing | A skipped integration suite would look green. |

---

## 4. Live verification and operational incidents

### Docker (build session, build log in `reports/docker-build.log`)
- `docker build` OK (`build_exit=0`); image **919 MB**; `agentops --help` runs as uid 10001.
- `docker compose --profile app up -d api`: migrations ran, `/health` ok, `/ready` reported the
  **database ok and Ollama unreachable**. Cause: the native Ollama listened on loopback only
  (`reports/ollama-11435.log`: `Listening on 127.0.0.1:11435 (version 0.40.7)`), so
  `host.docker.internal` could not reach it. Documented in the README: run Ollama with
  `OLLAMA_HOST=0.0.0.0:<port>` for the container case. Not changed in code — the readiness check
  reported the truth, which is its job.

### Incident 1: port 11434 belonged to someone else
Reported by the build session, consistent with the files: every model call failed with
`provider_error` while `/api/tags` answered. Port 11434 was owned by **another project's Docker
Ollama that did not have the qwen3 models**. Fix: a second, native Ollama on 11435
(`ollama-11435.log`, started 12 Nov 19:53:10, 14 s before `bench.sh`), and
`export AGENTOPS_OLLAMA_BASE_URL=http://localhost:11435` in every `reports/*.sh`. The outage is
visible later in the drift check as the *previous* operational window: failure rate 0.8333,
provider error rate 0.5227 (journal 10 §4). README Troubleshooting now has the symptom and fix.

### Incident 2: the Mac went to sleep
Reported by the build session: system sleep killed long runs twice. `reports/bench2-aborted.log`
is one of them (optimized run started 13 Nov 09:14, stops after 15 cases, with a provider retry
logged hours later). Fix: run with `caffeinate -ims` (prevent idle, disk and system sleep).

### Machine state
`bench4.sh` logged swap 9.3 → 11.0 GB during the baseline rerun, with another project's containers
running (build session). This is why latency is compared only between back-to-back runs (journal
09 §4) and why the README warns about memory pressure on 16 GB.

---

## 5. How it was verified

- `tests/unit/test_monitoring.py::test_rate_limiter_refuses_then_refills`: 2/min → two allowed,
  third refused with wait in (29, 30] s, another tenant allowed, allowed again at t = 30 s,
  `rejected == 1` (injected clock).
- `tests/integration/test_monitoring_db.py::test_rate_limit_returns_429_with_retry_after`: agent
  limit 1 → second request 429, code `too_many_requests`, `Retry-After ≥ 1`; tenant B still 200.
- `test_monitoring_is_operator_only`: 403 `permission_denied` for tenant B on monitoring summary,
  drift, check, experiments and evaluation runs.
- `tests/integration/test_evaluation_db.py::test_evaluation_api`: another tenant now gets 403.
- `tests/unit/test_monitoring.py::test_profiles_cannot_override_unsafe_settings`.
- CI steps re-run locally by the build session; the Recorder ran ruff, format, mypy and the DB-free
  suite (§ Status).

---

## 6. Commands

```bash
# Container
docker build -t agentops-ai:local . 2>&1 | tee reports/docker-build.log
docker run --rm agentops-ai:local agentops --help
OLLAMA_HOST=0.0.0.0:11435 ollama serve                                  # reachable from Docker
AGENTOPS_OLLAMA_BASE_URL_FROM_DOCKER=http://host.docker.internal:11435 \
  docker compose --profile app up -d api
curl localhost:8000/health; curl localhost:8000/ready
# Second Ollama when 11434 is taken
OLLAMA_HOST=127.0.0.1:11435 ollama serve
export AGENTOPS_OLLAMA_BASE_URL=http://localhost:11435
caffeinate -ims zsh reports/bench.sh
# CI gates locally
uv run agentops eval check datasets/baselines/golden-v1-graph-qwen3-8b-optimized.json \
  --baseline datasets/baselines/golden-v1-graph-qwen3-8b-day4.json
uv run ruff check app tests && uv run ruff format --check app tests && uv run mypy app
uv run pytest -q -W error
```

Settings: `AGENTOPS_ADMIN_TENANTS`, `AGENTOPS_RATE_LIMIT_ENABLED`,
`AGENTOPS_RATE_LIMIT_{AGENT,UPLOAD,EXPENSIVE,WORKSPACE}_PER_MIN`,
`AGENTOPS_OLLAMA_BASE_URL_FROM_DOCKER`, `AGENTOPS_API_PORT`.

---

## 7. What this milestone taught

1. **Fail closed in prod, convenient in dev.** Empty `admin_tenants` means `tenant_a` in dev and
   nobody in prod — the same rule as Day 1's API keys.
2. **A readiness check that says "unreachable" is working.** The container's `/ready` was right;
   the fix was the Ollama bind address, not the check.
3. **Shared developer machines produce production-shaped incidents.** A port owned by another
   project's container, sleep killing a run, swap from someone else's containers: each became a
   README entry and a habit (`caffeinate`, a dedicated port, logging swap).
4. **"CI green" for a model system means "the recorded evidence still passes the gate".** CI can't
   run qwen3:8b; it can make sure nobody edits a baseline or threshold into a contradiction.
5. **Document the limit you chose not to build.** In-process buckets and caches are fine for one
   process and are written down as such, with the upgrade path.

---

## 8. Known limitations and open findings (Recorder review of the working tree)

1. **CI still gates static files.** Both gates compare committed reports; no fresh run is produced
   in CI, so a code change that breaks the agent passes CI unless someone runs the benchmark and
   commits the result. A recorded-response replay is still the missing piece (Day 3 finding 1).
2. **Rate limits are per process and per route class only**: no global cap on concurrent model
   work, so 30 agent runs/min/tenant × N tenants can still queue the one local model for hours;
   deadlines (180 s) bound each run, not the queue. `POST /retrieval/search` embeds every query
   on the shared Ollama and has no rate class.
3. **WebSocket terminal not rate limited** (documented).
4. **Compose default Ollama URL is `:11434`**, while the live runs used `:11435`; the README covers
   it through `AGENTOPS_OLLAMA_BASE_URL_FROM_DOCKER`, but the default would hit the other project's
   Ollama on this machine.
5. **Image size 919 MB** for a Python API (slim base + git + full dependency set); no multi-stage
   trim. `datasets/` (incl. the prompt-injection corpus doc and baselines) ships in the image.
6. **Migrations at start** race if several replicas start together (Alembic has no lock here);
   acceptable for one replica.
7. **Admin is a tenant list, not a role**: an admin tenant's key is all-powerful for evals and
   monitoring; there is no separate operator credential or audit of admin calls beyond logs.
8. **Unchanged Day 2/3 deferrals:** in-process `BackgroundTasks` indexing and eval tasks (no durable
   queue), approval expiry/notification, synchronous approve, workspace not an OS sandbox,
   all-or-nothing workspace permissions, receipts never cleaned up.
9. **The Recorder could not re-run the 75 DB tests** (port conflict above); the 370 figure rests on
   the build session's run.

---

## 9. What I'd do differently at 100x scale

- **Gateway-level limits** (Redis or the API gateway) per tenant and per route, plus a **global
  concurrency limit and queue for model work** with per-tenant fair share; 429 vs 202-queued as
  explicit outcomes.
- **Durable jobs** for indexing, evals and experiments (a queue with retries and idempotent
  handlers), so a restart doesn't lose work.
- **Roles and scopes** (operator, tenant admin, user) instead of an admin-tenant list, with an
  audit log of operator actions.
- **Multi-stage image**, SBOM and vulnerability scan in CI; migrations as a one-shot job before
  rollout; read-only root filesystem.
- **Model serving as its own service** (vLLM/Ollama on GPU nodes) behind the router, health-checked
  and autoscaled, so the API container never depends on a developer's loopback.
- **CI replay** of recorded model responses per PR and a nightly live benchmark on fixed hardware.

---

## 10. Day 4 retrospective (M9 → M11)

Day 4 was planned for Sat 24 Oct and finished Fri 13 Nov (checklist); the live benchmark and drift
runs took two days of wall clock on a shared 16 GB machine.

### What Day 4 delivered
- **M9 — optimization** (journal 09): usage accounting from the ledger (calls by model, retries,
  embeddings, cache hits, hypothetical hosted cost), five tuning switches defaulting to Day 3,
  rule routing + calculator nudge, evidence cap with only shown passages citable, fixed
  `insufficient_evidence` text, three caches with versioned permission-aware keys, experiment
  profiles with an allowlist, benchmark verdict. **Live: `optimized` accepted** — 36/38 vs 33/38,
  tokens/request −21%, qwen3:8b calls/request 3.03 → 2.76, p50 −36%, p95 −8%, tool_use 2/5 → 5/5.
- **M10 — drift** (journal 10): model-free retrieval eval with hit@k, recall@k, MRR and answer
  coverage; a four-family detector with `measured`/`suspected`/`insufficient_data`; checks and
  alerts persisted; API and CLI. **Live: the deliberate chunking regression was caught** (exit 1,
  pass rate and coverage measured) while recall and hit rate stayed 1.0.
- **M11 — hardening** (this journal): per-tenant rate limits with 429 + `Retry-After`, operator-only
  eval/experiments/monitoring, Dockerfile and compose `api` profile, CI gates on the committed
  benchmark plus a docker build job.
- Tests: **370** (Day 3 ended at 323), `-W error`, ruff, format and mypy strict clean.

### The mistakes, in order, and how each was caught
| # | Mistake | Caught by | Fix |
|---|---|---|---|
| 1 | Turned query rewrite off because the retrieval probe said coverage 1.0 | benchmark gate (31/38, multi_document 2/5) | rewrite stays on; profile kept as `optimized-no-rewrite` |
| 2 | Coverage counted "15 minutes" from the failover runbook | explaining mistake 1 | only expected-document passages count; regression test |
| 3 | Rule routing skipped tool selection for comparisons | gate (`multi-availability` newly failing), then 3×: 3/3 vs 0/3 | `is_comparison` keeps `select_tools` (and its `document_metadata` call) |
| 4 | Compared latency across days | baseline rerun: same answers, p50 60.6 s vs 43.1 s | back-to-back runs for latency; tokens/calls as the stable metric |
| 5 | Mixed-chunking signal fired on the experiment's own tenant | the drift demo | per-tenant grouping |
| 6 | Ran the benchmark against an Ollama without the models | every call `provider_error` | native Ollama on 11435 |
| 7 | Let the Mac sleep mid-run (twice) | aborted logs | `caffeinate -ims` |

Pattern, same as Days 2-3: every mistake that mattered was caught by a live run disagreeing with
an expectation — and then explained before being fixed. The gate rejected two of three candidate
speedups; both rejections were correct.

### What the live runs taught
1. **Temperature 0 makes quality comparisons deterministic** on one setup (identical answers 23 h
   apart, token totals equal to Day 3's), and leaves latency to machine state.
2. **A removed call can carry hidden work** (`select_tools` → `document_metadata` → version labels).
3. **Small corpora saturate file-level retrieval metrics**; answer-level coverage was the signal
   that moved.
4. **The judge rewards refusals**: it scored the degraded run higher (0.998 vs 0.969).
5. **Changed ≠ worse**: the tokens alert was recovery from the provider outage.
6. **Don't claim unmeasured wins**: caches had ≈ 0 hits; the relevance floor was measured and
   dropped.

### Day 3 open items closed on Day 4
`insufficient_evidence` text unverified → fixed text; evidence cap → `_bounded`; tool use 2/5 →
5/5 (optimized); per-case cost not persisted → `cost_usd`/`usage` columns; RAG metrics missing →
retrieval eval; any tenant can start a run → admin only; `GET /api/v1/metrics` → done; relevance
threshold → measured, not adopted; rate limiting → done (in-process).

### Still open, carried to Day 5 and beyond (from `docs/day-checklist.md` Day 4 "Deferred" and this review)
- `multi-retention` and `ws-edit-approved` fail in every profile.
- Loop-mode baseline never run (graph only).
- Caches unmeasured live; `optimized-small` and the semantic cache never run live.
- Rate limits and caches per process; WebSocket terminal not limited.
- Drift: overall `ok` when nothing was measured; no newly-failing rule; alerts cannot be
  acknowledged; no scheduled checks (journal 10 §8).
- CI gates static reports only; no replay.
- Day 2/3 carry-overs: durable queue, approval expiry and claim-then-resume recovery, synchronous
  approve, SSE reconnect (Day 5 UI), workspace OS sandbox, history trimming, router retry tokens not
  budgeted.
- **Nothing committed yet**: Day 3 and Day 4 are one uncommitted working tree on
  `day-4-optimization-drift-hardening`; committing (per milestone) is the user's call.
