# Interview Prep 11 — Hardening: Rate Limits, Operator-Only APIs, Docker, CI (Day 4, M11)

Every answer is grounded in the working tree of branch `day-4-optimization-drift-hardening`
(uncommitted), `reports/docker-build.log`, `reports/ollama-11435.log` and the run scripts in
`reports/`. Format: direct answer, then the reasoning, then a code pointer. Companion:
`docs/journal/11-hardening.md` (includes the Day 4 retrospective).

---

## Basic

### Q1. What did the hardening milestone add?
**Answer:** Per-tenant token-bucket rate limits per route class with 429 + `Retry-After`;
operator-only (admin) evaluation, experiment and monitoring APIs; a Dockerfile (non-root,
healthcheck, migrations at start) and a compose `api` profile; CI gates on the committed Day 4
benchmark and a docker build job.
Pointer: `app/core/ratelimit.py`; `app/api/deps.py`; `Dockerfile`; `.github/workflows/ci.yml`.

### Q2. What are the rate-limit classes?
**Answer:** `agent` 30/min (agent run and stream, approve, reject, `/rag/answer`, `/llm/chat`),
`upload` 60/min (upload, reindex), `expensive` 6/min (eval run, experiment run, drift check),
`workspace` 120/min (file write, exec). All configurable via `AGENTOPS_RATE_LIMIT_*`.
Pointer: `app/core/config.py` (`rate_limit_*`); `rate_limited(...)` in `app/api/*.py`.

### Q3. What does a limited client see?
**Answer:** HTTP 429 with `{"error": {"code": "too_many_requests", ...}}` and a `Retry-After`
header in whole seconds (rounded up). Other tenants are unaffected.
Pointer: `app/core/errors.py:TooManyRequestsError`; `app/main.py` error handler;
`tests/integration/test_monitoring_db.py::test_rate_limit_returns_429_with_retry_after`.

### Q4. Who is an admin?
**Answer:** A tenant listed in `AGENTOPS_ADMIN_TENANTS`. If empty: `tenant_a` outside prod, nobody
in prod — fail closed. Non-admins get 403 `permission_denied` on evaluations, experiments and
`/monitoring/*`.
Pointer: `app/core/config.py:admin_tenant_ids`; `app/api/deps.py:require_admin`.

### Q5. What's in the Docker image?
**Answer:** `python:3.13-slim`, git, uv, the frozen non-dev dependencies, the app, Alembic config,
README and `datasets/`; a non-root user (uid 10001) owning `/app` and `/data/workspace`; a
healthcheck on `/health`; and a CMD that runs `alembic upgrade head` then uvicorn. No models —
Ollama stays on the host. Image 919 MB (build session).
Pointer: `Dockerfile`; `.dockerignore`.

### Q6. What does CI check now?
**Answer:** ruff, format, mypy strict, pytest with a required Postgres; then three file-only gates:
the Day 3 baseline's floor check, **the optimized benchmark report vs the Day 4 baseline with the
regression thresholds**, and that the experiments file loads. A second job builds the image and
runs `agentops --help` in it.
Pointer: `.github/workflows/ci.yml`.

---

## Logical

### Q7. Why a token bucket per (tenant, route class)?
**Answer:** Continuous refill gives smooth limits with bounded bursts (capacity = per-minute
limit). Keying by tenant stops one tenant starving another; keying by route class stops cheap calls
from being throttled like 30-minute evals.
Pointer: `app/core/ratelimit.py:RateLimiter.acquire`.

### Q8. How is the wait time computed?
**Answer:** Tokens refill at `per_minute / 60` per second up to capacity; if fewer than 1 token is
left, the wait is `(1 − tokens) / rate`. With 2/min after two requests, the wait is just under
30 s; at t = 30 s a request is allowed again.
Pointer: `tests/unit/test_monitoring.py::test_rate_limiter_refuses_then_refills` (injected clock).

### Q9. Why is `expensive` only 6/min?
**Answer:** An eval or experiment holds the single local model for around 30 minutes, and a drift
check reads across tenants. The eval API also allows one run at a time per process (409); the rate
class stops hammering that endpoint.
Pointer: `app/api/evaluations.py`, `app/api/experiments.py` (`ConflictError`).

### Q10. Why make evaluations and monitoring admin-only?
**Answer:** Day 3 found that any authenticated tenant could start an eval: it runs as the `eval`
tenant with workspace write permissions and occupies the shared model. Monitoring reads cross-tenant
history. Both are operator functions. `/metrics` stays open because it's filtered to the caller.
Pointer: journal 08 §8 item 10; `app/api/monitoring.py` docstring.

### Q11. How can a profile not be used to escalate?
**Answer:** The API accepts a profile *name*, not settings. Profiles may override only an allowlist
of retrieval/routing/caching/chunking/model settings — no database URL, keys, workspace root or
limits — and are validated when loaded.
Pointer: `app/experiments/configurations.py:OVERRIDABLE`;
`tests/unit/test_monitoring.py::test_profiles_cannot_override_unsafe_settings`.

### Q12. Why run migrations at container start?
**Answer:** One service, one replica: running `alembic upgrade head` before uvicorn makes a fresh
database usable immediately, and Alembic is idempotent. With several replicas starting together it
would race — then it should be a one-shot job before rollout.
Pointer: `Dockerfile` CMD.

---

## Advanced

### Q13. Why not run Ollama in the container too?
**Answer:** Docker on macOS can't use the Apple GPU, so qwen3:8b would run on CPU. Ollama runs
natively on the host and the container reaches it at `host.docker.internal`
(`AGENTOPS_OLLAMA_BASE_URL_FROM_DOCKER`).
Pointer: `Dockerfile` header comment; `docker-compose.yml`.

### Q14. The container's `/ready` said Ollama was unreachable. What happened?
**Answer:** The native Ollama listened on loopback only — its log says `Listening on
127.0.0.1:11435` — so a connection from the container's network couldn't reach it. The database
check passed. The readiness check was right; the fix is starting Ollama with
`OLLAMA_HOST=0.0.0.0:<port>`, now documented.
Pointer: `reports/ollama-11435.log`; README "Hardening".

### Q15. Tell me about the port 11434 incident.
**Answer:** Every model call failed with `provider_error` while `/api/tags` answered. Port 11434 was
owned by another project's Docker Ollama, which didn't have the qwen3 models. I started a native
Ollama on 11435 and exported `AGENTOPS_OLLAMA_BASE_URL=http://localhost:11435` in every benchmark
script. The outage later appeared in the drift check's previous window: 83% failure rate, 52%
provider errors.
Pointer: `reports/*.sh`; `reports/drift-degraded.txt`; README "Troubleshooting".

### Q16. And the runs that died?
**Answer:** macOS sleep killed long benchmark runs twice — `bench2-aborted.log` stops after 15 of 38
cases. Long runs now go through `caffeinate -ims`. It's the local version of "batch jobs need to
survive the host": a real system would run evals as durable jobs.
Pointer: `reports/bench2-aborted.log`.

### Q17. What does "CI green" mean for a system whose core is a local LLM?
**Answer:** CI can't run qwen3:8b, so it proves code quality (lint, types, 370 tests incl. Postgres
integration) and that the committed evidence is consistent: the accepted optimized report still
passes the regression gate against the Day 4 baseline, and the experiments file validates. It does
not prove the current code produces those results; that needs a live run or replayed responses.
Pointer: `.github/workflows/ci.yml`.

---

## Scenario

### Q18. "We deploy three API replicas. What breaks?"
**Answer:** Rate limits and caches are in-process: each replica allows the full rate and has its own
caches (no correctness issue — keys are versioned — but lower hit rates). Migrations at start would
race. Eval runs' one-at-a-time lock is per process. Fix: Redis-backed buckets (same `acquire`
interface), a migration job, and durable eval jobs with a DB-level lock.
Pointer: `app/core/ratelimit.py` docstring; README "Known limitations".

### Q19. "A tenant sends 30 agent runs a minute, legally. Is the system OK?"
**Answer:** Not really: there's no global concurrency cap for model work. One local model serves
everyone, so 30 runs/min/tenant queue at Ollama; each run is bounded by its 180 s deadline, so many
will end `deadline_exceeded`. The limiter protects fairness between tenants, not capacity. Next step
is a global model-work queue with per-tenant fair share.
Pointer: `app/core/config.py` (`agent_deadline_s`, `rate_limit_agent_per_min`).

### Q20. "Someone set `AGENTOPS_ENV=prod` and forgot `AGENTOPS_ADMIN_TENANTS`."
**Answer:** Nobody is admin: evals, experiments and monitoring return 403. That's the intended
fail-closed behaviour, same as prod refusing to start without API keys or an explicit database URL.
Pointer: `app/core/config.py:admin_tenant_ids`, `_check`.

### Q21. "Is the workspace safe in the container?"
**Answer:** Safer than on the host — non-root uid 10001, the workspace on its own volume, the
executor's allowlist and path jail still apply — but still not an OS sandbox: no seccomp profile,
no network isolation per command, no read-only root filesystem. That's a documented Day 3 deferral.
Pointer: `Dockerfile`; README "Workspace".

---

## Trade-off

### Q22. In-process rate limiting vs Redis?
**Answer:** In-process is exact for the one process deployed, zero dependencies and trivially
testable with an injected clock. Redis costs a network hop per request and an extra service, but is
required for replicas. §16 allows a documented development alternative, so in-process plus a
documented upgrade path was the right call for this scope.
Pointer: `app/core/ratelimit.py`.

### Q23. Admin tenant list vs role-based access?
**Answer:** A list is one setting and one dependency, good enough for one operator. It conflates
"tenant" and "role": an admin tenant's key can do everything operator-level, and there's no
separate audit trail. At scale: roles/scopes on keys and an audit log of operator actions.
Pointer: `app/api/deps.py:require_admin`.

### Q24. A 919 MB image — acceptable?
**Answer:** For a local project, yes: slim base, git for the workspace executor, the full dependency
set. It's not optimized: single stage, `datasets/` inside. A multi-stage build, a distroless or
slimmer runtime and moving datasets to a volume would cut it.
Pointer: `Dockerfile`.

---

## Justification

### Q25. "Why ship rate limits that don't work across replicas?"
**Answer:** Because the deployment is one replica, the limitation is written down in three places
(module docstring, config comment, README), and the interface isolates the change. Shipping nothing
would leave the model endpoint unprotected; shipping Redis would add infrastructure the project
doesn't otherwise need.
Pointer: `app/core/ratelimit.py`; `app/core/config.py`.

### Q26. "Why gate committed reports in CI if it doesn't test the model?"
**Answer:** It catches the human errors that are otherwise silent: editing a threshold or a baseline
so the accepted benchmark no longer passes, or a malformed report or experiments file. It's honest
about what it is — the journal says it compares static files — and the live gate runs locally with
`--baseline`.
Pointer: journal 11 §8 item 1.

### Q27. "Why not fix `/ready` to pass in the container?"
**Answer:** It was correct: the API could not reach the model. Making it pass would hide a real
outage. The fix belongs to the Ollama bind address, and it's documented.
Pointer: README "Hardening".

---

## System design

### Q28. How would you harden this for 100x scale?
**Answer:** Gateway/Redis rate limits plus a global, fair-share queue for model work; durable jobs
for indexing, evals and experiments; roles and scopes with an audit log; a multi-stage image with
SBOM and vulnerability scanning; migrations as a pre-rollout job; model serving as its own
autoscaled service; per-PR CI replay of recorded model responses and a nightly live benchmark on
fixed hardware.
Pointer: journal 11 §9.

### Q29. Summarize Day 4 in one minute.
**Answer:** Optimization: usage accounting, rule routing, a calculator nudge, caches with versioned
permission-aware keys; the benchmark gate rejected two speedups (query rewrite off; routing that
skipped comparisons) and accepted the third — 36/38 vs 33/38, −21% tokens, −36% p50. Drift: a
model-free retrieval eval with answer coverage and a four-family detector with measured vs suspected
evidence; it caught a deliberate chunking regression that recall and hit rate missed. Hardening:
rate limits, admin-only operator APIs, Docker, CI gates. 370 tests. Still open: two golden cases,
loop mode, cache benefits unmeasured, CI without replay.
Pointer: journal 11 §10.

### Q30. What would you tell the next engineer about working on this machine?
**Answer:** Check which Ollama owns the port and that it has the models; log swap before trusting
latency; compare latency only back to back; use `caffeinate -ims` for long runs; repeat a
suspicious case 3× per profile — at temperature 0 that tells you whether it's behaviour; and never
believe a probe that disagrees with a live run until you've explained the disagreement.
Pointer: journals 09-11.
